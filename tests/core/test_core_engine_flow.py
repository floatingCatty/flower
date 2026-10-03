"""Engine flows over local shell/function nodes: DAG order, when/trigger/on_failure, foreach, gates,
retry+backoff, timeouts, run status transitions."""
from __future__ import annotations

import json
import textwrap
import time

import pytest

from core_helpers import drive, events, out_json, tick_until
from forgeflow.util import parse_iso


def sh(id_, run, **kw):
    return {"id": id_, "kind": "shell", "run": run, **kw}


def statuses(eng):
    return {k: v.status for k, v in eng.state().nodes.items()}


def counter(path) -> str:
    """Shell snippet: increment the counter file at `path`, value in $C."""
    return f'C=$(cat "{path}" 2>/dev/null || echo 0); C=$((C+1)); echo $C > "{path}"'


# ------------------------------------------------------------------ basic DAGs

def test_diamond_dag_order_and_outputs(mkplan, start):
    plan = mkplan([
        sh("a", out_json({"x": 2})),
        sh("b", 'echo "{\\"y\\": $(( ${a.outputs.x} * 10 ))}" > "$FF_OUTPUTS"'),
        sh("c", 'echo "{\\"z\\": $(( ${a.outputs.x} + 1 ))}" > "$FF_OUTPUTS"'),
        sh("d", 'echo "{\\"sum\\": $(( ${b.outputs.y} + ${c.outputs.z} ))}" > "$FF_OUTPUTS"'),
    ])
    eng = start(plan)
    rep = drive(eng)
    assert rep.status == "succeeded"
    st = eng.state()
    assert st.nodes["d"].result.outputs == {"sum": 23}
    evs = eng.journal.read()
    seq = {(e["eventType"], e.get("nodeId")): e["seq"] for e in evs}
    assert seq[("node.started", "d")] > seq[("node.succeeded", "b")]
    assert seq[("node.started", "d")] > seq[("node.succeeded", "c")]
    assert seq[("node.started", "b")] > seq[("node.succeeded", "a")]
    # the resolved inputs are frozen into node.started
    started_d = next(e for e in evs if e["eventType"] == "node.started" and e.get("nodeId") == "d")
    assert started_d["payload"]["decl_hash"].startswith("sha256:")
    assert "23" not in json.dumps(started_d["payload"]["inputs"])  # inputs empty; resolution is in node.json
    node_json = json.loads((eng.paths.attempt_dir("d", 1) / "node.json").read_text())
    assert "$(( 20 + 3 ))" in node_json["run"]


def test_run_inputs_and_env(mkplan, start, tmp_path):
    plan = mkplan([sh("a", 'echo "{\\"v\\": \\"$FF_IN_NAME-${inputs.n}-$FF_NODE_ID-$FF_ATTEMPT\\"}" > "$FF_OUTPUTS"',
                      inputs={"name": "${inputs.name}"})],
                  inputs={"name": {"type": "string"}, "n": {"type": "integer", "default": 4}})
    eng = start(plan, {"name": "si"})
    assert drive(eng).status == "succeeded"
    assert eng.state().nodes["a"].result.outputs == {"v": "si-4-a-1"}


def test_missing_and_unknown_inputs_rejected(mkplan, start):
    from forgeflow.plan import PlanInvalid
    plan = mkplan([sh("a", "true")], inputs={"name": {"type": "string"}, "n": {"type": "integer"}})
    with pytest.raises(PlanInvalid) as ei:
        start(plan, {"n": "notanint", "zzz": 1})
    codes = sorted(i["code"] for i in ei.value.issues)
    assert codes == ["input_missing", "input_type", "input_unknown"]


def test_summary_is_last_stdout_line(mkplan, start):
    eng = start(mkplan([sh("a", "echo first; echo; echo 'the last line'")]))
    drive(eng)
    assert eng.state().nodes["a"].result.summary == "the last line"


def test_nonzero_exit_fails_run_and_skips_downstream(mkplan, start):
    eng = start(mkplan([sh("a", "echo boom >&2; exit 3"), sh("b", "true", needs=["a"])]))
    rep = drive(eng)
    assert rep.status == "failed"
    st = eng.state()
    err = st.nodes["a"].last.error
    assert err["error_class"] == "exit_nonzero" and "code 3" in err["message"] and "boom" in err["message"]
    assert err["retryable"] is False
    assert st.nodes["b"].status == "skipped"
    assert "upstream a failed" in st.nodes["b"].skipped_reason


def test_bad_outputs_json_is_contract_failure(mkplan, start):
    eng = start(mkplan([sh("a", "echo 'not json' > \"$FF_OUTPUTS\"")]))
    drive(eng)
    assert eng.state().nodes["a"].last.error["error_class"] == "contract"


def test_declared_outputs_and_files_contract(mkplan, start):
    eng = start(mkplan([
        sh("good", out_json({"n": 1}) + "\necho hi > result.txt", outputs={"n": "integer"}, files={"res": "result.txt"}),
        sh("bad", out_json({"n": "one"}), outputs={"n": "integer", "m": "string"}, files={"res": "missing.txt"}),
    ]))
    drive(eng)
    st = eng.state()
    assert st.nodes["good"].status == "succeeded"
    f = st.nodes["good"].result.files["res"]
    assert f["path"].endswith("result.txt") and len(f["sha256"]) == 64 and f["bytes"] == 3
    err = st.nodes["bad"].last.error
    assert err["error_class"] == "contract"
    probs = " ".join(err["details"]["problems"])
    assert "missing.txt" in probs and "'m' is a required property" in probs and "n" in probs


def test_unresolvable_reference_fails_with_template_class(mkplan, start):
    eng = start(mkplan([sh("a", out_json({"x": 1})), sh("b", "echo ${a.outputs.nope}")]))
    drive(eng)
    err = eng.state().nodes["b"].last.error
    assert err["error_class"] == "template" and "nope" in err["message"]


def test_function_node(mkplan, start, src_dir):
    (src_dir / "mymod.py").write_text(textwrap.dedent("""
        def square(x, ctx):
            return {"sq": x * x, "node": ctx["node_id"], "attempt": ctx["attempt"]}
        def scalar(x):
            return x + 1
        def boom(x):
            raise ValueError("bad value %s" % x)
    """))
    eng = start(mkplan([
        {"id": "f", "kind": "function", "call": "mymod:square", "inputs": {"x": 7}},
        {"id": "g", "kind": "function", "call": "mymod:scalar", "args": {"x": "${f.outputs.sq}"}},
        {"id": "h", "kind": "function", "call": "mymod:boom", "inputs": {"x": 1}},
        {"id": "i", "kind": "function", "call": "mymod:square", "inputs": {"x": 1, "unexpected": 2}},
    ]))
    drive(eng)
    st = eng.state()
    assert st.nodes["f"].result.outputs == {"sq": 49, "node": "f", "attempt": 1}
    assert st.nodes["g"].result.outputs == {"result": 50}
    assert st.nodes["h"].status == "failed"
    assert "ValueError: bad value 1" in st.nodes["h"].last.error["message"]
    assert "does not accept" in st.nodes["i"].last.error["message"]


# ------------------------------------------------------------------ when / triggers / on_failure

def test_when_false_skips_node_and_downstream_with_reason(mkplan, start):
    eng = start(mkplan([
        sh("check", out_json({"big": False})),
        sh("heavy", "true", when="${check.outputs.big}"),
        sh("post", "true", needs=["heavy"]),
        sh("light", "true", when="not ${check.outputs.big}"),
    ]))
    drive(eng)
    st = eng.state()
    assert st.nodes["heavy"].status == "skipped"
    assert st.nodes["heavy"].skipped_reason.startswith("condition false")
    assert st.nodes["post"].status == "skipped"
    assert "heavy" in st.nodes["post"].skipped_reason and "condition false" in st.nodes["post"].skipped_reason
    assert st.nodes["light"].status == "succeeded"
    assert not st.nodes["heavy"].attempts  # never launched


def test_when_branch_not_taken_does_not_fail_run(mkplan, start):
    eng = start(mkplan([
        sh("check", out_json({"big": False})),
        sh("heavy", "true", when="${check.outputs.big}"),
        sh("post", "true", needs=["heavy"]),
    ]))
    assert drive(eng).status == "succeeded"


def test_when_join_with_all_done(mkplan, start):
    eng = start(mkplan([
        sh("check", out_json({"big": False})),
        sh("heavy", "true", when="${check.outputs.big}"),
        sh("light", "true", when="not ${check.outputs.big}"),
        sh("join", "true", needs=["heavy", "light"], trigger="all_done"),
    ]))
    rep = drive(eng)
    assert statuses(eng)["join"] == "succeeded"
    assert rep.status == "succeeded"


def test_when_evaluation_error_fails_node(mkplan, start):
    eng = start(mkplan([sh("a", out_json({})), sh("b", "true", when="${a.outputs.missing} > 1")]))
    drive(eng)
    err = eng.state().nodes["b"].last.error
    assert err["error_class"] == "template" and "when" in err["message"]


def test_trigger_all_done_runs_after_failure(mkplan, start):
    eng = start(mkplan([sh("a", "exit 1"), sh("cleanup", "true", needs=["a"], trigger="all_done")]))
    rep = drive(eng)
    assert statuses(eng) == {"a": "failed", "cleanup": "succeeded"}
    assert rep.status == "failed"


def test_trigger_any_success(mkplan, start):
    eng = start(mkplan([sh("a", "exit 1"), sh("b", "true"),
                        sh("any", "true", needs=["a", "b"], trigger="any_success"),
                        sh("none", "true", needs=["a"], trigger="any_success")]))
    drive(eng)
    st = eng.state()
    assert st.nodes["any"].status == "succeeded"
    assert st.nodes["none"].status == "skipped" and st.nodes["none"].skipped_reason == "no upstream succeeded"


def test_on_failure_continue(mkplan, start):
    eng = start(mkplan([sh("a", "exit 1", on_failure="continue"), sh("b", "true", needs=["a"])]))
    rep = drive(eng)
    assert statuses(eng) == {"a": "failed", "b": "succeeded"}
    assert rep.status == "succeeded"


def test_skip_cascades_in_one_drive(mkplan, start):
    eng = start(mkplan([sh("a", "exit 1")] + [sh(f"n{i}", "true", needs=[f"n{i-1}" if i else "a"]) for i in range(6)]))
    drive(eng)
    st = eng.state()
    assert all(st.nodes[f"n{i}"].status == "skipped" for i in range(6))
    assert st.nodes["n5"].skipped_reason.startswith("upstream n4 skipped")


def test_concurrency_limit(mkplan, start, tmp_path):
    log = tmp_path / "conc.log"
    script = f'echo "start $(date +%s.%N)" >> {log}; sleep 0.6; echo "end $(date +%s.%N)" >> {log}'
    eng = start(mkplan([sh(f"n{i}", script) for i in range(4)], defaults={"concurrency": 2}))
    seen = []
    eng.drive(timeout=30, max_sleep=0.2, on_tick=lambda rep: seen.append(len(rep.running)))
    assert eng.state().status == "succeeded"
    assert max(seen) <= 2
    # verify on the wall clock too
    ev = []
    for line in log.read_text().split("\n"):
        if line:
            k, t = line.split()
            ev.append((float(t), 1 if k == "start" else -1))
    cur = peak = 0
    for _, d in sorted(ev):
        cur += d
        peak = max(peak, cur)
    assert peak <= 2


# ------------------------------------------------------------------ foreach

def test_foreach_expansion_and_collector(mkplan, start):
    eng = start(mkplan([
        sh("gen", out_json({"items": [1, 2, 3]})),
        sh("sq", 'echo "{\\"v\\": $(( ${item} * ${item} )), \\"i\\": ${index}}" > "$FF_OUTPUTS"',
           foreach="${gen.outputs.items}"),
        sh("total", 'echo "${sq.outputs.items}" > all.txt; echo "${sq.outputs.count}"'),
    ]))
    rep = drive(eng)
    assert rep.status == "succeeded"
    st = eng.state()
    assert [st.nodes[f"sq[{i}]"].status for i in range(3)] == ["succeeded"] * 3
    outs = st.nodes["sq"].result.outputs
    assert outs["items"] == [{"v": 1, "i": 0}, {"v": 4, "i": 1}, {"v": 9, "i": 2}]
    assert outs["count"] == 3 and outs["succeeded"] == 3 and outs["failed"] == []
    assert st.nodes["total"].result.summary == "3"
    assert st.generation == 1
    am = next(iter(st.amendments.values()))
    assert am.status == "approved" and am.source_node == "sq" and am.decided_by == "policy:foreach"


def test_foreach_literal_list_and_dict(mkplan, start):
    eng = start(mkplan([
        sh("lit", 'echo "{\\"x\\": \\"${item.name}\\"}" > "$FF_OUTPUTS"', foreach=[{"name": "a"}, {"name": "b"}]),
        {"id": "src", "kind": "shell", "run": out_json({"m": {"k1": 1, "k2": 2}})},
        sh("kv", 'echo "{\\"k\\": \\"${item.key}\\", \\"v\\": ${item.value}}" > "$FF_OUTPUTS"',
           foreach="${src.outputs.m}"),
    ]))
    drive(eng)
    st = eng.state()
    assert st.nodes["lit"].result.outputs["items"] == [{"x": "a"}, {"x": "b"}]
    assert st.nodes["kv"].result.outputs["items"] == [{"k": "k1", "v": 1}, {"k": "k2", "v": 2}]


def test_foreach_empty_list_skips(mkplan, start):
    eng = start(mkplan([sh("gen", out_json({"items": []})), sh("each", "true", foreach="${gen.outputs.items}")]))
    drive(eng)
    st = eng.state()
    assert st.nodes["each"].status == "skipped" and "empty" in st.nodes["each"].skipped_reason


def test_foreach_non_list_fails(mkplan, start):
    eng = start(mkplan([sh("gen", out_json({"items": 5})), sh("each", "true", foreach="${gen.outputs.items}")]))
    drive(eng)
    err = eng.state().nodes["each"].last.error
    assert err["error_class"] == "template" and "list" in err["message"]


def test_foreach_with_failing_child(mkplan, start):
    run = 'if [ "${item}" = "2" ]; then exit 4; fi; echo "{\\"v\\": ${item}}" > "$FF_OUTPUTS"'
    eng = start(mkplan([
        sh("strict", run, foreach=[1, 2, 3]),
        sh("lenient", run, foreach=[1, 2, 3], on_failure="continue"),
        sh("after_strict", "true", needs=["strict"]),
        sh("after_lenient", 'echo "${lenient.outputs.failed}"', needs=["lenient"]),
    ]))
    rep = drive(eng)
    st = eng.state()
    # every child runs to completion even though one fails
    assert [st.nodes[f"strict[{i}]"].status for i in range(3)] == ["succeeded", "failed", "succeeded"]
    assert st.nodes["strict"].status == "failed"
    err = st.nodes["strict"].last.error
    assert err["error_class"] == "foreach" and "strict[1]" in err["message"]
    assert err["outputs"]["items"] == [{"v": 1}, None, {"v": 3}]
    assert st.nodes["after_strict"].status == "skipped"
    assert st.nodes["lenient"].status == "succeeded"
    assert st.nodes["lenient"].result.outputs["failed"] == ["lenient[1]"]
    assert st.nodes["after_lenient"].status == "succeeded"
    assert rep.status == "failed"


def _two_foreach(mkplan, start):
    eng = start(mkplan([
        sh("f1", 'echo "{\\"v\\": ${item}}" > "$FF_OUTPUTS"', foreach=[1, 2]),
        sh("f2", 'echo "{\\"v\\": ${item}}" > "$FF_OUTPUTS"', foreach=[10, 20, 30]),
    ]))
    rep = drive(eng)
    return eng, rep


def test_two_foreach_nodes_expanding_in_the_same_tick(mkplan, start):
    eng, rep = _two_foreach(mkplan, start)
    st = eng.state()
    assert rep.status == "succeeded"
    assert [x["v"] for x in st.nodes["f1"].result.outputs["items"]] == [1, 2]
    assert [x["v"] for x in st.nodes["f2"].result.outputs["items"]] == [10, 20, 30]


def test_foreach_generations_never_drop_nodes(mkplan, start):
    eng, rep = _two_foreach(mkplan, start)
    st = eng.state()
    # every approved generation must build on its predecessor (no lost expansion)
    gens = st.generations
    for prev, nxt in zip(gens, gens[1:]):
        prev_ids = {n["id"] for n in prev["plan"]["nodes"]}
        nxt_ids = {n["id"] for n in nxt["plan"]["nodes"]}
        assert prev_ids <= nxt_ids, f"generation {nxt['generation']} dropped {prev_ids - nxt_ids}"


# ------------------------------------------------------------------ gates

def test_gate_approve(mkplan, start):
    eng = start(mkplan([sh("a", out_json({"r": 1})),
                        {"id": "ok", "kind": "gate", "message": "result is ${a.outputs.r}; continue?"},
                        sh("b", 'echo "${ok.outputs.decision} ${ok.outputs.text}"', needs=["ok"])]))
    rep = drive(eng)
    assert rep.status == "parked"
    st = eng.state()
    assert st.status == "parked"
    assert st.nodes["ok"].status == "waiting"
    g = st.open_gates()[0]
    assert g.node == "ok" and g.message == "result is 1; continue?" and g.decisions == ["approve", "reject"]
    req = eng.paths.pending / "ok_a1.request.json"
    assert req.exists()
    eng.answer("ok", "approve", text="looks good", by="human:boss")
    assert not req.exists()
    rep = drive(eng)
    assert rep.status == "succeeded"
    st = eng.state()
    assert st.nodes["ok"].result.outputs == {"decision": "approve", "text": "looks good", "by": "human:boss"}
    assert st.nodes["b"].result.summary == "approve looks good"


def test_gate_answer_validation(mkplan, start):
    from forgeflow.util import ForgeflowError
    eng = start(mkplan([{"id": "g", "kind": "gate"}]))
    drive(eng)
    with pytest.raises(ForgeflowError) as ei:
        eng.answer("g", "maybe")
    assert ei.value.code == "bad_decision"
    with pytest.raises(ForgeflowError) as ei:
        eng.answer("nosuch", "approve")
    assert ei.value.code == "gate_not_found"
    eng.answer("g", "approve")
    with pytest.raises(ForgeflowError) as ei:
        eng.answer("g#a1", "approve")
    assert ei.value.code == "gate_closed"


def test_gate_answer_via_file(mkplan, start):
    eng = start(mkplan([{"id": "g", "kind": "gate"}]))
    drive(eng)
    (eng.paths.pending / "g_a1.answer.json").write_text(json.dumps({"decision": "approve", "by": "web:ui"}))
    assert drive(eng).status == "succeeded"
    assert eng.state().nodes["g"].result.outputs["by"] == "web:ui"
    # a bad answer file is rejected with an error file, not crashing the tick
    eng2 = start(mkplan([{"id": "g", "kind": "gate"}], id="t2"))
    drive(eng2)
    (eng2.paths.pending / "g_a1.answer.json").write_text(json.dumps({"decision": "nope"}))
    eng2.tick()
    assert (eng2.paths.pending / "g_a1.answer.rejected").exists()
    assert json.loads((eng2.paths.pending / "g_a1.answer.error.json").read_text())["code"] == "bad_decision"
    assert eng2.state().nodes["g"].status == "waiting"


def test_gate_reject_without_on_reject_fails(mkplan, start):
    eng = start(mkplan([{"id": "g", "kind": "gate"}, sh("b", "true", needs=["g"])]))
    drive(eng)
    eng.answer("g", "reject", text="no")
    rep = drive(eng)
    st = eng.state()
    assert st.nodes["g"].status == "failed" and st.nodes["g"].last.error["error_class"] == "rejected"
    assert st.nodes["b"].status == "skipped"
    assert rep.status == "failed"


def _loop_plan(mkplan, tmp_path, max_attempts=2, deterministic=False):
    cnt = tmp_path / "draft.cnt"
    body = "echo draft" if deterministic else counter(cnt) + '\necho "{\\"version\\": $C}" > "$FF_OUTPUTS"'
    return mkplan([
        sh("draft", body),
        {"id": "review", "kind": "gate", "message": "review draft", "needs": ["draft"],
         "on_reject": {"rerun": ["draft"], "max_attempts": max_attempts}},
        sh("publish", 'echo "published after ${review.outputs.decision}: ${review.feedback}"', needs=["review"]),
    ])


def test_gate_reject_loop_reruns_target_then_approve(mkplan, start, tmp_path):
    eng = start(_loop_plan(mkplan, tmp_path))
    assert drive(eng).status == "parked"
    eng.answer("review", "reject", text="needs more detail")
    rep = drive(eng)
    assert rep.status == "parked"
    st = eng.state()
    assert len(st.nodes["draft"].attempts) == 2
    assert st.nodes["draft"].result.outputs == {"version": 2}
    assert st.nodes["review"].status == "waiting"
    assert st.nodes["review"].rerun_count == 1
    assert [g.id for g in st.open_gates()] == ["review#a2"]
    # feedback of the previous round is referenceable while the gate is pending again
    ctx = st.template_context()
    assert ctx["nodes"]["review"]["feedback"] == "needs more detail"
    eng.answer("review", "approve", text="ship it")
    assert drive(eng).status == "succeeded"
    st = eng.state()
    assert st.nodes["publish"].result.summary == "published after approve: ship it"
    assert st.nodes["publish"].status == "succeeded"


def test_gate_reject_loop_exhausted_fails(mkplan, start, tmp_path):
    eng = start(_loop_plan(mkplan, tmp_path, max_attempts=1))
    drive(eng)
    eng.answer("review", "reject", text="r1")
    drive(eng)
    assert len(eng.state().nodes["draft"].attempts) == 2
    eng.answer("review", "reject", text="r2")
    rep = drive(eng)
    st = eng.state()
    assert st.nodes["review"].status == "failed"
    assert "rework limit 1 reached" in st.nodes["review"].last.error["message"]
    assert st.nodes["publish"].status == "skipped"
    assert rep.status == "failed"
    assert len(st.nodes["draft"].attempts) == 2


def test_gate_reject_loop_reasks_even_if_rework_is_identical(mkplan, start, tmp_path):
    eng = start(_loop_plan(mkplan, tmp_path, deterministic=True))
    drive(eng)
    eng.answer("review", "reject", text="try again")
    rep = drive(eng)
    st = eng.state()
    assert st.nodes["publish"].status != "succeeded", "publish ran although the only review answer was 'reject'"
    assert rep.status == "parked" and st.nodes["review"].status == "waiting"


def test_gate_feedback_reaches_rerun_target(mkplan, start, tmp_path):
    """The rework loop: the re-run node sees the reviewer's text as ${feedback} (empty on the first pass)."""
    log = tmp_path / "feedback.log"
    eng = start(mkplan([
        sh("draft", f'echo "fb=[${{feedback}}]" >> "{log}"; echo "{{\\"n\\": $(wc -l < "{log}")}}" > "$FF_OUTPUTS"'),
        {"id": "review", "kind": "gate", "needs": ["draft"], "on_reject": {"rerun": ["draft"], "max_attempts": 3}},
    ]))
    drive(eng)
    eng.answer("review", "reject", text="add error bars")
    drive(eng)
    eng.answer("review", "reject", text="units!")
    drive(eng)
    eng.answer("review", "approve")
    assert drive(eng).status == "succeeded"
    assert log.read_text().splitlines() == ["fb=[]", "fb=[add error bars]", "fb=[units!]"]
    # ${feedback} creates no dependency edge (otherwise draft -> review -> draft would be a cycle)
    from forgeflow import plan as planmod
    assert planmod.Graph(eng.state().plan).needs["draft"] == []


def test_custom_gate_decisions(mkplan, start):
    eng = start(mkplan([{"id": "pick", "kind": "gate", "decisions": ["fast", "accurate"]},
                        sh("fast", "true", when="${pick.outputs.decision} == 'fast'"),
                        sh("accurate", "true", when="${pick.outputs.decision} == 'accurate'")]))
    drive(eng)
    eng.answer("pick", "accurate")
    drive(eng)
    st = eng.state()
    assert st.nodes["pick"].status == "succeeded"
    assert st.nodes["accurate"].status == "succeeded" and st.nodes["fast"].status == "skipped"


# ------------------------------------------------------------------ retry / timeouts

def test_retry_with_exponential_backoff(mkplan, start, tmp_path):
    cnt = tmp_path / "retry.cnt"
    eng = start(mkplan([sh("flaky", counter(cnt) + '\nif [ "$C" -lt 3 ]; then echo "fail $C" >&2; exit 1; fi\n'
                                    + 'echo "{\\"tries\\": $C}" > "$FF_OUTPUTS"',
                           retry={"max_attempts": 3, "on": ["exit_nonzero"], "backoff": "0.5s"})]))
    t0 = time.time()
    rep = drive(eng)
    assert rep.status == "succeeded"
    assert time.time() - t0 >= 1.4  # 0.5s + 1.0s of backoff
    st = eng.state()
    ns = st.nodes["flaky"]
    assert [a.status for a in ns.attempts] == ["failed", "failed", "succeeded"]
    assert ns.result.outputs == {"tries": 3}
    rs = events(eng, "node.retry_scheduled", "flaky")
    fails = events(eng, "node.failed", "flaky")
    assert len(rs) == 2 and all(f["payload"]["retryable"] for f in fails)
    delays = [(parse_iso(r["payload"]["not_before"]) - parse_iso(f["occurredAtIso"])).total_seconds()
              for r, f in zip(rs, fails)]
    assert 0.4 <= delays[0] <= 0.8 and 0.9 <= delays[1] <= 1.3, delays
    starts = [e for e in events(eng, "node.started", "flaky")]
    assert [s["payload"]["attempt"] for s in starts] == [1, 2, 3]
    for r, s in zip(rs, starts[1:]):
        assert parse_iso(s["occurredAtIso"]) >= parse_iso(r["payload"]["not_before"])


def test_retry_budget_exhausted(mkplan, start):
    eng = start(mkplan([sh("bad", "exit 1", retry={"max_attempts": 2, "on": ["exit_nonzero"], "backoff": "0.1s"})]))
    assert drive(eng).status == "failed"
    ns = eng.state().nodes["bad"]
    assert len(ns.attempts) == 2 and ns.status == "failed"
    assert len(events(eng, "node.retry_scheduled", "bad")) == 1


def test_exit_nonzero_not_retried_by_default(mkplan, start):
    eng = start(mkplan([sh("bad", "exit 1", retry={"max_attempts": 3, "backoff": "0.1s"})]))
    assert drive(eng).status == "failed"
    assert len(eng.state().nodes["bad"].attempts) == 1


def test_retrying_node_is_running_status(mkplan, start):
    eng = start(mkplan([sh("bad", "exit 1", retry={"max_attempts": 2, "on": ["exit_nonzero"], "backoff": "3s"})]))
    st = tick_until(eng, lambda s: s.nodes["bad"].status == "retrying", timeout=10)
    rep = eng.tick()
    assert rep.status == "running" and rep.next_poll_s > 0.5
    assert any("retry at" in w for w in rep.waiting_on)
    assert st.nodes["bad"].retry_not_before


def test_total_timeout_kills(mkplan, start):
    eng = start(mkplan([sh("slow", "sleep 30", timeout="1s")]))
    t0 = time.time()
    rep = drive(eng, timeout=20)
    assert time.time() - t0 < 8
    assert rep.status == "failed"
    err = eng.state().nodes["slow"].last.error
    assert err["error_class"] == "timeout"


def test_idle_timeout_kills_silent_process(mkplan, start):
    eng = start(mkplan([sh("quiet", "echo hello; sleep 30", timeout={"idle": "1s"}),
                        sh("chatty", "for i in 1 2 3 4 5 6 7 8; do echo $i; sleep 0.3; done", timeout={"idle": "1.5s"})]))
    t0 = time.time()
    drive(eng, timeout=20)
    assert time.time() - t0 < 8
    st = eng.state()
    assert st.nodes["quiet"].last.error["error_class"] == "idle_timeout"
    assert st.nodes["chatty"].status == "succeeded"


def test_timeout_is_retryable_when_declared(mkplan, start):
    eng = start(mkplan([sh("slow", "sleep 30", timeout="0.5s",
                           retry={"max_attempts": 2, "on": ["timeout"], "backoff": "0.1s"})]))
    drive(eng, timeout=25)
    ns = eng.state().nodes["slow"]
    assert [a.error["error_class"] for a in ns.attempts] == ["timeout", "timeout"]


# ------------------------------------------------------------------ run status transitions

def run_events(eng):
    return [(e["eventType"], (e["payload"] or {}).get("status")) for e in eng.journal.read()
            if e["eventType"].startswith("run.") or e["eventType"].startswith("plan.")]


def test_run_status_transitions(mkplan, start):
    eng = start(mkplan([sh("a", "true"), {"id": "g", "kind": "gate", "needs": ["a"]}, sh("b", "true", needs=["g"])]),
                approve=False)
    st = eng.state()
    assert st.status == "awaiting_approval" and [g.id for g in st.open_gates()] == ["plan"]
    rep = eng.tick()
    assert rep.status == "awaiting_approval" and not rep.started
    assert not st.nodes["a"].attempts
    eng.answer("plan", "approve", by="human:pi")
    rep = eng.tick()
    st = eng.state()
    assert st.approved and st.approved_by == "human:pi"
    assert st.status == "running" and rep.started == ["a"]
    assert drive(eng).status == "parked"
    eng.answer("g", "approve")
    assert drive(eng).status == "succeeded"
    seq = [t for t, _ in run_events(eng)]
    assert seq[:2] == ["run.created", "plan.proposed"]
    i_app = seq.index("plan.approved")
    assert seq[i_app + 1] == "run.started"
    assert seq.index("run.parked") > i_app
    assert seq[-1] == "run.completed"
    assert eng.state().completed_at


def test_plan_rejected(mkplan, start):
    eng = start(mkplan([sh("a", "true")]), approve=False)
    eng.answer("plan", "reject", text="too expensive")
    rep = eng.tick()
    st = eng.state()
    assert rep.status == "rejected" and st.status == "rejected" and st.status_reason == "too expensive"
    assert not st.nodes["a"].attempts
    eng.tick()
    assert len(events(eng, "plan.rejected")) == 1


def test_tick_is_idempotent_on_settled_runs(mkplan, start):
    eng = start(mkplan([sh("a", "true")]))
    drive(eng)
    n = len(eng.journal.read())
    for _ in range(3):
        eng.tick()
    assert len(eng.journal.read()) == n
    eng2 = start(mkplan([{"id": "g", "kind": "gate"}], id="t2"))
    drive(eng2)
    n = len(eng2.journal.read())
    for _ in range(3):
        eng2.tick()
    assert len(eng2.journal.read()) == n, "parked run must not append a run.parked event on every tick"


def test_concurrent_tick_reports_busy(mkplan, start):
    eng = start(mkplan([sh("a", "true")]))
    with eng.lock() as got:
        assert got
        from forgeflow.engine import Engine
        rep = Engine(eng.paths).tick()
        assert rep.busy
    assert not eng.tick().busy
