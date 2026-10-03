"""The development loop inside a run: edit the plan file (or the code), `flower rerun RUN NODE --follow`.

* rerun picks up edits to the plan file for the node, its downstream and new nodes, as a recorded amendment;
* approval: `policies: {edits: ask|unfinished|all}` (default ask) or `--yes` from the human running it;
* --follow watches one node until it finishes and exits 0/1 by its result; while someone follows, polling is fast.
"""
from __future__ import annotations

import json
import os
import socket
import textwrap
from pathlib import Path

from flower.engine import Engine
from flower.plan import normalize, validate
from flower.rundir import RunPaths, followers, list_runs


def _plan(tmp_path: Path, nodes: str, policy: str | None = None) -> Path:
    p = tmp_path / "plan.yaml"
    pol = f"policies: {{edits: {policy}}}\n" if policy else ""
    p.write_text("flower: 1\nid: dev\n" + pol + "nodes:\n" + textwrap.dedent(nodes))
    return p


A_OK = """\
  - {id: a, kind: shell, run: 'echo "{\\"x\\": 2}" > "$FLOWER_OUTPUTS"', outputs: {x: integer}}
"""
B_FAIL = """\
  - {id: b, kind: shell, run: 'echo boom >&2; exit 3', outputs: {y: integer}}
"""
B_FIXED = """\
  - {id: b, kind: shell, run: 'echo "{\\"y\\": $((${a.outputs.x} * 10))}" > "$FLOWER_OUTPUTS"', outputs: {y: integer}}
"""


def _start(cli, plan: Path) -> str:
    code, res = cli("run", str(plan), "--yes")
    return res["data"]["run_id"] if "run_id" in res.get("data", {}) else list_runs(Path(os.environ["FLOWER_HOME"]))[-1]


def _eng(home, rid) -> Engine:
    return Engine(RunPaths(home, rid))


def test_rerun_picks_up_a_plan_edit_and_follows_the_node(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FAIL, policy="unfinished")
    rid = _start(cli, plan)
    assert _eng(home, rid).state().nodes["b"].status == "failed"
    _plan(tmp_path, A_OK + B_FIXED, policy="unfinished")  # fix it in the plan file
    code, res = cli("rerun", rid, "b", "--follow")
    assert code == 0 and res["data"]["status"] == "succeeded", res
    st = _eng(home, rid).state()
    assert st.nodes["b"].result.outputs["y"] == 20
    am = [a for a in st.amendments.values() if "plan file edited" in (a.rationale or "")]
    assert len(am) == 1 and am[0].status == "approved"
    assert st.generation == 1
    assert not (_eng(home, rid).paths.follow / "b.json").exists()  # the follow marker is gone


def test_default_policy_asks_before_applying_an_edit(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FAIL)
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED)
    code, res = cli("rerun", rid, "b")
    assert code == 3 and res["data"]["gate"].startswith("amend-")
    eng = _eng(home, rid)
    assert eng.state().nodes["b"].status == "failed"  # nothing re-ran with the old definition
    code, _ = cli("approve", rid, res["data"]["gate"])
    assert code in (0, 3)
    code, res = cli("rerun", rid, "b", "--follow")
    assert code == 0, res
    assert _eng(home, rid).state().nodes["b"].result.outputs["y"] == 20


def test_yes_approves_the_edit(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FAIL)
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED)
    code, res = cli("rerun", rid, "b", "--follow", "--yes")
    assert code == 0 and _eng(home, rid).state().nodes["b"].result.outputs["y"] == 20


def test_unfinished_policy_still_asks_for_a_finished_node(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FIXED, policy="unfinished")
    rid = _start(cli, plan)
    assert _eng(home, rid).state().status == "succeeded"
    _plan(tmp_path, A_OK.replace("2}", "3}") + B_FIXED, policy="unfinished")  # edit a succeeded node
    code, res = cli("rerun", rid, "a")
    assert code == 3 and res["data"]["changed"] == ["a"]


def test_new_node_added_by_rerun(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FIXED, policy="unfinished")
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED + """\
  - {id: c, kind: shell, run: 'echo "{\\"z\\": $((${b.outputs.y} + 1))}" > "$FLOWER_OUTPUTS"', outputs: {z: integer}}
""", policy="unfinished")
    code, res = cli("rerun", rid, "c", "--follow")
    assert code == 0, res
    st = _eng(home, rid).state()
    assert st.nodes["c"].result.outputs["z"] == 21
    assert len(st.nodes["a"].attempts) == 1 and len(st.nodes["b"].attempts) == 1  # nothing else re-ran


def test_only_the_rerun_cone_is_picked_up(home, tmp_path, cli):
    plan = _plan(tmp_path, A_OK + B_FIXED + """\
  - {id: other, kind: shell, run: 'echo one'}
""", policy="unfinished")
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED.replace("* 10", "* 100") + """\
  - {id: other, kind: shell, run: 'echo two'}
""", policy="all")
    ed = _eng(home, rid).plan_edits("a")
    assert ed["changed"] == ["b"] and ed["added"] == []  # `other` is not downstream of `a`


def test_follow_reports_failure_with_exit_1(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FAIL)
    rid = _start(cli, plan)
    code, res = cli("rerun", rid, "b", "--follow", "--no-edits")
    assert code == 1 and res["data"]["status"] == "failed"
    assert res["data"]["error"]["error_class"] == "exit_nonzero"


def test_followers_make_polling_fast(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK)
    rid = _start(cli, plan)
    paths = RunPaths(home, rid)
    paths.follow.mkdir(parents=True, exist_ok=True)
    (paths.follow / "a.json").write_text(json.dumps({"node": "a", "pid": os.getpid(), "host": socket.gethostname()}))
    assert followers(paths) == ["a"]
    (paths.follow / "dead.json").write_text(json.dumps({"node": "x", "pid": 2 ** 22 + 12345, "host": socket.gethostname()}))
    assert followers(paths) == ["a"] and not (paths.follow / "dead.json").exists()  # stale markers are cleaned


def test_edits_policy_is_validated():
    raw = {"flower": 1, "id": "p", "policies": {"edits": "sometimes"}, "nodes": [{"id": "a", "kind": "shell", "run": "true"}]}
    assert any(i["path"] == "policies.edits" for i in validate(normalize(raw)))


# ---------------------------------------------------------------------- the run first: start / add / init / hook
import io  # noqa: E402

from flower import devloop  # noqa: E402


def _rid(home) -> str:
    return list_runs(Path(os.environ["FLOWER_HOME"]))[-1]


def test_start_creates_a_parked_draft_run(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    code, res = cli("start", "Find the answer", "--id", "ans", "--dir", str(tmp_path / "ans"))
    assert code == 0, res
    rid = res["data"]["run_id"]
    st = _eng(home, rid).state()
    assert st.status == "parked" and "no steps yet" in st.status_reason
    plan = (tmp_path / "ans" / "plan.yaml").read_text()
    assert "nodes: []" in plan and "edits: unfinished" in plan
    code, res = cli("start", "Find the answer", "--id", "ans", "--dir", str(tmp_path / "ans"))
    assert code != 0 and res["error"]["code"] == "exists"


def test_add_writes_the_step_and_runs_it(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "Find the answer", "--id", "ans", "--dir", str(tmp_path / "ans"))
    rid = res["data"]["run_id"]
    code, res = cli("add", rid, "first", "--out", "x:integer", "--title", "first one", "--",
                    'echo "{\\"x\\": 41}" > "$FLOWER_OUTPUTS"')
    assert code == 0 and res["data"]["status"] == "succeeded", res
    code, res = cli("add", rid, "second", "--needs", "first", "--out", "y:integer", "--",
                    'echo "{\\"y\\": $((${first.outputs.x} + 1))}" > "$FLOWER_OUTPUTS"')
    assert code == 0, res
    st = _eng(home, rid).state()
    assert st.nodes["second"].result.outputs["y"] == 42
    assert st.generation == 2 and st.status == "succeeded"
    text = (tmp_path / "ans" / "plan.yaml").read_text()
    assert "- id: first" in text and "title: first one" in text and "# A draft started" in text  # comments kept
    code, res = cli("add", rid, "first", "--", "true")
    assert code != 0 and res["error"]["code"] == "exists"


def test_add_refuses_an_invalid_step_and_leaves_the_file(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    before = (tmp_path / "x" / "plan.yaml").read_text()
    code, res = cli("add", res["data"]["run_id"], "bad", "--needs", "nope", "--", "true")
    assert code != 0
    assert (tmp_path / "x" / "plan.yaml").read_text() == before


def test_new_inputs_and_clusters_reach_a_running_draft(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    rid = res["data"]["run_id"]
    p = tmp_path / "x" / "plan.yaml"
    p.write_text(p.read_text().replace("clusters: {}", (
        "inputs:\n  scratch: {type: string}\n  greeting: {type: string, default: hello}\n\n"
        "clusters:\n  box: {transport: local, scheduler: none, remote_root: \"${inputs.scratch}\"}")))
    code, res = cli("add", rid, "s", "--cluster", "box", "-i", f"scratch={tmp_path / 'remote'}", "--out",
                    "g:string", "--", 'echo "{\\"g\\": \\"${inputs.greeting}\\"}" > "$FLOWER_OUTPUTS"')
    assert code == 0, res
    st = _eng(home, rid).state()
    assert st.nodes["s"].result.outputs["g"] == "hello"
    assert st.plan["clusters"]["box"]["remote_root"] == str(tmp_path / "remote")
    assert (tmp_path / "remote").is_dir()
    # an existing cluster is fixed: a change is reported, not applied
    p.write_text(p.read_text().replace("scheduler: none,", "scheduler: none, max_jobs: 3,"))
    ed = _eng(home, rid).plan_edits("s")
    assert ed["ignored"] and "fixed" in ed["ignored"][0]


def test_new_input_without_a_value_is_refused(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    p = tmp_path / "x" / "plan.yaml"
    p.write_text(p.read_text().replace("clusters: {}", "inputs:\n  host: {type: string}\n\nclusters: {}"))
    code, res = cli("add", res["data"]["run_id"], "s", "--", "true")
    assert code != 0 and res["error"]["code"] == "input_value"


def test_insert_node_keeps_the_rest_of_the_file():
    text = "flower: 1\nid: p\nnodes:\n    - {id: a, kind: shell, run: 'true'}   # four-space list\n\nresults: [a]\n"
    out = devloop.insert_node(text, {"id": "b", "kind": "shell", "run": "echo 1\necho 2"})
    assert out.index("- id: b") < out.index("results: [a]")
    assert "\n    - id: b\n" in out and "run: |" in out and "# four-space list" in out


def test_init_ships_the_instructions_with_the_project(cli, home, tmp_path):
    code, res = cli("init", str(tmp_path / "proj"), "--hook")
    assert code == 0, res
    root = tmp_path / "proj"
    assert (root / ".claude" / "skills" / "flower" / "SKILL.md").is_file()
    assert (root / ".agents" / "skills" / "flower" / "SKILL.md").is_file()
    agents = (root / "AGENTS.md").read_text()
    assert "flower start" in agents and devloop.AGENTS_BEGIN in agents
    cfg = json.loads((root / ".claude" / "settings.local.json").read_text())
    cmd = cfg["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert cmd.endswith("-m flower hook bash")
    (root / "AGENTS.md").write_text("# mine\n\n" + agents)
    cli("init", str(root), "--hook")   # idempotent: one managed block, one hook, the user's text kept
    agents = (root / "AGENTS.md").read_text()
    assert agents.count(devloop.AGENTS_BEGIN) == 1 and agents.startswith("# mine")
    assert len(json.loads((root / ".claude" / "settings.local.json").read_text())["hooks"]["PostToolUse"]) == 1


def test_hook_reminds_only_about_compute_beside_an_active_run(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    root = Path(os.environ["FLOWER_HOME"]).parent if (Path(os.environ["FLOWER_HOME"]).name == ".flower") \
        else Path(os.environ["FLOWER_HOME"])
    cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    proj = devloop._project_root(Path.cwd()) or devloop._project_root(root)
    assert proj is not None
    cwd = proj
    say = lambda c, s="s1": devloop.reminder(c, cwd, s)  # noqa: E731
    assert say("ls -la") is None and say("python3 -c 'print(1)'") is None and say("flower status x") is None
    msg = say("python3 analyse.py")
    assert msg and "flower add" in msg
    assert say("mpirun -np 4 pw.x") is None            # quiet for a while after a reminder in that session
    assert say("ssh box 'mpirun -np 4 pw.x'", "s2")
    monkeypatch.setenv("FLOWER_INSIDE_RUN", "1")
    assert say("python3 analyse.py", "s3") is None       # inside a step: never
    monkeypatch.delenv("FLOWER_INSIDE_RUN")
    assert say("F=../.venv/bin/flower; $F remote exec -- 'python3 x.py'", "s4") is None   # flower, via a variable
    assert say("cat > check.sh <<'EOF'\npython3 - <<'PY'\nprint(1)\nPY\nEOF\nls", "s5") is None   # writing a file


def test_hook_command_never_fails(cli, home, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    code, text = cli("hook", "bash", as_json=False)
    assert code == 0 and not text.strip()
