"""CLI surface: the `--json` envelope {ok, data, next} and the exit-code vocabulary
(0 succeeded/ok · 1 failed · 2 usage/plan invalid · 3 running / parked / awaiting approval). Every command returns
at once (a background driver continues the run); only `--follow` waits."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from core_helpers import FLOWER_EXE, out_json, pid_alive, wait_for
from flower.engine import Engine
from flower.rundir import RunPaths, list_runs


def write_plan(tmp_path, nodes, name="plan.yaml", **top):
    plan = {"flower": 1, "id": top.pop("id", "cli-test"), "nodes": nodes, **top}
    p = tmp_path / name
    p.write_text(yaml.safe_dump(plan, sort_keys=False))
    return p


def sh(id_, run, **kw):
    return {"id": id_, "kind": "shell", "run": run, **kw}


def envelope(out):
    assert set(out) >= {"ok", "next"} and ("data" in out or "error" in out)
    assert isinstance(out["next"], list)
    return out


def latest_run(home):
    return list_runs(home)[-1]


# ------------------------------------------------------------------ plan validate

def test_plan_validate_ok_and_invalid(cli, tmp_path):
    good = write_plan(tmp_path, [sh("a", "true")])
    code, out = cli("plan", "validate", str(good))
    assert code == 0 and envelope(out)["ok"] and out["data"]["valid"] and out["data"]["digest"].startswith("sha256:")
    bad = write_plan(tmp_path, [sh("a", "echo ${zz.outputs.x}", typo=1), {"id": "b", "kind": "shell"}], name="bad.yaml")
    code, out = cli("plan", "validate", str(bad))
    assert code == 2 and envelope(out)["ok"] is False
    err = out["error"]
    assert err["code"] == "plan_invalid" and len(err["details"]) >= 3
    assert all({"code", "path", "message"} <= set(d) for d in err["details"])


def test_plan_file_errors(cli, tmp_path):
    code, out = cli("plan", "validate", str(tmp_path / "missing.yaml"))
    assert code == 2 and out["error"]["code"] == "plan_not_found"
    (tmp_path / "broken.yaml").write_text("nodes: [\n  - id: a\n   bad")
    code, out = cli("plan", "validate", str(tmp_path / "broken.yaml"))
    assert code == 2 and out["error"]["code"] == "plan_yaml"


# ------------------------------------------------------------------ run / exit codes

def test_run_follow_succeeded_exit_0(cli, tmp_path):
    p = write_plan(tmp_path, [sh("a", out_json({"v": 1})), sh("b", "echo ${a.outputs.v}")])
    code, out = cli("run", str(p), "--follow", "--timeout", "30")
    assert code == 0 and envelope(out)["ok"]
    d = out["data"]
    assert d["status"] == "succeeded" and [n["status"] for n in d["nodes"]] == ["succeeded", "succeeded"]


def test_run_failed_exit_1(cli, tmp_path):
    p = write_plan(tmp_path, [sh("a", "exit 5")])
    code, out = cli("run", str(p), "--follow", "--timeout", "30")
    assert code == 1 and envelope(out)["ok"] is False
    assert out["data"]["status"] == "failed"
    failed = [n for n in out["data"]["nodes"] if n["status"] == "failed"][0]
    assert failed["error"]["error_class"] == "exit_nonzero"
    assert any("rerun" in n for n in out["next"])


def test_run_invalid_plan_exit_2_and_no_run_created(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "true", needs=["ghost"])])
    code, out = cli("run", str(p))
    assert code == 2 and out["error"]["code"] == "plan_invalid"
    assert list_runs(home) == []


def test_run_missing_input_exit_2(cli, tmp_path):
    p = write_plan(tmp_path, [sh("a", "true")], inputs={"x": {"type": "integer"}})
    code, out = cli("run", str(p))
    assert code == 2 and out["error"]["details"][0]["code"] == "input_missing"
    code, out = cli("run", str(p), "-i", "x=notint")
    assert code == 2 and out["error"]["details"][0]["code"] == "input_type"
    code, out = cli("run", str(p), "-i", "x=3", "-i", "badformat")
    assert code == 2 and out["error"]["code"] == "bad_input"


def test_run_returns_at_once_and_the_run_goes_on(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "sleep 5"), sh("b", "echo after", needs=["a"])])
    t0 = time.time()
    code, out = cli("run", str(p), "-y")              # -y: accepted, what `run` does anyway
    assert code == 0 and envelope(out)["ok"] and time.time() - t0 < 10
    rid = out["data"]["run_id"]
    assert out["data"]["status"] == "running"
    code, out = cli("status", rid, "--follow", "--timeout", "30")
    assert code == 0 and out["data"]["status"] == "succeeded"
    assert Engine(RunPaths(home, rid)).state().nodes["b"].result.summary == "after"


def test_run_review_waits_for_approval_exit_3_then_approve(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "true")])
    code, out = cli("run", str(p), "--review")
    assert code == 3 and envelope(out)["ok"] is True
    assert out["data"]["status"] == "awaiting_approval"
    assert any("approve" in n for n in out["next"])
    rid = out["data"]["run_id"]
    code, out = cli("status", rid, "--follow", "--timeout", "2")
    assert code == 3
    code, out = cli("approve", rid, "-m", "ok by me")
    assert code == 0 and envelope(out)["ok"]
    code, out = cli("status", rid, "--follow", "--timeout", "30")
    assert code == 0 and out["data"]["status"] == "succeeded"
    st = Engine(RunPaths(home, rid)).state()
    assert st.approved_by == "human:tester"


def test_reject_plan(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "true")])
    _, out = cli("run", str(p), "--review")
    rid = out["data"]["run_id"]
    code, out = cli("reject", rid, "--note", "too big")
    assert code == 0
    assert out["data"]["status"] == "rejected"
    code, out = cli("status", rid, "--follow")
    assert code == 1 and out["data"]["status"] == "rejected"


def test_gate_parks_exit_3_answer_resumes(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "true"), {"id": "review", "kind": "gate", "needs": ["a"]},
                              sh("b", "echo ${review.outputs.text}", needs=["review"]),
                              {"id": "pick", "kind": "gate", "needs": ["b"], "decisions": ["small", "large"]}])
    code, out = cli("run", str(p), "--follow", "--timeout", "30")
    assert code == 3 and out["ok"] and out["data"]["status"] == "parked"
    gates = out["data"]["open_gates"]
    assert [g["id"] for g in gates] == ["review#a1"]
    assert any(n.startswith("flower approve") for n in out["next"])
    rid = out["data"]["run_id"]
    code, out = cli("show", rid, "review#a1")           # a gate by its id, where a step would go
    assert code == 0 and out["data"]["status"] == "open"
    code, out = cli("show", rid, "--gate", "review")
    assert code == 0 and out["data"]["status"] == "open"
    code, out = cli("approve", rid, "review", "maybe")
    assert code == 2 and out["error"]["code"] == "bad_decision"
    code, out = cli("approve", rid, "review", "--note", "lgtm", "--follow", "--timeout", "30")
    assert code == 3 and out["data"]["status"] == "parked"
    code, out = cli("show", rid, "b")
    assert code == 0 and out["data"]["attempts"][-1]["summary"] == "lgtm"
    code, out = cli("approve", rid, "pick", "large", "--follow", "--timeout", "30")   # a gate's own decisions
    assert code == 0 and out["data"]["status"] == "succeeded"
    assert Engine(RunPaths(home, rid)).state().nodes["pick"].result.outputs["decision"] == "large"
    code, out = cli("approve", rid, "review")
    assert code == 2 and out["error"]["code"] in ("gate_closed", "gate_not_found", "which_gate")


def test_reject_text_is_the_rework_instruction(cli, tmp_path, home):
    """BUGS #64: the rework instruction of `reject --text T` was lost; --text is now another name of --note, and
    the step sent back reads it as ${feedback}."""
    p = write_plan(tmp_path, [sh("a", "echo '${feedback}' > fb.txt", files={"fb": "fb.txt"}),
                              {"id": "review", "kind": "gate", "needs": ["a"], "on_reject": {"rerun": ["a"]}}])
    code, out = cli("run", str(p), "--follow", "--timeout", "30")
    rid = out["data"]["run_id"]
    code, out = cli("reject", rid, "review", "--text", "k=20", "--follow", "--timeout", "30")
    assert code == 3, out
    fb = Engine(RunPaths(home, rid)).state().nodes["a"].result.files["fb"]["path"]
    assert open(fb).read().split()[0] == "k=20"


def test_status_follow_list_show_log(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", out_json({"v": {"deep": [10, 20]}, "s": "hi"})
                                 + "\necho data > f.txt; echo printed", files={"f": "f.txt"})])
    cli("run", str(p), "--follow", "--timeout", "30")
    rid = latest_run(home)
    code, out = cli("status", rid)
    assert code == 0 and out["data"]["status"] == "succeeded" and out["data"]["run_id"] == rid
    code, out = cli("status", rid, "--follow")
    assert code == 0
    code, out = cli("status")                       # without a run: the project's runs
    assert code == 0 and out["data"][0]["run_id"] == rid and out["data"][0]["done"] == 1
    code, out = cli("show", rid, "a")
    assert code == 0 and out["data"]["spec"]["id"] == "a" and len(out["data"]["attempts"]) == 1
    assert out["data"]["outputs"]["s"] == "hi"
    code, out = cli("show", rid, "a", "v.deep.1")
    assert code == 0 and out["data"] == 20
    code, out = cli("show", rid, "a", "f")
    assert out["data"].endswith("f.txt")
    code, out = cli("log", rid)
    assert code == 0 and out["data"][0]["eventType"] == "run.created"
    code, out = cli("log", rid, "--node", "a")
    assert out["data"] and all(e["nodeId"] == "a" for e in out["data"])
    code, out = cli("log", rid, "--note", "looked at it")
    assert code == 0 and Engine(RunPaths(home, rid)).journal.read()[-1]["payload"]["text"] == "looked at it"
    code, out = cli("show", rid, "a", "--logs")
    assert code == 0 and "printed" in out["data"]["text"]
    code, alias = cli("logs", rid, "a")             # the earlier name of `show --logs`
    assert code == 0 and alias["data"] == out["data"]
    code, out = cli("show", rid, "zz")
    assert code == 2 and out["error"]["code"] == "node_not_found"


def test_show_unknown_key_is_a_clean_error(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", out_json({"v": [1]}))])
    cli("run", str(p), "--follow", "--timeout", "30")
    rid = latest_run(home)
    for key in ("nope", "v.x", "v.5"):
        code, out = cli("show", rid, "a", key)
        assert code == 2 and out["ok"] is False


def test_logs_unknown_attempt_is_a_clean_error(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "true")])
    cli("run", str(p), "--follow", "--timeout", "30")
    code, out = cli("show", latest_run(home), "a", "--logs", "--attempt", "9")
    assert code == 2 and out["ok"] is False


def test_reject_unknown_gate_is_a_clean_error(cli, tmp_path, home):
    p = write_plan(tmp_path, [{"id": "g", "kind": "gate"}])
    cli("run", str(p), "--follow", "--timeout", "30")
    code, out = cli("reject", latest_run(home), "nosuchgate")
    assert code == 2 and out["error"]["code"] == "gate_not_found"


def test_rerun_cli(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", out_json({"v": 1})), sh("b", "echo ${a.outputs.v}")])
    cli("run", str(p), "--follow", "--timeout", "30")
    rid = latest_run(home)
    code, out = cli("rerun", rid, "a")                 # returns at once; the run goes on in the background
    assert code == 0 and "queued again: a, b" in out["message"]
    code, out = cli("status", rid, "--follow", "--timeout", "30")
    assert code == 0 and out["data"]["status"] == "succeeded"
    st = Engine(RunPaths(home, rid)).state()
    assert len(st.nodes["a"].attempts) == 2 and st.nodes["b"].result.reused_from == "a1"
    code, out = cli("rerun", rid, "nope")
    assert code == 2 and out["error"]["code"] == "node_not_found"


def test_an_edit_of_finished_work_applies_at_once(cli, tmp_path, home):
    """Plan edits do not wait for approval: `rerun RUN` applies the plan file, a finished step whose command changed
    runs again with its downstream (the earlier attempts stay in the record), and the change is recorded with who
    made it and the diff. A description-only edit applies without re-running anything."""
    p = write_plan(tmp_path, [sh("a", out_json({"v": 1})), sh("b", "echo ${a.outputs.v}")])
    cli("run", str(p), "--follow", "--timeout", "30")
    rid = latest_run(home)
    write_plan(tmp_path, [sh("a", out_json({"v": 2})), sh("b", "echo ${a.outputs.v}")])
    code, out = cli("rerun", rid, "--follow", "--timeout", "30")
    assert code == 0 and out["data"]["status"] == "succeeded", out
    eng = Engine(RunPaths(home, rid))
    st = eng.state()
    assert st.generation == 1 and st.nodes["b"].result.summary == "2"
    assert [len(st.nodes[n].attempts) for n in "ab"] == [2, 2] and st.nodes["a"].attempts[0].outputs == {"v": 1}
    ev = [e for e in eng.journal.read() if e["eventType"].startswith("plan.amendment.")]
    assert [e["eventType"] for e in ev] == ["plan.amendment.proposed", "plan.amendment.approved"]
    assert ev[1]["actor"] == "human:tester" and ev[0]["payload"]["diff"]["changed"][0]["id"] == "a"
    write_plan(tmp_path, [sh("a", out_json({"v": 2}), description="Makes v."), sh("b", "echo ${a.outputs.v}")])
    code, out = cli("rerun", rid, "--follow", "--timeout", "30")
    st = eng.state()
    assert code == 0 and st.generation == 2 and st.graph().nodes["a"]["description"] == "Makes v."
    assert [len(st.nodes[n].attempts) for n in "ab"] == [2, 2]


def test_cancel_cli(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("slow", "sleep 30")])
    code, out = cli("run", str(p))
    rid = latest_run(home)
    code, out = cli("cancel", rid, "--reason", "enough")
    assert code == 0 and out["data"]["status"] == "cancelled"
    code, out = cli("status", rid, "--follow")
    assert code == 1
    code, out = cli("cancel", rid)
    assert code == 2 and out["error"]["code"] == "run_finished"


def test_approve_note_is_recorded(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", out_json({"v": 1})), {"id": "g", "kind": "gate", "needs": ["a"]}])
    cli("run", str(p), "--follow", "--timeout", "30")
    rid = latest_run(home)
    cli("approve", rid, "g", "--note", "fine")
    code, out = cli("log", rid)
    assert code == 0 and any(e["eventType"] == "gate.answered" and e["payload"]["text"] == "fine" for e in out["data"])


def test_status_makes_a_pass(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "true")])
    _, out = cli("run", str(p), "--review")
    rid = out["data"]["run_id"]
    Engine(RunPaths(home, rid)).answer("plan", "approve")
    code, out = cli("status", rid)
    assert code in (0, 3) and Engine(RunPaths(home, rid)).state().nodes["a"].attempts


def test_unknown_run_and_no_runs(cli, home, tmp_path):
    code, out = cli("status")
    assert code == 0 and out["data"] == []
    p = write_plan(tmp_path, [sh("a", "true")])
    cli("run", str(p), "--review")
    code, out = cli("status", "does-not-exist")
    assert code == 2 and out["error"]["code"] == "run_not_found"


def test_usage_error_exit_2(cli):
    with pytest.raises(SystemExit) as ei:
        cli("approve", "a", "b", "c", "d")
    assert ei.value.code == 2
    from flower.cli import main
    assert main([]) == 2


def test_corrupt_journal_exit_1(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", "true")])
    _, out = cli("run", str(p), "--review")
    ev = RunPaths(home, out["data"]["run_id"]).events
    lines = ev.read_text().splitlines(keepends=True)
    lines[0] = "garbage\n"
    ev.write_text("".join(lines))
    code, out = cli("status", out["data"]["run_id"])
    assert code == 1 and out["error"]["code"] == "journal_corrupt"


# ------------------------------------------------------------------ real executable

def _exe(args, env, cwd, timeout=60):
    return subprocess.run([str(FLOWER_EXE), *args], env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


def test_installed_executable_envelope_and_codes(tmp_path, home):
    env = dict(os.environ)
    ok = write_plan(tmp_path, [sh("a", "echo hello")])
    r = _exe(["run", str(ok), "--follow", "--json", "--timeout", "30"], env, tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] and out["data"]["status"] == "succeeded"
    bad = write_plan(tmp_path, [sh("a", "exit 2")], name="bad.yaml")
    r = _exe(["run", str(bad), "--follow", "--json", "--timeout", "30"], env, tmp_path)
    assert r.returncode == 1 and json.loads(r.stdout)["data"]["status"] == "failed"
    gate = write_plan(tmp_path, [{"id": "g", "kind": "gate"}], name="gate.yaml")
    r = _exe(["run", str(gate), "--follow", "--json", "--timeout", "30"], env, tmp_path)
    assert r.returncode == 3 and json.loads(r.stdout)["data"]["status"] == "parked"
    r = _exe(["plan", "validate", str(tmp_path / "nope.yaml"), "--json"], env, tmp_path)
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "plan_not_found"
    # human (non-json) mode: errors go to stderr with a hint
    r = _exe(["plan", "validate", str(tmp_path / "nope.yaml")], env, tmp_path)
    assert r.returncode == 2 and "error [plan_not_found]" in r.stderr


def test_detached_driver_killed_then_status_resumes(tmp_path, home):
    env = dict(os.environ)
    # `a` must outlast the CLI's own exit: on a loaded machine with a network-mounted home a 1 s step let the whole
    # run (and its driver) finish before the test looked at the driver
    p = write_plan(tmp_path, [sh("a", "sleep 5"), sh("b", "echo after", needs=["a"])])
    r = _exe(["run", str(p), "--json"], env, tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    rid = out["data"]["run_id"]
    pid = json.loads(RunPaths(home, rid).driver_file.read_text())["pid"]
    assert pid_alive(pid), Engine(RunPaths(home, rid)).state().status
    os.kill(pid, signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(pid), 5)
    eng = Engine(RunPaths(home, rid))
    deadline = time.time() + 20
    while time.time() < deadline and eng.state().status not in ("succeeded", "failed"):
        r = _exe(["status", rid, "--json"], env, tmp_path)
        assert r.returncode in (0, 3), r.stderr
        time.sleep(0.2)
    st = eng.state()
    assert st.status == "succeeded" and st.nodes["b"].result.summary == "after"
    assert len(st.nodes["a"].attempts) == 1


def test_status_exits_with_the_runs_status_code(cli, tmp_path, home):
    """`status` exits like `--follow` does: 0 succeeded, 1 failed, 3 running / parked / awaiting approval."""
    p = write_plan(tmp_path, [sh("a", "exit 1")])
    cli("run", str(p), "--follow", "--timeout", "30")
    rid = latest_run(home)
    code, out = cli("status", rid)
    assert code == 1 and out["data"]["status"] == "failed"
    code, _ = cli("status", rid, "--follow")
    assert code == 1
    ok = write_plan(tmp_path, [sh("a", "true")], name="ok.yaml")
    code, out = cli("run", str(ok), "--follow", "--timeout", "30")
    assert code == 0 and cli("status", out["data"]["run_id"])[0] == 0
    gate = write_plan(tmp_path, [{"id": "g", "kind": "gate"}], name="gate.yaml")
    code, out = cli("run", str(gate), "--follow", "--timeout", "30")
    assert code == 3 and cli("status", out["data"]["run_id"])[0] == 3                      # parked on a gate
    code, out = cli("run", str(ok), "--review")
    assert code == 3 and cli("status", out["data"]["run_id"], "--no-tick")[0] == 3         # awaiting approval


def test_a_step_cannot_answer_the_users_gate(cli, tmp_path, home):
    """A step's process runs with FLOWER_INSIDE_RUN; `flower approve` from inside it is refused, so work under way
    can never sign off a decision that belongs to the user."""
    p = write_plan(tmp_path, [{"id": "review", "kind": "gate", "message": "is the structure right?"},
                              sh("sneaky", f"{FLOWER_EXE} approve $FLOWER_RUN_ID review --note lgtm "
                                           "|| true")])
    cli("run", str(p), "--follow", "--timeout", "30")
    st = Engine(RunPaths(home, latest_run(home))).state()
    assert st.nodes["sneaky"].status == "succeeded"
    assert st.gates["review#a1"].status == "open", f"answered from inside a step by {st.gates['review#a1'].by}"


def test_run_reuse_takes_the_earlier_runs_inputs(cli, tmp_path, home):
    p = write_plan(tmp_path, [sh("a", 'echo "{\\"h\\": \\"${inputs.host}\\"}" > "$FLOWER_OUTPUTS"')],
                   inputs={"host": {"type": "string", "required": True}})
    code, out = cli("run", str(p), "--follow", "-i", "host=box", "--timeout", "30")
    assert code == 0, out
    first = out["data"]["run_id"]
    code, out = cli("run", str(p), "--follow", "--reuse", first, "--timeout", "30")   # no -i: the inputs come along
    assert code == 0, out
    st = Engine(RunPaths(home, out["data"]["run_id"])).state()
    assert st.inputs == {"host": "box"} and st.nodes["a"].result.reused_from == f"{first}:a#a1"
    write_plan(tmp_path, [sh("a", "true")])                     # the plan no longer declares `host`
    code, out = cli("run", str(p), "--follow", "--reuse", first, "--timeout", "30")
    assert code == 0, out


def test_a_driver_does_not_switch_to_code_that_does_not_import(tmp_path, monkeypatch):
    """BUGS #59: the drivers of running studies reloaded flower's code while it was being edited and failed on every
    tick until it was whole again. A changed code base is now imported in a fresh interpreter first."""
    from flower.cli import _new_code_broken
    assert _new_code_broken() is None
    bad = tmp_path / "shadow" / "flower"
    bad.mkdir(parents=True)
    (bad / "__init__.py").write_text("raise ImportError('half-written edit')\n")
    monkeypatch.setenv("PYTHONPATH", str(bad.parent))
    assert "half-written edit" in _new_code_broken()


def test_the_reference_doc_carries_the_agents_plan_reference():
    """docs/REFERENCE.md and the PLAN_REFERENCE.md installed with the skill drifted apart once; one source now."""
    from flower.skill import PLAN_REFERENCE
    doc = (Path(__file__).resolve().parents[2] / "docs" / "REFERENCE.md").read_text()
    body = PLAN_REFERENCE.split("\n", 2)[2].replace("\n## ", "\n### ").rstrip()
    assert body in doc
