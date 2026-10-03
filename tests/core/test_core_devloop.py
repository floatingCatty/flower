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
