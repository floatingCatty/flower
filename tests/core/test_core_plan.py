"""Plan validation (all issues at once, with suggestions), normalisation, refs-imply-edges, digests, diff,
and apply_amendment (add / detour / replace / drop / set_needs / stop / supersede + history guards)."""
from __future__ import annotations

import pytest

from flower import plan as planmod
from flower.plan import PlanInvalid


def P(nodes, **top):
    d = {"flower": 1, "id": "p", "nodes": nodes}
    d.update(top)
    return d


def issues_of(raw):
    with pytest.raises(PlanInvalid) as ei:
        planmod.check(raw)
    return ei.value.issues


def codes(issues):
    return sorted(i["code"] for i in issues)


def test_valid_plan_normalises_defaults():
    plan = planmod.check(P([{"id": "a", "kind": "shell", "run": "true"},
                            {"id": "b", "kind": "shell", "run": "true", "needs": "a", "timeout": "5m"}],
                           defaults={"retry": {"max_attempts": 2}, "timeout": "1h"}))
    a, b = plan["nodes"]
    assert a["trigger"] == "all_success"
    assert a["retry"] == {"max_attempts": 2, "backoff": "10s"}
    assert a["timeout"] == {"total": "1h"}
    assert b["timeout"] == {"total": "5m"}
    assert b["needs"] == ["a"]
    assert a["cache"] is True
    assert plan["title"] == "p"


def test_validation_returns_all_issues_at_once():
    raw = P([
        {"id": "a", "kind": "shell", "run": "true", "retyr": {"max_attempts": 2}},       # unknown field (typo)
        {"id": "b", "kind": "shell", "run": "echo ${ghost.outputs.x}"},                   # unknown ref
        {"id": "c", "kind": "shell", "timeout": {"total": "5 parsecs"}},                  # missing run + bad duration
        {"id": "d", "kind": "shell", "run": "x", "cluster": "nope"},                      # unknown cluster
        {"id": "e", "kind": "gate", "on_reject": {"rerun": ["zzz"]}},                     # on_reject unknown node
        {"id": "f", "kind": "agent", "prompt": "x"},                                      # a removed kind
        {"id": "g", "kind": "teleport"},                                                  # unknown kind
        {"id": "i", "kind": "shell", "run": "true", "trigger": "sometimes"},              # bad trigger
        {"id": "j", "kind": "shell", "run": "true", "retry": {"max_attempts": 0}},        # bad retry
    ], bogus_top=1)
    iss = issues_of(raw)
    by_code = codes(iss)
    for c in ("unknown_field", "unknown_ref", "required", "duration", "cluster", "kind", "trigger", "retry"):
        assert c in by_code, (c, iss)
    # every problem is reported, including two unknown_ref (ghost + zzz) and two unknown_field (typo + top)
    assert by_code.count("unknown_ref") >= 2
    assert by_code.count("unknown_field") >= 2
    # suggestions are attached
    typo = next(i for i in iss if i["code"] == "unknown_field" and "retyr" in i["path"])
    assert "retry" in typo["suggestion"]
    ghost = next(i for i in iss if i["code"] == "unknown_ref" and "ghost" in i["message"])
    assert "known nodes" in ghost["suggestion"]
    cl = next(i for i in iss if i["code"] == "cluster")
    assert "clusters" in cl["suggestion"]
    assert any(i["path"].endswith("on_reject.rerun") for i in iss)


def test_plan_invalid_error_payload():
    with pytest.raises(PlanInvalid) as ei:
        planmod.check(P([{"id": "a", "kind": "shell"}]))
    e = ei.value
    assert e.code == "plan_invalid"
    d = e.to_dict()
    assert d["details"] and d["suggestion"]


@pytest.mark.parametrize("raw,code", [
    ({"flower": 2, "id": "p", "nodes": [{"id": "a", "kind": "shell", "run": "x"}]}, "version"),
    ({"flower": 1, "nodes": [{"id": "a", "kind": "shell", "run": "x"}]}, "plan_id"),
    ({"flower": 1, "id": "p", "nodes": {}}, "nodes"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "1bad", "kind": "shell", "run": "x"}]}, "node_id"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "shell", "run": "x"},
                                           {"id": "a", "kind": "shell", "run": "y"}]}, "duplicate_id"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "shell"}]}, "required"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "function", "call": "m:f"}]}, "kind"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "shell", "run": "x",
                                           "outputs": {"k": "tensor"}}]}, "output_type"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "shell", "run": "x", "when": "lambda: 1"}]}, "expr"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "shell", "run": "x", "foreach": 3}]}, "foreach"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "gate", "decisions": []}]}, "decisions"),
    ({"flower": 1, "id": "p", "nodes": [{"id": "a", "kind": "shell", "run": "x", "timeout": {"total": "soon"}}]},
     "duration"),
    ({"flower": 1, "id": "p", "clusters": {"h": {"transport": "ftp"}},
      "nodes": [{"id": "a", "kind": "shell", "cluster": "h", "run": "x"}]}, "cluster"),
    ({"flower": 1, "id": "p", "clusters": {"h": {"transport": "ssh"}},
      "nodes": [{"id": "a", "kind": "shell", "cluster": "h", "run": "x"}]}, "cluster"),
])
def test_single_issue_codes(raw, code):
    assert code in codes(issues_of(raw))


def test_cycle_detected_with_path():
    iss = issues_of(P([{"id": "a", "kind": "shell", "run": "x", "needs": ["c"]},
                       {"id": "b", "kind": "shell", "run": "x", "needs": ["a"]},
                       {"id": "c", "kind": "shell", "run": "x", "needs": ["b"]}]))
    cyc = [i for i in iss if i["code"] == "cycle"]
    assert cyc and "->" in cyc[0]["message"]
    assert "on_reject" in cyc[0]["suggestion"]


def test_self_ref_via_template_is_not_a_cycle():
    planmod.check(P([{"id": "a", "kind": "shell", "run": "echo ${a.dir}"}]))


def test_cycle_via_template_refs():
    iss = issues_of(P([{"id": "a", "kind": "shell", "run": "echo ${b.outputs.x}"},
                       {"id": "b", "kind": "shell", "run": "echo ${a.outputs.x}"}]))
    assert "cycle" in codes(iss)


def test_refs_imply_edges():
    plan = planmod.check(P([{"id": "a", "kind": "shell", "run": "x"},
                            {"id": "b", "kind": "shell", "run": "x"},
                            {"id": "c", "kind": "shell", "run": "echo ${a.outputs.v} ${inputs.q} ${env.HOME} ${run.id}",
                             "when": "${b.outputs.ok} == 1", "needs": []}],
                           inputs={"q": {"type": "string", "default": "z"}}))
    g = planmod.Graph(plan)
    assert sorted(g.needs["c"]) == ["a", "b"]
    assert g.descendants("a") == ["c"]
    assert g.ancestors("c") == {"a", "b"}
    assert g.topo().index("c") > g.topo().index("a")


def test_escaped_ref_does_not_imply_edge():
    plan = planmod.check(P([{"id": "a", "kind": "shell", "run": "echo $${ghost.outputs.x}"}]))
    assert planmod.Graph(plan).needs["a"] == []


def test_digest_ignores_loader_metadata_and_decl_hash_ignores_resources():
    raw = P([{"id": "a", "kind": "shell", "run": "x"}])
    p1 = planmod.check(dict(raw, _source={"dir": "/a"}))
    p2 = planmod.check(dict(raw, _source={"dir": "/b"}))
    assert planmod.plan_digest(p1) == planmod.plan_digest(p2)
    n = p1["nodes"][0]
    assert planmod.decl_hash(n) == planmod.decl_hash(dict(n, timeout={"total": "9h"}, retry={"max_attempts": 9},
                                                         title="other"))
    assert planmod.decl_hash(n) != planmod.decl_hash(dict(n, run="y"))


def test_diff_plans():
    a = planmod.check(P([{"id": "x", "kind": "shell", "run": "1"}, {"id": "y", "kind": "shell", "run": "1"}]))
    b = planmod.check(P([{"id": "x", "kind": "shell", "run": "2"}, {"id": "z", "kind": "shell", "run": "1"}]))
    d = planmod.diff_plans(a, b)
    assert d["added"] == ["z"] and d["removed"] == ["y"]
    assert d["changed"] == [{"id": "x", "fields": ["run"]}]
    assert d["unchanged"] == []


def test_non_dict_retry_reports_issue_instead_of_crashing():
    """`retry: 3` is a natural typo; validation must report it, not raise TypeError/ValueError."""
    try:
        planmod.check(P([{"id": "a", "kind": "shell", "run": "x", "retry": 3}]))
    except PlanInvalid:
        return
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"crashed instead of reporting an issue: {type(exc).__name__}: {exc}")
    pytest.fail("accepted retry: 3")


def test_non_dict_outputs_spec_reports_issue_instead_of_crashing():
    try:
        planmod.check(P([{"id": "a", "kind": "shell", "run": "x", "outputs": ["a", "b"]}]))
    except PlanInvalid:
        return
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"crashed instead of reporting an issue: {type(exc).__name__}: {exc}")
    pytest.fail("accepted a list as outputs")


# ------------------------------------------------------------------ amendments

def base():
    return planmod.check(P([{"id": "a", "kind": "shell", "run": "1"},
                            {"id": "b", "kind": "shell", "run": "2", "needs": ["a"]},
                            {"id": "c", "kind": "shell", "run": "3", "needs": ["b"]}]))


def test_amend_add_and_detour_rewires_pending_children():
    st = {"a": "succeeded", "b": "pending", "c": "pending"}
    new, eff = planmod.apply_amendment(base(), [
        {"op": "add", "nodes": [{"id": "extra", "kind": "shell", "run": "x", "needs": ["a"]}]},
        {"op": "detour", "after": "a", "nodes": [{"id": "d1", "kind": "shell", "run": "x"},
                                                 {"id": "d2", "kind": "shell", "run": "y"}]},
    ], st)
    nodes = {n["id"]: n for n in new["nodes"]}
    assert eff["added"] == ["extra", "d1", "d2"]
    assert nodes["d1"]["needs"] == ["a"] and nodes["d2"]["needs"] == ["d1"]
    assert "d2" in nodes["b"]["needs"]          # pending child of `a` now waits for the detour
    assert "d2" in nodes["extra"]["needs"]      # also pending, also rewired
    assert nodes["extra"]["retry"]["max_attempts"] == 1  # normalised


def test_amend_replace_pending_ok_finished_refused_without_supersede():
    st = {"a": "succeeded", "b": "pending", "c": "pending"}
    new, eff = planmod.apply_amendment(base(), [{"op": "replace", "node": "b",
                                                 "with": {"kind": "shell", "run": "new", "needs": ["a"]}}], st)
    assert next(n for n in new["nodes"] if n["id"] == "b")["run"] == "new"
    assert eff["changed"] == ["b"] and eff["stale"] == []
    with pytest.raises(PlanInvalid) as ei:
        planmod.apply_amendment(base(), [{"op": "replace", "node": "a", "with": {"kind": "shell", "run": "z"}}], st)
    assert ei.value.issues[0]["code"] == "amend_history"
    assert "supersede" in ei.value.issues[0]["suggestion"]


def test_amend_supersede_marks_downstream_cone_stale():
    st = {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    new, eff = planmod.apply_amendment(base(), [{"op": "replace", "node": "a", "supersede": True,
                                                 "with": {"kind": "shell", "run": "z"}}], st)
    assert eff["stale"] == ["a", "b", "c"]


@pytest.mark.parametrize("op", [
    {"op": "drop", "node": "a"},
    {"op": "set_needs", "node": "a", "needs": []},
    {"op": "stop", "node": "a"},
])
def test_amend_history_is_immutable(op):
    with pytest.raises(PlanInvalid):
        planmod.apply_amendment(base(), [op], {"a": "succeeded", "b": "running", "c": "pending"})


def test_amend_drop_with_dependants_is_rejected_by_validation():
    with pytest.raises(PlanInvalid) as ei:
        planmod.apply_amendment(base(), [{"op": "drop", "node": "b"}], {"a": "pending", "b": "pending", "c": "pending"})
    assert "unknown_ref" in codes(ei.value.issues)


def test_amend_unknown_op_and_duplicates():
    with pytest.raises(PlanInvalid) as ei:
        planmod.apply_amendment(base(), [{"op": "explode"},
                                         {"op": "add", "nodes": [{"id": "a", "kind": "shell", "run": "x"}]}], {})
    assert codes(ei.value.issues) == ["amend_add", "amend_op"]


def test_amend_stop_pending_only():
    _, eff = planmod.apply_amendment(base(), [{"op": "stop", "nodes": ["b", "c"]}], {"a": "running"})
    assert eff["stop"] == ["b", "c"]


def test_amend_creating_cycle_rejected():
    with pytest.raises(PlanInvalid) as ei:
        planmod.apply_amendment(base(), [{"op": "set_needs", "node": "a", "needs": ["c"]}], {})
    assert "cycle" in codes(ei.value.issues)


def test_amend_does_not_mutate_input_plan():
    p = base()
    import copy
    snap = copy.deepcopy(p)
    planmod.apply_amendment(p, [{"op": "detour", "after": "a", "nodes": [{"id": "d", "kind": "shell", "run": "x"}]}],
                            {})
    assert p == snap


def test_yaml_on_off_yes_no_are_strings_not_booleans(tmp_path):
    """YAML 1.1 turns the key `on` into True; `retry: {on: [...]}` must survive loading and hashing."""
    from flower import plan as P
    f = tmp_path / "p.yaml"
    f.write_text("flower: 1\nid: y\nnodes:\n  - id: a\n    kind: shell\n    run: 'true'\n"
                 "    retry: {on: [exit_nonzero], max_attempts: 2}\n    env: {MODE: yes, FLAG: off}\n")
    plan = P.check(P.load_plan_file(f))
    node = plan["nodes"][0]
    assert node["retry"]["on"] == ["exit_nonzero"] and node["env"] == {"MODE": "yes", "FLAG": "off"}
    assert P.plan_digest(plan).startswith("sha256:")
