"""Agent-proposed plan amendments (effects.amend policies) via the `script` harness."""
from __future__ import annotations

import pytest

from agentkit import agent_node, events, last_attempt, make_plan


def shell(nid, needs=None, run="echo ran-$FF_NODE_ID"):
    n = {"id": nid, "kind": "shell", "run": run}
    if needs:
        n["needs"] = needs
    return n


def proposer(fake, amendment, effects=None, nid="scout", extra=None, name=None):
    ans = {"summary": "found something", "rationale": "because", "amendment": amendment}
    f = fake("script", [{"answer": ans}], name=name or nid)
    kw = {}
    if effects is not None:
        kw["effects"] = effects
    kw.update(extra or {})
    return agent_node(nid, f.harness(), **kw), f


def amend_add(*nodes, rationale="need more data"):
    return {"rationale": rationale, "ops": [{"op": "add", "nodes": list(nodes)}]}


def amendments(st):
    return list(st.amendments.values())


# ====================================================================== permission

def test_amendment_without_effects_permission_is_ignored_with_note(fake, run_plan):
    node, _ = proposer(fake, amend_add(shell("extra", ["scout"])))
    eng, st = run_plan(make_plan([node]))
    assert st.status == "succeeded"
    assert st.generation == 0 and not st.amendments and "extra" not in st.nodes
    notes = [n["text"] for n in st.notes]
    assert any("no `effects.amend` permission; ignored" in t for t in notes)
    assert last_attempt(st, "scout").status == "succeeded"


def test_amendment_key_in_answer_is_not_an_output(fake, run_plan):
    node, _ = proposer(fake, amend_add(shell("extra", ["scout"])), effects={"amend": True})
    eng, st = run_plan(make_plan([node]))
    assert "amendment" not in last_attempt(st, "scout").outputs
    assert "summary" not in last_attempt(st, "scout").outputs


def test_amend_true_without_policy_always_needs_a_gate(fake, run_plan):
    node, _ = proposer(fake, amend_add(shell("extra", ["scout"])), effects={"amend": True})
    eng, st = run_plan(make_plan([node]))
    assert st.status == "parked"
    (am,) = amendments(st)
    assert am.status == "proposed" and am.proposed_by == "agent:scout" and am.source_node == "scout"
    gates = st.open_gates()
    assert [g.id for g in gates] == [f"amend-{am.id}"] and gates[0].subject == "amendment"
    assert "PLAN CHANGE proposed by agent:scout" in gates[0].message
    assert "Why: need more data" in gates[0].message and "+ extra: shell" in gates[0].message
    assert (eng.paths.pending / f"amend-{am.id}.request.json").exists()


# ====================================================================== auto-approve within policy

def test_auto_approve_within_policy_applies_and_runs_new_node(fake, run_plan):
    pol = {"amend": {"auto_approve": True, "max_nodes": 2, "kinds": ["shell"]}}
    node, _ = proposer(fake, amend_add(shell("extra", ["scout"]), shell("extra2", ["extra"])), effects=pol)
    eng, st = run_plan(make_plan([node]))
    assert st.status == "succeeded", st.status_reason
    assert st.generation == 1
    (am,) = amendments(st)
    assert am.status == "approved" and am.decided_by == "policy:scout" and am.generation == 1
    assert am.diff["added"] == ["extra", "extra2"]
    assert st.nodes["extra"].status == "succeeded" and st.nodes["extra2"].status == "succeeded"
    assert not st.open_gates()
    assert eng.paths.current_plan_file.exists() and "extra2" in eng.paths.current_plan_file.read_text()
    assert st.generations[1]["amendment"] == am.id
    # base digest unchanged, current digest moved
    assert st.base_digest != st.digest


def test_auto_approved_detour_runs_before_existing_child(fake, run_plan):
    pol = {"amend": {"auto_approve": True}}
    amd = {"rationale": "convergence test first", "ops": [{"op": "detour", "after": "scout",
                                                            "nodes": [shell("kconv")]}]}
    node, _ = proposer(fake, amd, effects=pol)
    eng, st = run_plan(make_plan([node, shell("final", ["scout"])]))
    assert st.status == "succeeded", st.status_reason
    assert "kconv" in st.graph().needs["final"]
    seq = {e["nodeId"]: e["seq"] for e in events(eng, "node.started")}
    assert seq["kconv"] < seq["final"]


# ====================================================================== outside policy -> gate

@pytest.mark.parametrize("policy,amd,why", [
    ({"auto_approve": True, "max_nodes": 1}, amend_add(shell("e1", ["scout"]), shell("e2", ["scout"])), "too many nodes"),
    ({"auto_approve": True, "kinds": ["agent"]}, amend_add(shell("e1", ["scout"])), "disallowed kind"),
    ({"auto_approve": True, "ops": ["detour"]}, amend_add(shell("e1", ["scout"])), "disallowed op"),
])
def test_outside_policy_opens_gate_and_approval_applies(fake, run_plan, policy, amd, why):
    node, _ = proposer(fake, amd, effects={"amend": policy})
    eng, st = run_plan(make_plan([node]))
    assert st.status == "parked", why
    (am,) = amendments(st)
    assert am.status == "proposed", why
    gid = f"amend-{am.id}"
    assert st.gates[gid].status == "open"
    eng.answer(gid, "approve", text="ok, go", by="human:tester")
    eng.drive(timeout=30)
    st = eng.state()
    assert st.status == "succeeded", st.status_reason
    am = st.amendments[am.id]
    assert am.status == "approved" and am.decided_by == "human:tester" and st.generation == 1
    assert st.nodes["e1"].status == "succeeded"


def test_disallowed_stop_op_needs_gate_and_rejection_keeps_plan(fake, run_plan):
    pol = {"amend": {"auto_approve": True}}  # default ops: add, detour
    amd = {"rationale": "not needed", "ops": [{"op": "stop", "nodes": ["later"]}]}
    node, _ = proposer(fake, amd, effects=pol)
    eng, st = run_plan(make_plan([node, shell("gate-free", ["scout"]), shell("later", ["gate-free"])]))
    # the run keeps going (later runs) while the proposal waits for a human
    (am,) = amendments(st)
    gid = f"amend-{am.id}"
    eng.answer(gid, "reject", text="keep it", by="human:tester")
    eng.drive(timeout=30)
    st = eng.state()
    assert st.amendments[am.id].status == "rejected"
    assert st.amendments[am.id].decided_by == "human:tester"
    assert st.nodes["later"].status == "succeeded" and st.generation == 0
    assert st.status == "succeeded"


# ====================================================================== invalid proposals

@pytest.mark.parametrize("ops", [
    [{"op": "add", "nodes": [shell("scout")]}],                       # duplicate id
    [{"op": "frobnicate"}],                                           # unknown op
    [{"op": "add", "nodes": [{"id": "x", "kind": "shell"}]}],         # shell without run
    [{"op": "add", "nodes": [{"id": "x", "kind": "shell", "run": "true", "needs": ["ghost"]}]}],  # unknown ref
    [{"op": "replace", "node": "scout", "with": {"kind": "shell", "run": "true"}}],  # history is immutable
    [{"op": "add", "nodes": [{"kind": "shell", "run": "true"}]}],     # missing id
])
def test_invalid_amendment_is_rejected_but_node_succeeds(fake, run_plan, ops):
    node, _ = proposer(fake, {"rationale": "bad idea", "ops": ops},
                       effects={"amend": {"auto_approve": True, "ops": ["add", "detour", "replace"]}})
    eng, st = run_plan(make_plan([node]))
    assert st.nodes["scout"].status == "succeeded"
    assert st.status == "succeeded", st.status_reason
    (am,) = amendments(st)
    assert am.status == "rejected" and am.decided_by == "forgeflow"
    rej = events(eng, "plan.amendment.rejected")[-1]["payload"]
    assert rej["issues"], "rejection must carry the validation issues"
    assert st.generation == 0 and not st.open_gates()


def test_empty_ops_proposal_is_a_noop(fake, run_plan):
    node, _ = proposer(fake, {"rationale": "nothing", "ops": []}, effects={"amend": {"auto_approve": True}})
    eng, st = run_plan(make_plan([node]))
    assert st.status == "succeeded" and not st.amendments


def test_non_dict_amendment_is_ignored(fake, run_plan):
    f = fake("script", [{"answer": {"summary": "s", "amendment": "please add a node"}}])
    eng, st = run_plan(make_plan([agent_node("scout", f.harness(), effects={"amend": {"auto_approve": True}})]))
    assert st.status == "succeeded" and not st.amendments


def test_amendment_from_failed_contract_is_not_proposed(fake, run_plan):
    f = fake("script", [{"answer": {"summary": "s", "amendment": amend_add(shell("extra", ["scout"]))}}])
    node = agent_node("scout", f.harness(), effects={"amend": {"auto_approve": True}}, files={"r": "missing.txt"})
    eng, st = run_plan(make_plan([node]))
    assert st.nodes["scout"].status == "failed"
    assert not st.amendments and "extra" not in st.graph().nodes


def test_auto_approve_with_malformed_ops_does_not_crash_the_tick(fake, run_plan):
    node, _ = proposer(fake, {"rationale": "x", "ops": ["add extra node please"]},
                       effects={"amend": {"auto_approve": True}})
    eng, st = run_plan(make_plan([node]))  # must not raise
    (am,) = amendments(st)
    assert am.status == "rejected"


# ====================================================================== policy bypass / escalation

def test_kinds_policy_cannot_be_bypassed_with_singular_node_key(fake, run_plan, tmp_path):
    pol = {"amend": {"auto_approve": True, "kinds": ["shell"]}}
    sneaky = {"id": "sneaky", "kind": "function", "call": "os:getcwd", "needs": ["scout"]}
    node, _ = proposer(fake, {"rationale": "x", "ops": [{"op": "add", "node": sneaky}]}, effects=pol)
    eng, st = run_plan(make_plan([node]))
    (am,) = amendments(st)
    assert am.status == "proposed", "a `function` node was auto-approved under kinds: [shell]"


def test_auto_approved_nodes_cannot_escalate_their_amend_policy(fake, run_plan):
    child_fake = fake("script", [{"answer": {"summary": "child", "amendment": amend_add(
        *[shell(f"spawn{i}", ["child"]) for i in range(5)])}}], name="child")
    child = agent_node("child", child_fake.harness(), needs=["scout"], effects={"amend": {"auto_approve": True}})
    node, _ = proposer(fake, amend_add(child), effects={"amend": {"auto_approve": True, "max_nodes": 1,
                                                                  "kinds": ["agent"]}})
    eng, st = run_plan(make_plan([node]))
    added = [n for n in st.graph().nodes if n.startswith("spawn")]
    assert not added, f"max_nodes=1 grant escalated into {len(added) + 1} auto-approved nodes"


def test_gated_amendment_is_bound_to_the_plan_generation_it_was_reviewed_against(fake, run_plan):
    amd = {"rationale": "insert a check", "ops": [{"op": "detour", "after": "scout", "nodes": [shell("k")]}]}
    node, _ = proposer(fake, amd, effects={"amend": True})
    eng, st = run_plan(make_plan([node, {"id": "w", "kind": "wait", "signal": "go"}]))
    (am,) = amendments(st)
    gid = f"amend-{am.id}"
    reviewed_diff = st.gates[gid].extra.get("diff") or am.diff
    assert reviewed_diff["added"] == ["k"] and not reviewed_diff["changed"]
    # meanwhile, the plan moves on (generation 1): a new pending child of `scout`
    eng.propose_amendment([{"op": "add", "nodes": [shell("c", ["scout", "w"])]}], "human addition",
                          by="human:other", auto_approve=True)
    assert eng.state().generation == 1
    eng.answer(gid, "approve", by="human:reviewer")
    eng.tick()
    st = eng.state()
    applied = st.amendments[am.id]
    assert applied.status == "rejected" or "k" not in st.graph().needs["c"], \
        "approval of a generation-0 proposal re-wired node `c` (added in generation 1) without review"
