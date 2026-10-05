"""The development loop inside a run: edit the plan file (or the code), `flower rerun RUN NODE --follow`.

* rerun picks up edits to the plan file for the node, its downstream and new nodes, as a recorded amendment;
* new and unfinished steps change at once; an edit of a finished step waits for approval (or `--yes` from the human running it);
* --follow watches one node until it finishes and exits 0/1 by its result; while someone follows, polling is fast.
"""
from __future__ import annotations

import json
import os
import socket
import textwrap
from pathlib import Path
import pytest

from flower.engine import Engine
from flower.plan import normalize, validate
from flower.rundir import RunPaths, followers, list_runs


def _plan(tmp_path: Path, nodes: str) -> Path:
    p = tmp_path / "plan.yaml"
    p.write_text("flower: 1\nid: dev\nnodes:\n" + textwrap.dedent(nodes))
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
    plan = _plan(tmp_path, A_OK + B_FAIL)
    rid = _start(cli, plan)
    assert _eng(home, rid).state().nodes["b"].status == "failed"
    _plan(tmp_path, A_OK + B_FIXED)  # fix it in the plan file
    code, res = cli("rerun", rid, "b", "--follow")
    assert code == 0 and res["data"]["status"] == "succeeded", res
    st = _eng(home, rid).state()
    assert st.nodes["b"].result.outputs["y"] == 20
    am = [a for a in st.amendments.values() if "plan file edited" in (a.rationale or "")]
    assert len(am) == 1 and am[0].status == "approved"
    assert st.generation == 1
    assert not (_eng(home, rid).paths.follow / "b.json").exists()  # the follow marker is gone


def test_an_edit_of_a_failed_step_applies_and_of_a_succeeded_one_asks(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FAIL)
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED)
    code, res = cli("rerun", rid, "b", "--follow")         # b failed: unfinished, so the fix applies at once
    assert code == 0, res
    assert _eng(home, rid).state().nodes["b"].result.outputs["y"] == 20
    _plan(tmp_path, A_OK + B_FIXED.replace("* 10", "* 100"))   # b succeeded: an edit reached by rerunning a asks
    code, res = cli("rerun", rid, "a")
    assert code == 3 and res["data"]["gate"].startswith("amend-")
    assert _eng(home, rid).state().nodes["b"].result.outputs["y"] == 20
    code, _ = cli("approve", rid, res["data"]["gate"])        # BUGS #20: approved on a finished run, it applies
    assert code in (0, 3)
    code, res = cli("rerun", rid, "a", "--follow")
    assert code == 0, res
    assert _eng(home, rid).state().nodes["b"].result.outputs["y"] == 200


def test_yes_approves_the_edit(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FAIL)
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED)
    code, res = cli("rerun", rid, "b", "--follow", "--yes")
    assert code == 0 and _eng(home, rid).state().nodes["b"].result.outputs["y"] == 20


def test_an_edit_of_a_finished_node_asks(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FIXED)
    rid = _start(cli, plan)
    assert _eng(home, rid).state().status == "succeeded"
    _plan(tmp_path, A_OK + B_FIXED.replace("* 10", "* 100"))  # edit b, finished, reached by a rerun of a
    code, res = cli("rerun", rid, "a")
    assert code == 3 and res["data"]["changed"] == ["b"]


def test_rerunning_the_edited_step_itself_applies_the_edit(cli, home, tmp_path):
    """`flower rerun RUN STEP` after editing STEP is the request to re-execute it with the new definition; asking
    for approval added nothing (a rerun without an edit never asks, and earlier attempts stay recorded)."""
    plan = _plan(tmp_path, A_OK + B_FIXED)
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK.replace("2}", "3}") + B_FIXED)
    code, res = cli("rerun", rid, "a", "--follow")
    assert code == 0, res
    st = _eng(home, rid).state()
    assert st.nodes["a"].result.outputs["x"] == 3 and len(st.nodes["a"].attempts) == 2


def test_new_node_added_by_rerun(cli, home, tmp_path):
    plan = _plan(tmp_path, A_OK + B_FIXED)
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED + """\
  - {id: c, kind: shell, run: 'echo "{\\"z\\": $((${b.outputs.y} + 1))}" > "$FLOWER_OUTPUTS"', outputs: {z: integer}}
""")
    code, res = cli("rerun", rid, "c", "--follow")
    assert code == 0, res
    st = _eng(home, rid).state()
    assert st.nodes["c"].result.outputs["z"] == 21
    assert len(st.nodes["a"].attempts) == 1 and len(st.nodes["b"].attempts) == 1  # nothing else re-ran


def test_only_the_rerun_cone_is_picked_up(home, tmp_path, cli):
    plan = _plan(tmp_path, A_OK + B_FIXED + """\
  - {id: other, kind: shell, run: 'echo one'}
""")
    rid = _start(cli, plan)
    _plan(tmp_path, A_OK + B_FIXED.replace("* 10", "* 100") + """\
  - {id: other, kind: shell, run: 'echo two'}
""")
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


def test_policies_and_results_of_older_plans_are_dropped():
    raw = {"flower": 1, "id": "p", "policies": {"edits": "ask"}, "results": ["a"],
           "nodes": [{"id": "a", "kind": "shell", "run": "true"}]}
    plan = normalize(raw)
    assert validate(plan) == [] and "policies" not in plan and "results" not in plan


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
    assert "nodes: []" in plan and "policies" not in plan
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
    # an existing cluster's pacing may change (BUGS #41); where it runs is fixed: reported, not applied
    p.write_text(p.read_text().replace("scheduler: none,", "scheduler: none, max_jobs: 3,"))
    ed = _eng(home, rid).plan_edits("s")
    assert not ed["ignored"] and ed["ops"][0] == {"op": "tune_clusters", "clusters": {"box": {"max_jobs": 3}}}
    p.write_text(p.read_text().replace("scheduler: none,", "scheduler: none, prelude: 'module load x',"))
    ed = _eng(home, rid).plan_edits("s")
    assert ed["ignored"] and "prelude" in ed["ignored"][0]


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


def test_init_keeps_runs_out_of_git(tmp_path):
    """BUGS #57: `flower init` left `.flower/` (run logs, outputs, run inputs such as ssh hosts) to be committed
    in a new project; inside a git repository it is now ignored, once."""
    from flower.devloop import ensure_gitignore
    assert ensure_gitignore(tmp_path) is None and not (tmp_path / ".gitignore").exists()     # not a repository
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.pyc")
    assert ensure_gitignore(tmp_path) == tmp_path / ".gitignore"
    assert (tmp_path / ".gitignore").read_text().splitlines()[-1] == ".flower/"
    assert ensure_gitignore(tmp_path) is None and (tmp_path / ".gitignore").read_text().count(".flower/") == 1
    import subprocess
    repo = tmp_path / "repo"
    (repo / "study").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".flower/\n")
    assert ensure_gitignore(repo / "study") is None             # the repository's own .gitignore already covers it
    assert not (repo / "study" / ".gitignore").exists()


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
    assert say("python3 - <<'EOF'\nopen('a.py','w').write('x')\nEOF", "s6") is None   # a stdin script (editing)
    (cwd / "mymodel.py").write_text("X = 1\n")
    assert say("python3 -c 'import mymodel; print(mymodel.X)'", "s7")            # the study's own code: a check
    assert say("python3 - <<'EOF'\nfrom mymodel import X\nprint(X)\nEOF", "s8")   # same, as a heredoc


def test_hook_names_the_run_being_worked_on(cli, home, tmp_path, monkeypatch):
    """With two active runs the reminder named the most recently touched one, not the one whose directory the
    command ran in."""
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, ra = cli("start", "a", "--id", "a", "--dir", str(tmp_path / "a"))
    _, rb = cli("start", "b", "--id", "b", "--dir", str(tmp_path / "b"))
    a, b = ra["data"]["run_id"], rb["data"]["run_id"]
    root = devloop._project_root(Path.cwd()) or devloop._project_root(Path(os.environ["FLOWER_HOME"]))
    import time
    now = time.time()
    os.utime(root / ".flower" / "runs" / a / "events.jsonl", (now - 60, now - 60))
    os.utime(root / ".flower" / "runs" / b / "events.jsonl", (now, now))   # b touched last
    assert devloop.active_run(root)[0] == b
    assert devloop.active_run(root, tmp_path / "a")[0] == a            # in a's plan directory
    inside = root / ".flower" / "runs" / a / "nodes"
    inside.mkdir(parents=True, exist_ok=True)
    assert devloop.active_run(root, inside)[0] == a                    # in a's run directory
    assert a in (devloop.reminder("python3 analyse.py", inside, "s9") or "")


def test_hook_command_never_fails(cli, home, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    code, text = cli("hook", "bash", as_json=False)
    assert code == 0 and not text.strip()


def test_editing_a_foreach_steps_template_reruns_its_items(cli, home, tmp_path):
    """BUGS #23: a foreach step edited in the plan file (not its items) was re-collected with the old children."""
    plan = _plan(tmp_path, """\
  - {id: f, kind: shell, foreach: [1, 2], run: 'echo "{\\"v\\": ${item}}" > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
""")
    rid = _start(cli, plan)
    assert _eng(home, rid).state().status == "succeeded"
    _plan(tmp_path, """\
  - {id: f, kind: shell, foreach: [1, 2], run: 'echo "{\\"v\\": $((${item} * 10))}" > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
""")
    code, res = cli("rerun", rid, "f", "--follow", "--yes")
    assert code == 0, res
    st = _eng(home, rid).state()
    assert [st.nodes[f"f[{i}]"].result.outputs["v"] for i in (0, 1)] == [10, 20]
    assert len(st.nodes["f[0]"].attempts) == 2


def test_add_a_foreach_step(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    rid = res["data"]["run_id"]
    code, res = cli("add", rid, "sq", "--foreach", "[2, 3]", "--out", "v:integer", "--",
                    'echo "{\\"v\\": $((${item} * ${item}))}" > "$FLOWER_OUTPUTS"')
    assert code == 0, res
    st = _eng(home, rid).state()
    assert [st.nodes[f"sq[{i}]"].result.outputs["v"] for i in (0, 1)] == [4, 9]
    assert "foreach:" in (tmp_path / "x" / "plan.yaml").read_text()


def test_add_with_cpus_and_memory(cli, home, tmp_path, monkeypatch):
    """`flower add --cpus/--mem` write `resources:` (a parallel step needed a hand edit before); they are for
    cluster steps, and a local step is refused with the reason."""
    from types import SimpleNamespace
    from flower.devloop import node_from_args
    keys = ("title needs cluster environment stage_in retrieve setenv timeout_total foreach outs files retry "
            "on_failure trigger ins").split()
    a = SimpleNamespace(id="ed", **{k: None for k in keys}, cpus=8, mem="16G")
    assert node_from_args(a, ["--", "true"])["resources"] == {"cpus_per_task": 8, "mem": "16G"}
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    code, res = cli("add", res["data"]["run_id"], "par", "--cpus", "6", "--", "true")
    assert code != 0 and "cluster" in res["error"]["message"]
    assert "par" not in (tmp_path / "x" / "plan.yaml").read_text()


def test_sync_tunes_a_cluster_without_rerunning(cli, home, tmp_path):
    """BUGS #41: `cpus: 16` added to a running plan's cluster was ignored (a run's clusters were fixed), so three
    8-core jobs started on a machine meant to give the study 16 cores. Pacing settings may now change mid-run
    (`flower sync`, or any rerun/add); where a step runs stays fixed."""
    head = ("flower: 1\nid: dev\nclusters:\n"
            "  box: {transport: local, scheduler: none, max_jobs: 2%s}\nnodes:\n" + A_OK)
    p = tmp_path / "plan.yaml"
    p.write_text(head % "")
    rid = _start(cli, p)
    assert _eng(home, rid).state().nodes["a"].status == "succeeded"
    p.write_text(head % ", cpus: 16, min_poll: 2s")
    code, res = cli("sync", rid)
    assert code == 0, res
    st = _eng(home, rid).state()
    assert st.plan["clusters"]["box"] == {"transport": "local", "scheduler": "none", "max_jobs": 2, "cpus": 16,
                                          "min_poll": "2s"}
    assert len(st.nodes["a"].attempts) == 1                  # nothing re-ran
    p.write_text(head.replace("transport: local", "transport: ssh, host: elsewhere") % ", cpus: 16, min_poll: 2s")
    code, res = cli("sync", rid)
    assert code == 0 and "only cpus, max_jobs, min_poll may change" in res["message"], res
    assert _eng(home, rid).state().plan["clusters"]["box"]["transport"] == "local"
    code, res = cli("sync", rid)
    assert "already follows" in res["message"] or "note:" in res["message"]


def test_a_newer_plan_file_supersedes_an_older_waiting_edit(cli, home, tmp_path):
    """BUGS #58: an edit of finished work waits for approval; when the file was edited again and synced, the
    first proposal stayed open and parked the finished run. Each sync now withdraws older snapshots of the file."""
    head = "flower: 1\nid: dev\nnodes:\n"
    p = tmp_path / "plan.yaml"
    p.write_text(head + A_OK)
    rid = _start(cli, p)
    p.write_text(head + A_OK.replace("2}", "3}"))
    code, res = cli("sync", rid)
    first = res["data"]["amendment_id"]
    assert code == 3
    p.write_text(head + A_OK.replace("2}", "4}"))
    code, res = cli("sync", rid)
    second = res["data"]["amendment_id"]
    assert code == 3 and second != first
    st = _eng(home, rid).state()
    assert st.amendments[first].status == "rejected" and [g.amendment_id for g in st.open_gates()] == [second]
    p.write_text(head + A_OK)                                 # back to what ran: nothing left to decide
    code, res = cli("sync", rid)
    st = _eng(home, rid).state()
    assert code == 0 and not st.open_gates() and st.status == "succeeded", res


def test_an_added_environment_step_is_not_seen_as_edited(cli, home, tmp_path, monkeypatch):
    """BUGS #42: a step added with `--env` (implicit cluster `local`) carries `stage_in: []`, `retrieve: []`,
    `resources: {}` in the run but not in the plan file, so `flower sync` proposed to re-run it, finished and
    unedited."""
    from flower import envs as envmod
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    proj = tmp_path / "proj"
    d = proj / "envs" / "hello"
    d.mkdir(parents=True)
    (d / "setup.sh").write_text('mkdir -p "$FLOWER_ENV_PREFIX/bin"\n')
    (d / "activate.sh").write_text('export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"\n')
    (d / "check.sh").write_text('[ -d "$FLOWER_ENV_PREFIX/bin" ]\n')
    envmod.freeze(d, by="test")
    _, res = cli("start", "x", "--id", "x", "--dir", str(proj / "x"))
    rid = res["data"]["run_id"]
    code, res = cli("add", rid, "a", "--env", "hello", "--out", "v:integer", "--", 'echo "{\\"v\\": 1}" > "$FLOWER_OUTPUTS"')
    assert code == 0 and _eng(home, rid).state().nodes["a"].status == "succeeded", res
    code, res = cli("sync", rid)
    assert code == 0 and "already follows" in res["message"], res


def test_remote_exec_on_a_run_uses_its_inputs(cli, home, tmp_path, capsys):
    """`flower remote exec --run RUN` takes the cluster as the run has it (rendered with the run's inputs); with
    `--plan` every call had to repeat `-i`/`--inputs` (the ssh host and options of a draft)."""
    p = tmp_path / "plan.yaml"
    p.write_text("flower: 1\nid: dev\ninputs:\n  where: {type: string}\nclusters:\n"
                 "  box: {transport: local, scheduler: none, prelude: 'export WHERE=${inputs.where}'}\nnodes:\n" + A_OK)
    code, res = cli("run", str(p), "--yes", "-i", "where=over-there")
    rid = res["data"]["run_id"]
    capsys.readouterr()
    code, res = cli("remote", "exec", "--run", rid, "--cluster", "box", "--", 'echo "at $WHERE"')
    assert code == 0, res
    assert "at over-there" in capsys.readouterr().out + json.dumps(res)
    code, res = cli("remote", "exec", "--cluster", "box", "--", "true")
    assert code != 0 and "--run" in res["error"]["message"]
    code, res = cli("remote", "exec", "--run", rid, "--cluster", "local", "--", "echo here")   # always there
    assert code == 0, res


def test_rerun_names_edits_it_does_not_apply(cli, home, tmp_path):
    """BUGS #43: two steps were edited in the plan file, `rerun` of one applied only that one; the other kept its
    old definition and ran with it later, silently. The rerun now names the edits outside its reach."""
    two = """\
  - {id: a, kind: shell, run: 'echo "{\\"x\\": 1}" > "$FLOWER_OUTPUTS"', outputs: {x: integer}}
  - {id: b, kind: shell, run: 'echo "{\\"y\\": 1}" > "$FLOWER_OUTPUTS"', outputs: {y: integer}}
"""
    plan = _plan(tmp_path, two)
    rid = _start(cli, plan)
    _plan(tmp_path, two.replace('x\\": 1', 'x\\": 2').replace('y\\": 1', 'y\\": 2'))
    code, res = cli("rerun", rid, "a", "--yes")
    assert code == 0 and "also changes b" in res["message"] and "flower sync" in res["message"], res
    code, res = cli("sync", rid, "--yes")
    assert code == 0 and "changed b" in res["message"], res


def test_logs_show_the_outputs_of_a_failed_attempt(cli, home, tmp_path):
    """A check step that writes its verdict and exits 1 on a mismatch: the verdict was invisible (`flower output`
    shows successful results only, `flower logs` only stdout/stderr)."""
    plan = _plan(tmp_path, """\
  - {id: chk, kind: shell, run: 'echo "{\\"z\\": 130}" > "$FLOWER_OUTPUTS"; exit 1', outputs: {z: number}}
""")
    rid = _start(cli, plan)
    code, res = cli("logs", rid, "chk")
    assert code == 0 and '"z": 130' in res["data"]["text"] and "did not succeed" in res["data"]["text"], res


def test_rerun_keep_state_continues_from_the_checkpoint(cli, home, tmp_path):
    """BUGS #48: a checkpointed step that reached its time limit (not retried by default) could only be re-run
    from scratch: a rerun starts a fresh $FLOWER_STATE_DIR. `--keep-state` continues in the last one."""
    plan = _plan(tmp_path, """\
  - {id: long, kind: shell, outputs: {n: integer},
     run: 'n=$(cat "$FLOWER_STATE_DIR/n" 2>/dev/null || echo 0); echo $((n+1)) > "$FLOWER_STATE_DIR/n"; [ "$n" -ge 1 ] || exit 3; echo "{\\"n\\": $((n+1))}" > "$FLOWER_OUTPUTS"'}
""")
    rid = _start(cli, plan)
    assert _eng(home, rid).state().nodes["long"].status == "failed"        # first part done, then "out of time"
    cli("rerun", rid, "long", "--wait")
    assert _eng(home, rid).state().nodes["long"].status == "failed"        # a plain rerun starts from scratch
    code, res = cli("rerun", rid, "long", "--keep-state", "--wait")
    st = _eng(home, rid).state()
    assert st.nodes["long"].status == "succeeded" and st.nodes["long"].result.outputs["n"] == 2, res


def test_a_timeout_edit_keeps_finished_foreach_items(cli, home, tmp_path):
    """BUGS #49: raising the timeout of a foreach step (to let its last, slow item finish) re-ran every finished
    item, although timeout is not part of what a step does (not in its cache key)."""
    body = """\
  - {id: f, kind: shell, foreach: [1, 2], timeout: {total: TT}, run: 'echo "{\\"v\\": ${item}}" > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
"""
    plan = _plan(tmp_path, body.replace("TT", "1h"))
    rid = _start(cli, plan)
    _plan(tmp_path, body.replace("TT", "24h"))
    code, res = cli("sync", rid, "--yes")
    assert code == 0, res
    _eng(home, rid).drive(until="settled", timeout=30)
    st = _eng(home, rid).state()
    for i in (0, 1):
        assert len(st.nodes[f"f[{i}]"].attempts) == 1 and st.nodes[f"f[{i}]"].status == "succeeded"
        assert st.graph().nodes[f"f[{i}]"]["timeout"]["total"] == "24h"
    assert st.status == "succeeded", st.status
    # the same for a plain step: a finished step takes the new timeout without running again
    one = "  - {id: g, kind: shell, timeout: {total: TT}, run: 'true'}\n"
    plan = _plan(tmp_path, one.replace("TT", "1h"))
    rid = _start(cli, plan)
    _plan(tmp_path, one.replace("TT", "2h"))
    code, res = cli("sync", rid)          # no approval needed: nothing finished is touched
    assert code == 0 and "changed g" in res["message"], res
    st = _eng(home, rid).state()
    assert len(st.nodes["g"].attempts) == 1 and st.graph().nodes["g"]["timeout"]["total"] == "2h"


def test_add_step_inputs_keep_json_types():
    """`--in elements='["Au", "Hg"]'` reached the step as a string (the step iterated its characters)."""
    from flower.devloop import _typed
    assert _typed('["Au", "Hg"]') == ["Au", "Hg"] and _typed("1.5") == 1.5 and _typed("3") == 3
    assert _typed('{"a": 1}') == {"a": 1} and _typed("true") is True
    for v in ("${scan.outputs.items}", "/path/x.json", "mp2-atz", "1.5.2", "[not json"):
        assert _typed(v) == v


def test_items_appended_to_a_running_foreach_start_now(cli, home, tmp_path):
    """BUGS #51: items appended to a foreach list waited until no earlier item was in flight; with items
    running one after another that meant after the whole step."""
    import time
    body = """\
  - {id: f, kind: shell, foreach: ITEMS, run: 'sleep ${item}; echo "{\\"v\\": ${item}}" > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
"""
    plan = _plan(tmp_path, body.replace("ITEMS", "[20]"))
    code, res = cli("run", str(plan), "--yes", "--detach")
    rid = res["data"]["run_id"]
    eng = _eng(home, rid)
    deadline = time.time() + 20
    while time.time() < deadline and eng.state().nodes.get("f[0]") is None:
        eng.tick(); time.sleep(0.2)
    _plan(tmp_path, body.replace("ITEMS", "[20, 0]"))
    code, res = cli("sync", rid)
    assert code == 0, res
    deadline = time.time() + 15     # well inside the first item's 20 s, even on a loaded machine
    while time.time() < deadline and "f[1]" not in eng.state().nodes:
        eng.tick(); time.sleep(0.1)
    st = eng.state()
    assert "f[1]" in st.graph().nodes and st.nodes["f[0]"].status == "running", \
        "the appended item waited for the running one"


def test_a_settings_edit_reaches_pending_items_while_one_runs(cli, home, tmp_path):
    """BUGS #52: lowering a remote step's cores so two items fit the cpu budget did not reach the pending items
    until the running one had finished (the re-expansion waited for every in-flight item)."""
    import time
    head = ("flower: 1\nid: dev\nclusters:\n"
            "  box: {transport: local, scheduler: none, max_jobs: 1, min_poll: 0.2s, remote_root: '%s'}\nnodes:\n"
            % (tmp_path / "remote"))
    body = """\
  - {id: f, kind: shell, cluster: box, foreach: [20, 0, 0], timeout: {total: TT}, run: 'sleep ${item}', outputs: {}}
"""
    p = tmp_path / "plan.yaml"
    p.write_text(head + body.replace("TT", "1h"))
    code, res = cli("run", str(p), "--yes", "--detach")
    rid = res["data"]["run_id"]
    eng = _eng(home, rid)
    deadline = time.time() + 20
    while time.time() < deadline and (eng.state().nodes.get("f[0]") is None
                                      or eng.state().nodes["f[0]"].status != "running"):
        eng.tick(); time.sleep(0.2)
    p.write_text(head + body.replace("TT", "2h"))
    code, res = cli("sync", rid)
    assert code == 0, res
    deadline = time.time() + 15
    while time.time() < deadline and eng.state().graph().nodes["f[1]"]["timeout"]["total"] != "2h":
        eng.tick(); time.sleep(0.1)
    st = eng.state()
    assert st.nodes["f[0]"].status == "running", st.nodes["f[0]"].status
    assert st.graph().nodes["f[1]"]["timeout"]["total"] == "2h", "the pending item kept the old setting"


def test_add_description_and_the_warning_without_one(cli, home, tmp_path, monkeypatch):
    """Every step says what it establishes and how to read its result: `--description` writes it next to the title
    (and into the amendment's rationale); a step added without one gets a warning, never an error."""
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    rid = res["data"]["run_id"]
    code, res = cli("add", rid, "a", "--title", "Probe", "--description", "Checks that the machine answers.", "--",
                    "true")
    assert code == 0 and "no --description" not in res["message"], res
    text = (tmp_path / "x" / "plan.yaml").read_text()
    assert text.index("title: Probe") < text.index("description: Checks that the machine answers.") < text.index("run:")
    st = _eng(home, rid).state()
    assert any("Checks that the machine answers." in (am.rationale or "") for am in st.amendments.values())
    code, res = cli("add", rid, "b", "--", "true")
    assert code == 0 and "warning: step b has no --description" in res["message"], res
    from flower.plan import warnings
    assert warnings(_eng(home, rid).state().plan) == [
        "step 'b' has no description (what it establishes, how to read its result)"]


def test_a_description_edit_of_an_environment_step_applies_without_rerunning(cli, home, tmp_path, monkeypatch):
    """The run's copy of a step with an `environment:` (implicit cluster `local`) carries empty stage_in /
    retrieve / resources; describing it in the plan file must still not re-run it."""
    from flower import envs as envmod
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    proj = tmp_path / "proj"
    d = proj / "envs" / "hello"
    d.mkdir(parents=True)
    (d / "setup.sh").write_text('mkdir -p "$FLOWER_ENV_PREFIX/bin"\n')
    (d / "activate.sh").write_text('export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"\n')
    (d / "check.sh").write_text('[ -d "$FLOWER_ENV_PREFIX/bin" ]\n')
    envmod.freeze(d, by="test")
    _, res = cli("start", "x", "--id", "x", "--dir", str(proj / "x"))
    rid = res["data"]["run_id"]
    code, res = cli("add", rid, "a", "--env", "hello", "--", "true")
    p = proj / "x" / "plan.yaml"
    p.write_text(p.read_text().replace("    environment: hello\n", "    description: Says hello.\n    environment: hello\n"))
    code, res = cli("sync", rid)
    assert code == 0 and "plan edits applied" in res["message"], res
    st = _eng(home, rid).state()
    assert st.graph().nodes["a"]["description"] == "Says hello." and len(st.nodes["a"].attempts) == 1


def test_a_description_edit_applies_without_rerunning(cli, home, tmp_path):
    """Editing a finished step's description (or a foreach step's) is applied to the run, items included, and
    nothing runs again."""
    body = """\
  - {id: f, kind: shell, foreach: [1, 2], description: DESC, run: 'echo "{\\"v\\": ${item}}" > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
  - {id: g, kind: shell, needs: [f], description: DESC, run: 'true'}
"""
    plan = _plan(tmp_path, body.replace("DESC", "old words"))
    rid = _start(cli, plan)
    _plan(tmp_path, body.replace("DESC", "Squares; read v."))
    code, res = cli("sync", rid)          # no approval: nothing finished is re-run
    assert code == 0 and "plan edits applied" in res["message"], res
    _eng(home, rid).drive(until="settled", timeout=30)
    st = _eng(home, rid).state()
    g = st.graph().nodes
    assert g["f"]["description"] == g["f[0]"]["description"] == g["g"]["description"] == "Squares; read v."
    assert all(len(st.nodes[n].attempts) == 1 for n in ("f[0]", "f[1]", "g"))
    assert st.status == "succeeded"


def test_export_a_protocol_and_reproduce_it(cli, home, tmp_path):
    """A finished run as a reproducibility protocol: the steps behind a result (a side step drops out), the values
    to expect, and a re-run that compares clean against them; a plan file that no longer matches is refused."""
    body = """\
  - {id: a, kind: shell, description: Makes x., run: 'echo "{\\"x\\": 2}" > "$FLOWER_OUTPUTS"', outputs: {x: integer}}
  - {id: b, kind: shell, description: Doubles x., run: 'echo "{\\"y\\": $((${a.outputs.x} * 2))}" > "$FLOWER_OUTPUTS"', outputs: {y: integer}}
  - {id: c, kind: shell, needs: [a], description: A look on the side., run: 'true'}
"""
    plan = _plan(tmp_path, body)
    rid = _start(cli, plan)
    code, res = cli("export", rid, "b")
    assert code == 0 and res["data"]["steps"] == ["a", "b"], res
    import yaml
    proto = yaml.safe_load((tmp_path / "protocol.yaml").read_text())
    assert [n["id"] for n in proto["nodes"]] == ["a", "b"] and proto["nodes"][1]["description"] == "Doubles x."
    exp = json.loads((tmp_path / "expected.json").read_text())
    assert exp["steps"] == {"a": {"x": 2}, "b": {"y": 4}}
    assert "Doubles x." in (tmp_path / "PROTOCOL.md").read_text()
    rid2 = _start(cli, tmp_path / "protocol.yaml")
    code, res = cli("compare", rid2, str(tmp_path / "expected.json"))
    assert code == 0 and res["data"]["same"] == 2 and not res["data"]["differ"], res
    (tmp_path / "expected.json").write_text(json.dumps({"steps": {"a": {"x": 2}, "b": {"y": 5}}}))
    code, res = cli("compare", rid2, str(tmp_path / "expected.json"))
    assert code == 1 and res["data"]["differ"][0]["node"] == "b"
    _plan(tmp_path, body.replace("* 2", "* 3"))
    code, res = cli("export", rid, "b")
    assert code != 0 and res["error"]["code"] == "plan_drift", res


def test_tune_clusters_amendment_refuses_placement_keys():
    from flower.plan import apply_amendment, PlanInvalid
    plan = {"flower": 1, "id": "x", "clusters": {"box": {"transport": "local", "cpus": 4}}, "nodes": []}
    new, _ = apply_amendment(plan, [{"op": "tune_clusters", "clusters": {"box": {"cpus": 8, "max_jobs": None}}}], {})
    assert new["clusters"]["box"] == {"transport": "local", "cpus": 8}
    for bad in ({"box": {"host": "h"}}, {"nope": {"cpus": 2}}):
        with pytest.raises(PlanInvalid):
            apply_amendment(plan, [{"op": "tune_clusters", "clusters": bad}], {})


def test_an_edited_script_is_not_served_from_cache(cli, home, tmp_path):
    """BUGS #25: a shell step's cache key covered its command, not the script the command runs."""
    (tmp_path / "s.py").write_text('import json, os\nprint(json.dumps({"v": 1}))\n')
    plan = _plan(tmp_path, """\
  - {id: a, kind: shell, run: 'echo "{\\"x\\": 1}" > "$FLOWER_OUTPUTS"', outputs: {x: integer}}
  - {id: b, kind: shell, needs: [a], run: 'python3 ${plan.dir}/s.py > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
""")
    rid = _start(cli, plan)
    assert _eng(home, rid).state().nodes["b"].result.outputs["v"] == 1
    (tmp_path / "s.py").write_text('import json, os\nprint(json.dumps({"v": 2}))\n')
    code, res = cli("rerun", rid, "a", "--follow")       # b is downstream: stale, not forced
    _eng(home, rid).drive(until="settled", timeout=60)
    st = _eng(home, rid).state()
    assert st.nodes["b"].result.outputs["v"] == 2, "b was reused although its script changed"
    # unchanged script: the early cut-off still applies (no re-execution)
    n = len(st.nodes["b"].attempts)
    cli("rerun", rid, "a", "--follow")
    _eng(home, rid).drive(until="settled", timeout=60)
    st = _eng(home, rid).state()
    assert st.nodes["b"].last.reused_from if hasattr(st.nodes["b"].last, "reused_from") else True
    assert st.nodes["b"].result.outputs["v"] == 2 and len(st.nodes["b"].attempts) == n + 1


def test_rerun_after_an_edit_still_reruns_unchanged_items(cli, home, tmp_path):
    """BUGS #26: when the rerun also picked up a plan edit, the step was 'already pending' and its unchanged
    foreach items were re-collected from cache instead of run again."""
    (tmp_path / "s.py").write_text('import json, sys\nprint(json.dumps({"v": int(sys.argv[1])}))\n')
    body = """\
  - {id: f, kind: shell, foreach: ITEMS, run: 'python3 ${plan.dir}/s.py ${item} > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
"""
    plan = _plan(tmp_path, body.replace("ITEMS", "[1, 2]"))
    rid = _start(cli, plan)
    (tmp_path / "s.py").write_text('import json, sys\nprint(json.dumps({"v": 10 * int(sys.argv[1])}))\n')
    _plan(tmp_path, body.replace("ITEMS", "[1, 2, 3]"))
    code, res = cli("rerun", rid, "f", "--follow", "--yes")
    assert code == 0, res
    st = _eng(home, rid).state()
    assert [st.nodes[f"f[{i}]"].result.outputs["v"] for i in range(3)] == [10, 20, 30]


def test_add_with_step_inputs(cli, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    _, res = cli("start", "x", "--id", "x", "--dir", str(tmp_path / "x"))
    rid = res["data"]["run_id"]
    cli("add", rid, "sq", "--foreach", "[2, 3]", "--out", "v:integer", "--",
        'echo "{\\"v\\": $((${item} * ${item}))}" > "$FLOWER_OUTPUTS"')
    code, res = cli("add", rid, "total", "--in", "vals=${sq.outputs.items}", "--out", "s:integer", "--",
                    'python3 -c "import json, os; d = json.load(open(os.environ[\'FLOWER_INPUTS\'])); '
                    'print(json.dumps({\'s\': sum(x[\'v\'] for x in d[\'vals\'])}))" > "$FLOWER_OUTPUTS"')
    assert code == 0, res
    assert _eng(home, rid).state().nodes["total"].result.outputs["s"] == 13


def test_editing_tmpdir_keeps_finished_results(cli, home, tmp_path):
    """Where temporary files go does not change results: `tmpdir` is outside the cache key, so a plan edit that
    only moves them (applied with `rerun --cached`) keeps what already finished."""
    import tempfile
    counter = Path(tempfile.mkdtemp()) / "count"   # outside the plan directory (files there are part of the key)
    counter.write_text("")
    run = 'echo x >> ' + str(counter) + '; echo "{\\"v\\": 1}" > "$FLOWER_OUTPUTS"'
    plan = _plan(tmp_path, f"  - {{id: a, kind: shell, run: '{run}', outputs: {{v: integer}}}}\n")
    rid = _start(cli, plan)
    assert counter.read_text().count("x") == 1
    _plan(tmp_path, f"  - {{id: a, kind: shell, tmpdir: job, run: '{run}', outputs: {{v: integer}}}}\n")
    code, res = cli("rerun", rid, "a", "--cached", "--follow")
    assert code == 0, res
    st = _eng(home, rid).state()
    assert st.nodes["a"].result.outputs["v"] == 1
    assert counter.read_text().count("x") == 1, "a was executed again for a tmpdir edit"


def test_partial_results_of_a_running_foreach(cli, home, tmp_path):
    """`${step.partial}` gives a foreach's finished items without waiting for the rest (a preview of a campaign)."""
    plan = _plan(tmp_path, """\
  - {id: f, kind: shell, foreach: [1, 2, 30], run: 'sleep $(( ${item} / 10 )); echo "{\\"v\\": ${item}}" > "$FLOWER_OUTPUTS"', outputs: {v: integer}}
  - {id: peek, kind: shell, inputs: {sofar: "${f.partial}"}, run: 'python3 -c "import json, os; d = json.load(open(os.environ[\\"FLOWER_INPUTS\\"]))[\\"sofar\\"]; print(json.dumps({\\"n\\": sum(x is not None for x in d)}))" > "$FLOWER_OUTPUTS"', outputs: {n: integer}}
""")
    code, res = cli("run", str(plan), "--yes", "--detach")
    rid = list_runs(Path(os.environ["FLOWER_HOME"]))[-1]
    eng = _eng(home, rid)
    import time
    t0 = time.time()
    while time.time() - t0 < 20:
        eng.tick()
        st = eng.state()
        if st.nodes.get("peek") and st.nodes["peek"].status == "succeeded":
            break
        time.sleep(0.2)
    st = eng.state()
    assert st.nodes["peek"].status == "succeeded", "peek waited for the whole foreach"
    assert st.nodes["peek"].result.outputs["n"] < 3          # it ran before the 3 s item finished
    assert "f" not in st.graph().needs["peek"]


def test_compare_two_runs(cli, home, tmp_path):
    """`flower compare`: two runs of one workflow give the same results (exit 0), a changed input does not (exit 1);
    a run can also be named by its directory (another project, e.g. a fresh clone)."""
    p = tmp_path / "plan.yaml"
    p.write_text("""flower: 1
id: cmp
inputs: {n: {type: integer, default: 2}}
nodes:
  - {id: a, kind: shell, run: 'echo "{\\"x\\": ${inputs.n}}" > "$FLOWER_OUTPUTS"', outputs: {x: integer}}
""")
    cli("run", str(p), "--yes")
    cli("run", str(p), "--yes")
    cli("run", str(p), "--yes", "-i", "n=3")
    r1, r2, r3 = list_runs(Path(os.environ["FLOWER_HOME"]))[-3:]
    code, res = cli("compare", r1, r2)
    assert code == 0 and res["data"]["same"] == 1, res
    code, res = cli("compare", r1, str(_eng(home, r3).paths.dir))
    assert code == 1 and res["data"]["differ"][0]["diffs"][0]["key"] == ".x", res


def test_a_rerun_withdraws_its_own_older_waiting_edit(cli, home, tmp_path):
    """The proposal a rerun left waiting (before reruns applied their own step's edits) must not park the run
    once a newer rerun of that step has applied the file; an unrelated waiting edit stays."""
    plan = _plan(tmp_path, A_OK + B_FIXED)
    rid = _start(cli, plan)
    eng = _eng(home, rid)
    eng.propose_amendment([{"op": "replace", "node": "a", "with": {"kind": "shell", "run": "true"},
                            "supersede": True}], "plan file edited (changed a); picked up by `flower rerun`")
    _plan(tmp_path, A_OK.replace("2}", "3}") + B_FIXED)
    eng.propose_amendment(eng.plan_edits("a")["ops"], "plan file edited (changed a); picked up by `flower rerun`")
    code, res = cli("rerun", rid, "a", "--follow")       # the same edit is waiting, and now applies at once
    assert code == 0, res
    st = _eng(home, rid).state()
    assert not st.open_gates() and st.nodes["a"].result.outputs["x"] == 3


def test_add_stage_in_path_typed_from_the_current_directory(cli, home, tmp_path, monkeypatch):
    """`--stage-in` paths are relative to the plan's directory; one typed from the current directory (the study's
    parent, as in the eam-elastic benchmark) named a file that did not exist there, and the step failed at staging."""
    monkeypatch.setenv("FLOWER_NO_UI", "1")
    monkeypatch.chdir(tmp_path)
    _, res = cli("start", "x", "--id", "x", "--dir", "study")
    rid = res["data"]["run_id"]
    (tmp_path / "study" / "calc.py").write_text("print(1)\n")
    code, res = cli("add", rid, "a", "--cluster", "local", "--stage-in", "study/calc.py", "--no-follow", "--", "true")
    plan = (tmp_path / "study" / "plan.yaml").read_text()
    assert "- calc.py" in plan and "study/calc.py" not in plan, plan
