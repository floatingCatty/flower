"""Cross-run reuse (fork / replay), provenance export, and file permissions."""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap

from flower.engine import Engine, create_run
from flower.provenance import build_crate, export_crate
from flower.rundir import RunPaths

from core_helpers import drive


def _agent_script(tmp_path):
    """A deterministic stand-in agent that proposes one amendment and counts its calls."""
    counter = tmp_path / "agent_calls.txt"
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys
        sys.stdin.read()
        with open({str(counter)!r}, "a") as fh:
            fh.write("x")
        print(json.dumps({{"choice": "B", "summary": "picked B", "rationale": "B is better",
                          "amendment": {{"rationale": "need a check", "ops": [{{"op": "detour", "after": "pick",
                              "nodes": [{{"id": "check", "kind": "shell", "run": "echo checked"}}]}}]}}}}))
    """))
    return script, counter


def _plan(tmp_path, script):
    return {
        "flower": 1, "id": "replay", "_source": {"dir": str(tmp_path)},
        "nodes": [
            {"id": "prep", "kind": "shell", "run": 'echo "{\\"x\\": 1}" > "$FLOWER_OUTPUTS"', "outputs": {"x": "integer"}},
            {"id": "pick", "kind": "agent", "needs": ["prep"],
             "harness": {"name": "script", "command": [sys.executable, str(script)]},
             "prompt": "choose", "outputs": {"choice": "string"},
             "effects": {"amend": {"auto_approve": True, "max_nodes": 1, "kinds": ["shell"], "ops": ["detour"]}}},
            {"id": "use", "kind": "shell", "needs": ["pick"], "run": "echo using ${pick.outputs.choice}"},
            {"id": "ok", "kind": "gate", "needs": ["use"], "message": "fine?"},
        ],
    }


def test_fork_reuses_agent_decisions_and_replays_their_amendments(home, tmp_path):
    script, counter = _agent_script(tmp_path)
    eng = create_run(_plan(tmp_path, script), {}, root=home, approve=True)
    rep = drive(eng, timeout=30)
    assert rep.status == "parked"  # waiting on gate ok
    eng.answer("ok#a1", "approve", by="human:t")
    assert drive(eng, timeout=30).status == "succeeded"
    st = eng.state()
    assert "check" in st.graph().nodes and counter.read_text() == "x"

    # replay the approved base plan from `use`: prep + pick reused, the agent is NOT called again,
    # its recorded amendment is re-applied, and the human gate is asked again
    src = st.run_id
    eng2 = create_run(dict(st.generations[0]["plan"]), st.inputs, root=home, approve=True,
                      reuse_from=[src], rerun_from=["use"])
    rep2 = drive(eng2, timeout=30)
    st2 = eng2.state()
    assert counter.read_text() == "x", "agent was called again during replay"
    assert st2.nodes["prep"].result.reused_from == f"{src}:prep#a1"
    assert st2.nodes["pick"].result.reused_from == f"{src}:pick#a1"
    assert "check" in st2.graph().nodes  # amendment replayed
    assert st2.nodes["use"].result.reused_from is None  # re-executed (rerun_from)
    assert rep2.status == "parked" and st2.nodes["ok"].status == "waiting"  # decisions are never reused


def test_reuse_only_when_definition_and_inputs_match(home, tmp_path):
    plan = {"flower": 1, "id": "r2", "inputs": {"n": {"type": "integer", "default": 1}},
            "nodes": [{"id": "a", "kind": "shell", "run": 'echo "{\\"v\\": ${inputs.n}}" > "$FLOWER_OUTPUTS"'},
                      {"id": "b", "kind": "shell", "run": "echo b"}]}
    e1 = create_run(plan, {"n": 1}, root=home, approve=True)
    assert drive(e1, timeout=20).status == "succeeded"
    e2 = create_run(plan, {"n": 2}, root=home, approve=True, reuse_from=[e1.state().run_id])
    assert drive(e2, timeout=20).status == "succeeded"
    st = e2.state()
    assert st.nodes["a"].result.reused_from is None and st.nodes["a"].result.outputs == {"v": 2}
    assert st.nodes["b"].result.reused_from.endswith(":b#a1")


def test_fork_does_not_reuse_function_result_after_its_module_changed(home, tmp_path):
    # regression (benchmark si-dos-fermi): a fork replayed results computed by the old code
    src = tmp_path / "src"
    src.mkdir()
    (src / "calc.py").write_text("def f():\n    return {'v': 1}\n")
    plan = {"flower": 1, "id": "code", "_source": {"dir": str(src)},
            "nodes": [{"id": "f", "kind": "function", "call": "calc:f"},
                      {"id": "b", "kind": "shell", "run": "echo b"}]}
    e1 = create_run(dict(plan), {}, root=home, approve=True)
    assert drive(e1, timeout=20).status == "succeeded"
    rid = e1.state().run_id
    e2 = create_run(dict(plan), {}, root=home, approve=True, reuse_from=[rid])
    assert drive(e2, timeout=20).status == "succeeded"
    assert e2.state().nodes["f"].result.reused_from == f"{rid}:f#a1"   # unchanged source: reused
    (src / "calc.py").write_text("def f():\n    return {'v': 20}\n")
    e3 = create_run(dict(plan), {}, root=home, approve=True, reuse_from=[rid])
    assert drive(e3, timeout=20).status == "succeeded"
    st = e3.state()
    assert st.nodes["f"].result.reused_from is None and st.nodes["f"].result.outputs == {"v": 20}
    assert st.nodes["b"].result.reused_from == f"{rid}:b#a1"


def test_ro_crate_export_is_valid_json_ld_with_provenance(home, tmp_path):
    plan = {"flower": 1, "id": "crate", "title": "Crate test",
            "nodes": [{"id": "a", "kind": "shell", "run": 'echo hi > out.txt; echo "{}" > "$FLOWER_OUTPUTS"',
                       "files": {"out": "out.txt"}},
                      {"id": "g", "kind": "gate", "needs": ["a"], "message": "ok?"}]}
    eng = create_run(plan, {}, root=home, approve=True)
    drive(eng, timeout=20)
    eng.answer("g#a1", "approve", text="looks good", by="human:rev")
    drive(eng, timeout=20)
    crate = build_crate(eng)
    assert crate["@context"] == "https://w3id.org/ro/crate/1.1/context"
    graph = {e["@id"]: e for e in crate["@graph"]}
    assert graph["./"]["mainEntity"] == {"@id": "plan.yaml"}
    assert "ComputationalWorkflow" in graph["plan.yaml"]["@type"]
    files = [e for e in crate["@graph"] if e.get("@type") == "File" and e.get("sha256")]
    assert files and files[0]["@id"].endswith("out.txt")
    decisions = [e for e in crate["@graph"] if str(e.get("@id", "")).startswith("#decision-")]
    assert any(d.get("disambiguatingDescription") == "looks good" for d in decisions)
    assert any(e.get("@type") == "Person" and e.get("name") == "human:rev" for e in crate["@graph"])
    path = export_crate(eng)
    assert json.loads(path.read_text())["@graph"]


def test_generated_files_respect_umask(home, tmp_path):
    old = os.umask(0o022)
    try:
        eng = create_run({"flower": 1, "id": "perm", "nodes": [{"id": "a", "kind": "shell", "run": "true"}]},
                         {}, root=home, approve=True)
        drive(eng, timeout=20)
        from flower.report import write_report
        paths = write_report(eng)
        for p in (eng.paths.plan_file, paths["md"], paths["html"]):
            mode = stat.S_IMODE(os.stat(p).st_mode)
            assert mode & 0o044 == 0o044, f"{p} is not group/world readable: {oct(mode)}"
    finally:
        os.umask(old)
