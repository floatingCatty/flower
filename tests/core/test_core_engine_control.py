"""Control verbs: cancel (node / run), rerun (--downstream, cache reuse, reopen), amendments, waits & signals."""
from __future__ import annotations

import json
import time

import pytest

from core_helpers import drive, events, out_json, pid_alive, tick_until, wait_for
from flower.plan import PlanInvalid
from flower.util import FlowerError


def sh(id_, run, **kw):
    return {"id": id_, "kind": "shell", "run": run, **kw}


def counter(path) -> str:
    return f'C=$(cat "{path}" 2>/dev/null || echo 0); C=$((C+1)); echo $C > "{path}"'


def runner_info(eng, node, attempt=1):
    p = eng.paths.attempt_dir(node, attempt) / "proc" / "runner.json"
    assert wait_for(p.exists, 10), "runner.json never appeared"
    return json.loads(p.read_text())


def wait_running(eng, node, attempt=1):
    tick_until(eng, lambda s: s.nodes[node].status == "running" and s.nodes[node].last.n == attempt)
    return runner_info(eng, node, attempt)


# ------------------------------------------------------------------ cancel

def test_cancel_running_node_kills_process(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 30"), sh("b", "true", needs=["a"])]))
    info = wait_running(eng, "a")
    assert pid_alive(info["child_pid"])
    eng.cancel(node="a", reason="changed my mind", by="human:x")
    assert any(e["payload"].get("phase") == "cancelling" for e in events(eng, "node.progress", "a"))
    st = tick_until(eng, lambda s: s.nodes["a"].status == "cancelled", timeout=10)
    assert st.nodes["a"].last.status == "cancelled"
    assert wait_for(lambda: not pid_alive(info["child_pid"]) and not pid_alive(info["runner_pid"]), 5)
    rep = drive(eng)
    assert eng.state().nodes["b"].status == "skipped"
    assert rep.status == "failed"


def test_cancel_pending_node(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 1"), sh("b", "true", needs=["a"]), sh("c", "true", needs=["b"])]))
    wait_running(eng, "a")
    eng.cancel(node="b")
    st = eng.state()
    assert st.nodes["b"].status == "cancelled" and not st.nodes["b"].attempts
    drive(eng)
    st = eng.state()
    assert st.nodes["a"].status == "succeeded" and st.nodes["c"].status == "skipped"


def test_cancel_errors(mkplan, start):
    eng = start(mkplan([sh("a", "true")]))
    drive(eng)
    with pytest.raises(FlowerError) as ei:
        eng.cancel(node="a")
    assert ei.value.code == "node_finished"
    with pytest.raises(FlowerError) as ei:
        eng.cancel(node="zz")
    assert ei.value.code == "node_not_found"
    with pytest.raises(FlowerError) as ei:
        eng.cancel()
    assert ei.value.code == "run_finished"


def test_cancel_whole_run(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 30"), sh("b", "true", needs=["a"]), {"id": "g", "kind": "gate"},
                        {"id": "w", "kind": "wait", "signal": "go"}]))
    info = wait_running(eng, "a")
    tick_until(eng, lambda s: s.nodes["g"].status == "waiting" and s.nodes["w"].status == "waiting")
    eng.cancel(reason="stop all")
    rep = eng.tick()
    st = eng.state()
    assert rep.status == "cancelled" and st.status == "cancelled"
    assert st.nodes["a"].status == "cancelled" and st.nodes["b"].status == "skipped"
    assert st.nodes["g"].status == "cancelled" and st.nodes["w"].status == "cancelled"
    assert st.open_gates() == []
    assert st.gates["g#a1"].decision == "withdrawn"
    assert wait_for(lambda: not pid_alive(info["child_pid"]), 5)
    # idempotent afterwards
    n = len(eng.journal.read())
    eng.tick()
    assert len(eng.journal.read()) == n


def test_cancel_run_awaiting_approval(mkplan, start):
    eng = start(mkplan([sh("a", "true")]), approve=False)
    eng.cancel(reason="never mind")
    eng.tick()
    assert eng.state().status == "cancelled"


def test_cancel_gate_node_closes_gate(mkplan, start):
    eng = start(mkplan([{"id": "g", "kind": "gate"}, sh("b", "true")]))
    drive(eng)
    eng.cancel(node="g")
    rep = drive(eng)
    st = eng.state()
    assert st.open_gates() == []
    assert rep.status == "failed"


def test_rerun_after_run_cancel_does_not_hang(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 30"), sh("b", "true", needs=["a"])]))
    wait_running(eng, "a")
    eng.cancel()
    eng.tick()
    assert eng.state().status == "cancelled"
    try:
        eng.rerun("a")
    except FlowerError:
        return  # refusing to reopen a cancelled run would also be acceptable
    # either way the run must not be stuck in "running" with nothing running: a is really re-executing
    rep = drive(eng, timeout=2.5)
    st = eng.state()
    assert rep.status in ("succeeded", "failed", "cancelled") or (
        st.nodes["a"].status == "running" and st.nodes["a"].last.n == 2), rep.status
    eng.cancel()
    assert drive(eng, timeout=10).status == "cancelled"


# ------------------------------------------------------------------ rerun

def _chain(mkplan, a_run):
    return mkplan([
        sh("a", a_run),
        sh("b", 'echo "{\\"b\\": ${a.outputs.v}}" > "$FLOWER_OUTPUTS"'),
        sh("c", 'echo "{\\"c\\": ${b.outputs.b}}" > "$FLOWER_OUTPUTS"'),
        sh("x", out_json({"x": 1})),
    ])


def test_rerun_downstream_reuses_unchanged(mkplan, start):
    eng = start(_chain(mkplan, out_json({"v": 7})))
    assert drive(eng).status == "succeeded"
    targets = eng.rerun("a", by="human:r")
    assert targets == ["a", "b", "c"]
    st = eng.state()
    assert st.status == "running"
    assert [st.nodes[n].status for n in "abc"] == ["pending"] * 3
    assert st.nodes["a"].force_next and not st.nodes["b"].force_next
    assert events(eng, "run.reopened")
    assert drive(eng).status == "succeeded"
    st = eng.state()
    a = st.nodes["a"]
    assert len(a.attempts) == 2 and a.result.n == 2 and a.result.reused_from is None
    assert (eng.paths.attempt_dir("a", 2) / "proc" / "exit.json").exists()   # really executed
    for n in "bc":
        r = st.nodes[n].result
        assert r.n == 2 and r.reused_from == "a1", (n, r)
        assert not (eng.paths.attempt_dir(n, 2) / "proc").exists()          # not executed
    assert len(st.nodes["x"].attempts) == 1
    assert st.nodes["c"].result.outputs == {"c": 7}


def test_rerun_downstream_reexecutes_changed(mkplan, start, tmp_path):
    cnt = tmp_path / "a.cnt"
    eng = start(_chain(mkplan, counter(cnt) + '\necho "{\\"v\\": $C}" > "$FLOWER_OUTPUTS"'))
    drive(eng)
    eng.rerun("a")
    assert drive(eng).status == "succeeded"
    st = eng.state()
    for n in "bc":
        assert st.nodes[n].result.reused_from is None
        assert st.nodes[n].result.n == 2
    assert st.nodes["c"].result.outputs == {"c": 2}


def test_rerun_only_and_cached(mkplan, start):
    eng = start(_chain(mkplan, out_json({"v": 1})))
    drive(eng)
    assert eng.rerun("a", downstream=False) == ["a"]
    drive(eng)
    st = eng.state()
    assert len(st.nodes["a"].attempts) == 2 and len(st.nodes["b"].attempts) == 1
    eng.rerun("a", downstream=False, force=False)
    drive(eng)
    a = eng.state().nodes["a"]
    assert a.result.n == 3 and a.result.reused_from == "a2"


def test_rerun_failed_node_recovers_downstream(mkplan, start, tmp_path):
    cnt = tmp_path / "f.cnt"
    eng = start(mkplan([sh("a", counter(cnt) + '\n[ "$C" -ge 2 ] || exit 1\n' + out_json({"v": 3})),
                        sh("b", 'echo "{\\"b\\": ${a.outputs.v}}" > "$FLOWER_OUTPUTS"')]))
    assert drive(eng).status == "failed"
    assert eng.state().nodes["b"].status == "skipped"
    eng.rerun("a")
    assert eng.state().status == "running"
    assert drive(eng).status == "succeeded"
    st = eng.state()
    assert st.nodes["b"].result.outputs == {"b": 3}
    completed = [e["payload"]["status"] for e in events(eng, "run.completed")]
    assert completed == ["failed", "succeeded"]


def test_rerun_errors(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 30"), sh("b", "true", needs=["a"])]))
    wait_running(eng, "a")
    with pytest.raises(FlowerError) as ei:
        eng.rerun("a")
    assert ei.value.code == "node_running"
    with pytest.raises(FlowerError) as ei:
        eng.rerun("nope")
    assert ei.value.code == "node_not_found"


def test_rerun_refuses_when_downstream_active(mkplan, start):
    eng = start(mkplan([sh("a", "true"), sh("b", "sleep 30", needs=["a"])]))
    wait_running(eng, "b")
    with pytest.raises(FlowerError) as ei:
        eng.rerun("a")
    assert "b" in ei.value.message


# ------------------------------------------------------------------ amendments

def test_amend_add_needs_approval_then_runs(mkplan, start):
    eng = start(mkplan([sh("a", out_json({"v": 5})), {"id": "g", "kind": "gate", "needs": ["a"]}]))
    drive(eng)
    aid = eng.propose_amendment([{"op": "add", "nodes": [sh("extra", 'echo "${a.outputs.v}"')]}], "need extra",
                                by="human:dev")
    st = eng.state()
    assert st.amendments[aid].status == "proposed"
    assert f"amend-{aid}" in [g.id for g in st.open_gates()]
    assert "extra" not in st.graph().nodes
    eng.answer(f"amend-{aid}", "approve", by="human:pi")
    drive(eng)
    st = eng.state()
    assert st.generation == 1 and st.amendments[aid].status == "approved"
    assert st.nodes["extra"].result.summary == "5"
    assert st.generations[1]["by"] == "human:pi"
    assert eng.paths.current_plan_file.exists() and "extra" in eng.paths.current_plan_file.read_text()


def test_amend_rejected_by_gate(mkplan, start):
    eng = start(mkplan([{"id": "g", "kind": "gate"}]))
    drive(eng)
    aid = eng.propose_amendment([{"op": "add", "nodes": [sh("extra", "true")]}], "maybe")
    eng.answer(f"amend-{aid}", "reject", text="no thanks")
    eng.tick()
    st = eng.state()
    assert st.amendments[aid].status == "rejected" and st.generation == 0
    assert "extra" not in st.graph().nodes


def test_amend_detour_inserts_before_pending_child(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 0.5"), sh("b", "true", needs=["a"])]))
    wait_running(eng, "a")
    eng.propose_amendment([{"op": "detour", "after": "a", "nodes": [sh("d", "true")]}], "check first",
                          auto_approve=True)
    assert drive(eng).status == "succeeded"
    seq = {(e["eventType"], e.get("nodeId")): e["seq"] for e in eng.journal.read()}
    assert seq[("node.started", "d")] > seq[("node.succeeded", "a")]
    assert seq[("node.started", "b")] > seq[("node.succeeded", "d")]


def test_amend_replace_pending_and_stop(mkplan, start, tmp_path):
    eng = start(mkplan([sh("a", "sleep 0.5"), sh("b", "echo old", needs=["a"]), sh("c", "true", needs=["a"]),
                        sh("d", "true", needs=["c"])]))
    wait_running(eng, "a")
    eng.propose_amendment([{"op": "replace", "node": "b", "with": {"kind": "shell", "run": "echo new", "needs": ["a"]}},
                           {"op": "stop", "node": "c"}], "swap", auto_approve=True)
    drive(eng)
    st = eng.state()
    assert st.nodes["b"].result.summary == "new"
    assert st.nodes["c"].status == "skipped" and "stopped by amendment" in st.nodes["c"].skipped_reason
    assert st.nodes["d"].status == "skipped"


def test_amend_illegal_edit_of_finished_node_rejected(mkplan, start):
    eng = start(mkplan([sh("a", "true"), {"id": "g", "kind": "gate", "needs": ["a"]}]))
    drive(eng)
    for ops in ([{"op": "replace", "node": "a", "with": {"kind": "shell", "run": "false"}}],
                [{"op": "drop", "node": "a"}],
                [{"op": "set_needs", "node": "a", "needs": ["g"]}]):
        with pytest.raises(PlanInvalid):
            eng.propose_amendment(ops, "rewrite history")
    st = eng.state()
    assert st.generation == 0
    assert len(st.amendments) == 3 and all(a.status == "rejected" for a in st.amendments.values())
    assert all(e["payload"].get("issues") for e in events(eng, "plan.amendment.rejected"))


def test_amend_supersede_finished_node_reopens_and_reuses(mkplan, start):
    eng = start(mkplan([sh("a", out_json({"v": 1})), sh("b", 'echo "{\\"b\\": ${a.outputs.v}}" > "$FLOWER_OUTPUTS"'),
                        sh("c", out_json({"c": 0}))]))
    assert drive(eng).status == "succeeded"
    eng.propose_amendment([{"op": "replace", "node": "a", "supersede": True,
                            "with": {"kind": "shell", "run": "echo different-script; " + out_json({"v": 1})}}],
                          "new script, same result", auto_approve=True)
    st = eng.state()
    assert st.status == "running" and st.generation == 1
    assert st.nodes["a"].status == "pending" and st.nodes["b"].status == "pending"
    assert st.nodes["c"].status == "succeeded"
    assert drive(eng).status == "succeeded"
    st = eng.state()
    assert st.nodes["a"].result.n == 2 and st.nodes["a"].result.reused_from is None   # definition changed
    assert st.nodes["b"].result.reused_from == "a1"                                   # early cut-off
    assert len(st.nodes["c"].attempts) == 1


def test_amend_before_approval_refused(mkplan, start):
    eng = start(mkplan([sh("a", "true")]), approve=False)
    with pytest.raises(FlowerError) as ei:
        eng.propose_amendment([{"op": "add", "nodes": [sh("x", "true")]}], "early")
    assert ei.value.code == "run_not_started"


def test_amend_that_no_longer_applies_is_rejected_at_approval(mkplan, start):
    eng = start(mkplan([{"id": "g", "kind": "gate"}, sh("b", "true", needs=["g"])]))
    drive(eng)
    aid = eng.propose_amendment([{"op": "replace", "node": "b", "with": {"kind": "shell", "run": "echo v2",
                                                                         "needs": ["g"]}}], "tweak b")
    eng.answer("g", "approve")
    drive(eng)  # b runs and finishes before the amendment is approved
    assert eng.state().nodes["b"].status == "succeeded"
    eng.answer(f"amend-{aid}", "approve")
    eng.tick()
    am = eng.state().amendments[aid]
    assert am.status == "rejected" and "no longer applies" in am.effects["reason"]


def test_amend_on_finished_run_reopens(mkplan, start):
    eng = start(mkplan([sh("a", "true")]))
    drive(eng)
    eng.propose_amendment([{"op": "add", "nodes": [sh("more", "echo more", needs=["a"])]}], "grow", auto_approve=True)
    assert eng.state().status == "running"
    assert drive(eng).status == "succeeded"
    assert eng.state().nodes["more"].result.summary == "more"
    assert [e["payload"]["status"] for e in events(eng, "run.completed")] == ["succeeded", "succeeded"]


def test_supersede_running_node_does_not_orphan_process(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 30")]))
    info = wait_running(eng, "a")
    try:
        eng.propose_amendment([{"op": "replace", "node": "a", "supersede": True,
                                "with": {"kind": "shell", "run": "true"}}], "replace running", auto_approve=True)
    except PlanInvalid:
        return  # refusing is fine
    drive(eng, timeout=10)
    assert not pid_alive(info["child_pid"]), "attempt 1 process still running after its node was superseded"


# ------------------------------------------------------------------ waits & signals

def test_wait_signal(mkplan, start):
    eng = start(mkplan([{"id": "w", "kind": "wait", "signal": "go"},
                        sh("after", 'echo "${w.outputs.data.x}"')]))
    rep = drive(eng)
    assert rep.status == "parked" and rep.next_poll_s == 0
    assert any("waiting for signal" in x for x in rep.waiting_on)
    eng.signal("other", {"x": 0})
    drive(eng)
    assert eng.state().nodes["w"].status == "waiting"
    eng.signal("go", {"x": 42}, by="human:s")
    assert drive(eng).status == "succeeded"
    st = eng.state()
    outs = dict(st.nodes["w"].result.outputs)
    assert isinstance(outs.pop("signal_seq"), int)
    assert outs == {"signal": "go", "data": {"x": 42}, "by": "human:s", "expired": False}
    assert st.nodes["after"].result.summary == "42"
    # parked -> running transition was journalled
    types = [e["eventType"] for e in eng.journal.read() if e["eventType"] in ("run.parked", "run.started")]
    assert "run.started" in types[types.index("run.parked"):]


def test_signal_sent_before_wait_is_armed_is_not_consumed(mkplan, start):
    """Signals are sticky (fixed): one sent before its wait node is armed is delivered when it arms."""
    eng = start(mkplan([sh("a", "sleep 0.3"), {"id": "w", "kind": "wait", "signal": "go", "needs": ["a"]}]))
    eng.tick()
    eng.signal("go", {"early": True})
    drive(eng)
    st = eng.state()
    assert st.nodes["w"].status == "succeeded" and st.nodes["w"].result.outputs["data"] == {"early": True}


def test_signal_via_file_dropbox(mkplan, start):
    eng = start(mkplan([{"id": "w", "kind": "wait", "signal": "ready"}]))
    drive(eng)
    (eng.paths.signals / "s1.json").write_text(json.dumps({"name": "ready", "data": [1, 2], "by": "cron"}))
    assert drive(eng).status == "succeeded"
    assert eng.state().nodes["w"].result.outputs["data"] == [1, 2]
    assert (eng.paths.signals / "consumed" / "s1.json").exists()
    assert len(events(eng, "signal.received")) == 1


def test_wait_timer(mkplan, start):
    eng = start(mkplan([{"id": "t", "kind": "wait", "timer": "1s"}, sh("after", "true", needs=["t"])]))
    t0 = time.time()
    eng.tick()
    rep = eng.tick()
    assert rep.status == "parked" and rep.next_poll_s > 0      # timed park keeps a driver alive
    rep = drive(eng, timeout=10)
    assert rep.status == "succeeded"
    assert time.time() - t0 >= 0.9
    assert eng.state().nodes["t"].result.outputs["timer"] is True


def test_wait_deadline_expiry_is_branchable(mkplan, start):
    eng = start(mkplan([{"id": "w", "kind": "wait", "signal": "data", "deadline": "1s"},
                        sh("late", "true", when="${w.outputs.expired}"),
                        sh("ontime", "true", when="not ${w.outputs.expired}"),
                        sh("join", "true", needs=["late", "ontime"], trigger="all_done")]))
    drive(eng, timeout=10)
    st = eng.state()
    assert st.nodes["w"].status == "succeeded" and st.nodes["w"].result.outputs["expired"] is True
    assert len(events(eng, "wait.expired", "w")) == 1
    assert st.nodes["late"].status == "succeeded" and st.nodes["ontime"].status == "skipped"
    assert st.nodes["join"].status == "succeeded"


def test_engine_drive_keeps_alive_for_timers(mkplan, start):
    eng = start(mkplan([{"id": "t", "kind": "wait", "timer": "0.8s"}]))
    rep = eng.drive(timeout=10, max_sleep=0.3)
    assert rep.status == "succeeded"


def test_rerun_reexecutes_when_upstream_file_changes(mkplan, start, tmp_path):
    cnt = tmp_path / "file.cnt"
    eng = start(mkplan([sh("a", counter(cnt) + '\necho "content $C" > data.txt', files={"data": "data.txt"}),
                        sh("b", 'cat "${a.files.data}"')]))
    drive(eng)
    assert eng.state().nodes["b"].result.summary == "content 1"
    eng.rerun("a")
    drive(eng)
    b = eng.state().nodes["b"].result
    assert b.reused_from is None and b.summary == "content 2"


def test_rerun_upstream_children_wait_for_their_new_items(mkplan, start, tmp_path):
    # regression (benchmark si-dos-fermi-remote, UI rerun of scf): the stale children of a foreach ran first
    # with their *old* item (the previous attempt's output paths), then the collector noticed the item list
    # had changed, superseded them, and they ran a second time
    cnt = tmp_path / "gen.cnt"
    seen = tmp_path / "seen.txt"
    eng = start(mkplan([
        sh("gen", counter(cnt) + '\necho "{\\"items\\": [\\"v$C\\"]}" > "$FLOWER_OUTPUTS"'),
        sh("each", f'echo "${{item}}" >> {seen}', foreach="${gen.outputs.items}"),
    ]))
    assert drive(eng).status == "succeeded"
    assert seen.read_text().split() == ["v1"]
    eng.rerun("gen")
    assert drive(eng).status == "succeeded"
    st = eng.state()
    assert seen.read_text().split() == ["v1", "v2"], "a child ran with its stale item before re-expansion"
    assert len([a for a in st.nodes["each[0]"].attempts if a.status == "succeeded" and not a.reused_from]) == 2


def test_rerun_upstream_of_foreach_reexpands(mkplan, start, tmp_path):
    cnt = tmp_path / "items.cnt"
    eng = start(mkplan([
        sh("gen", counter(cnt) + '\nif [ "$C" = 1 ]; then echo "{\\"items\\": [1]}"; else echo "{\\"items\\": [1, 2, 3]}"; '
                                 'fi > "$FLOWER_OUTPUTS"'),
        sh("each", 'echo "{\\"v\\": ${item}}" > "$FLOWER_OUTPUTS"', foreach="${gen.outputs.items}"),
    ]))
    drive(eng)
    assert eng.state().nodes["each"].result.outputs["count"] == 1
    eng.rerun("gen")
    assert drive(eng).status == "succeeded"
    st = eng.state()
    assert st.nodes["gen"].result.outputs["items"] == [1, 2, 3]
    assert st.nodes["each"].result.outputs["count"] == 3


def _counted_foreach(mkplan, tmp_path):
    # one counter file per item: the children run concurrently
    return mkplan([
        sh("gen", out_json({"items": [1, 2]})),
        sh("each", counter(tmp_path / "each${item}.cnt") + '\necho "{\\"v\\": ${item}, \\"c\\": $C}" > "$FLOWER_OUTPUTS"',
           foreach="${gen.outputs.items}"),
        sh("total", 'echo "${each.outputs.items}"'),
    ])


def test_rerun_foreach_parent_reexecutes_its_children(mkplan, start, tmp_path):
    # regression (benchmark si-dos-fermi): `rerun each` only re-collected the old children's results,
    # because children are upstream of their collector, not descendants
    eng = start(_counted_foreach(mkplan, tmp_path))
    assert drive(eng).status == "succeeded"
    targets = eng.rerun("each")
    assert set(targets) == {"each", "each[0]", "each[1]", "total"}
    st = eng.state()
    assert all(st.nodes[c].force_next for c in ("each", "each[0]", "each[1]"))
    assert not st.nodes["total"].force_next
    assert drive(eng).status == "succeeded"
    st = eng.state()
    for c in ("each[0]", "each[1]"):
        r = st.nodes[c].result
        assert r.n == 2 and r.reused_from is None, (c, r)
    assert [i["c"] for i in st.nodes["each"].result.outputs["items"]] == [2, 2]
    assert st.nodes["total"].result.reused_from is None   # its input (the items) changed
    assert len(st.nodes["gen"].attempts) == 1


def test_rerun_foreach_parent_only_skips_downstream(mkplan, start, tmp_path):
    eng = start(_counted_foreach(mkplan, tmp_path))
    drive(eng)
    assert set(eng.rerun("each", downstream=False)) == {"each", "each[0]", "each[1]"}
    assert drive(eng).status == "succeeded"
    st = eng.state()
    assert [i["c"] for i in st.nodes["each"].result.outputs["items"]] == [2, 2]
    assert len(st.nodes["total"].attempts) == 1


def test_rerun_foreach_parent_refuses_while_a_child_runs(mkplan, start):
    eng = start(mkplan([sh("each", "sleep 30", foreach=[1])]))
    tick_until(eng, lambda s: "each[0]" in s.nodes and s.nodes["each[0]"].status == "running")
    with pytest.raises(FlowerError) as ei:
        eng.rerun("each")
    assert ei.value.code == "node_running"
    eng.cancel(by="human:x")


def _calc_plan(mkplan):
    return mkplan([
        sh("a", out_json({"x": 1})),
        {"id": "f", "kind": "function", "call": "calc:f", "args": {"x": "${a.outputs.x}"}},
    ])


def test_function_cache_keys_on_module_source(mkplan, start, src_dir):
    # regression (benchmark si-dos-fermi): editing the called module did not invalidate cached results
    (src_dir / "calc.py").write_text("def f(x):\n    return {'v': x + 1}\n")
    eng = start(_calc_plan(mkplan))
    drive(eng)
    assert eng.state().nodes["f"].result.outputs == {"v": 2}
    eng.rerun("a")
    drive(eng)
    f = eng.state().nodes["f"].result
    assert f.reused_from == "a1" and f.outputs == {"v": 2}            # source unchanged: reused
    (src_dir / "calc.py").write_text("def f(x):\n    return {'v': x + 100}\n")
    eng.rerun("a")
    drive(eng)
    f = eng.state().nodes["f"].result
    assert f.reused_from is None and f.outputs == {"v": 101}          # source changed: re-executed


def test_function_cache_keys_on_whole_local_package(mkplan, start, src_dir):
    pkg = src_dir / "sci"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text("K = 1\n")
    (pkg / "api.py").write_text("from sci.helpers import K\ndef f(x):\n    return {'v': x * K}\n")
    extra = src_dir / "extra"                                          # a node-level pythonpath dir
    extra.mkdir()
    (extra / "other.py").write_text("def g():\n    return {'w': 1}\n")
    eng = start(mkplan([
        sh("a", out_json({"x": 3})),
        {"id": "f", "kind": "function", "call": "sci.api:f", "args": {"x": "${a.outputs.x}"}},
        {"id": "g", "kind": "function", "call": "other:g", "needs": ["a"], "pythonpath": [str(extra)]},
    ]))
    drive(eng)
    assert eng.state().nodes["f"].result.outputs == {"v": 3}
    (pkg / "helpers.py").write_text("K = 10\n")                        # a helper, not the called module
    (extra / "other.py").write_text("def g():\n    return {'w': 22}\n")
    eng.rerun("a")
    drive(eng)
    st = eng.state()
    assert st.nodes["f"].result.reused_from is None and st.nodes["f"].result.outputs == {"v": 30}
    assert st.nodes["g"].result.reused_from is None and st.nodes["g"].result.outputs == {"w": 22}


def test_rerun_resets_retry_budget(mkplan, start):
    eng = start(mkplan([sh("a", "exit 1", retry={"max_attempts": 2, "on": ["exit_nonzero"], "backoff": "0.05s"})]))
    assert drive(eng).status == "failed"
    assert len(eng.state().nodes["a"].attempts) == 2
    eng.rerun("a")
    assert drive(eng).status == "failed"
    ns = eng.state().nodes["a"]
    assert [a.n for a in ns.attempts] == [1, 2, 3, 4]       # a fresh budget of 2 after the rerun
    assert len(events(eng, "node.retry_scheduled", "a")) == 2
