"""The CLI surface in subprocesses: JSON envelope {ok, data, error, next} and exit codes 0/1/2/3."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from agentkit import agent_node, ff, make_plan, write_plan

GOOD = {"energy": -5.25, "summary": "computed the energy", "rationale": "PBE because the task said so"}


def assert_envelope(data, ok=True):
    assert isinstance(data, dict), "stdout must be exactly one JSON object with --json"
    assert data["ok"] is ok
    assert "next" in data and isinstance(data["next"], list)
    if ok:
        assert "data" in data
    else:
        assert set(data["error"]) >= {"code", "message"}


@pytest.fixture
def agent_plan(fake, tmp_path):
    f = fake("claude", [{"answer": GOOD}], name="cli-claude")
    plan = make_plan([agent_node("calc", f.harness(), outputs={"energy": "number"}),
                      {"id": "post", "kind": "shell", "needs": ["calc"],
                       "run": 'echo "{\\"double\\": 2, \\"summary\\": \\"post done\\"}" > "$FF_OUTPUTS"'}],
                     pid="cli-agent", results=["calc"])
    return write_plan(tmp_path / "plan.yaml", plan), f


# ====================================================================== plan verbs

def test_plan_new_validate_show_reference(tmp_path):
    p = tmp_path / "starter.yaml"
    code, d, _ = ff("plan", "new", p)
    assert code == 0 and p.exists()
    assert_envelope(d)
    code, d, _ = ff("plan", "validate", p)
    assert code == 0 and d["data"]["valid"] is True and d["data"]["id"] == "starter"
    assert d["data"]["digest"].startswith("sha256:")
    assert any("forgeflow run" in n for n in d["next"])
    code, d, _ = ff("plan", "show", p)
    assert code == 0 and "overview" in d["data"] and d["data"]["plan"]["id"] == "starter"
    code, d, _ = ff("plan", "reference")
    assert code == 0 and "forgeflow plan reference" in d["data"]["reference"]
    code, d, _ = ff("plan", "new", p)
    assert code == 2
    assert_envelope(d, ok=False)
    assert d["error"]["code"] == "exists"
    code, d, _ = ff("plan", "new", p, "--force")
    assert code == 0


def test_plan_validate_reports_every_issue(tmp_path):
    bad = {"forgeflow": 1, "id": "bad plan!", "nodes": [
        {"id": "a", "kind": "agent", "harness": {"name": "gemini"}},
        {"id": "b", "kind": "shell", "run": "true", "needs": ["ghost"], "typo_field": 1}]}
    p = write_plan(tmp_path / "bad.yaml", bad)
    code, d, _ = ff("plan", "validate", p)
    assert code == 2
    assert_envelope(d, ok=False)
    assert d["error"]["code"] == "plan_invalid"
    codes = {i["code"] for i in d["error"]["details"]}
    assert {"plan_id", "required", "harness", "unknown_ref", "unknown_field"} <= codes
    assert all("path" in i and "message" in i for i in d["error"]["details"])


def test_plan_validate_missing_file_and_bad_yaml(tmp_path):
    code, d, _ = ff("plan", "validate", tmp_path / "nope.yaml")
    assert code == 2 and d["error"]["code"] == "plan_not_found"
    (tmp_path / "x.yaml").write_text("nodes: [\n  - id: a\n   kind: shell")
    code, d, _ = ff("plan", "validate", tmp_path / "x.yaml")
    assert code == 2 and d["error"]["code"] == "plan_yaml"


# ====================================================================== the run lifecycle

def test_full_agent_run_lifecycle(agent_plan, tmp_path, ff_home):
    plan_path, f = agent_plan
    # run -> awaiting approval (exit 3), never auto-approved in --json mode
    code, d, _ = ff("run", plan_path)
    assert code == 3
    assert_envelope(d)
    rid = d["data"]["run_id"]
    assert d["data"]["status"] == "awaiting_approval"
    assert [g["id"] for g in d["data"]["open_gates"]] == ["plan"]
    assert any(n.startswith(f"forgeflow approve {rid}") for n in d["next"])
    assert "overview" in d["data"]
    assert f.calls() == []
    events = (ff_home / ".forgeflow" / "runs" / rid / "events.jsonl").read_text()
    assert "plan.approved" not in events

    code, d, _ = ff("status", rid)
    assert code == 0 and d["data"]["status"] == "awaiting_approval"
    code, d, _ = ff("wait", rid, "--timeout", "5")
    assert code == 3 and d["data"]["status"] == "awaiting_approval"

    code, d, _ = ff("show", rid, "--gate", "plan")
    assert code == 0 and d["data"]["id"] == "plan" and d["data"]["status"] == "open"
    assert "calc" in d["data"]["message"]

    # approve and drive in the foreground
    code, d, _ = ff("approve", rid, "--note", "looks right", "--wait")
    assert code == 0, d
    assert d["data"]["status"] == "succeeded"
    assert any(n == f"forgeflow report {rid}" for n in d["next"])
    code, d, _ = ff("wait", rid)
    assert code == 0 and d["data"]["status"] == "succeeded"
    code, d, _ = ff("status", rid)
    assert code == 0 and {n["id"]: n["status"] for n in d["data"]["nodes"]} == {"calc": "succeeded", "post": "succeeded"}
    assert d["data"]["cost"]["usd"] == pytest.approx(0.25)

    # show node
    code, d, _ = ff("show", rid, "calc")
    assert code == 0
    (att,) = d["data"]["attempts"]
    assert att["session"] and att["outputs"] == {"energy": -5.25} and att["rationale"] == GOOD["rationale"]
    assert d["data"]["spec"]["harness"]["name"] == "claude"
    code, txt, p = ff("show", rid, "calc", json_out=False)
    assert code == 0 and "agent session:" in p.stdout and "rationale: PBE" in p.stdout

    # logs: rendered agent transcript
    code, d, _ = ff("logs", rid, "calc")
    assert code == 0
    t = d["data"]["text"]
    assert "=== prompt ===" in t and "=== turn 0 (main) ===" in t
    assert "assistant: Let me look at the working directory." in t and "  → Bash: ls -la" in t
    assert "[result: success · 3 turns · $0.2500]" in t and "=== final structured answer ===" in t
    code, d, _ = ff("logs", rid, "calc", "--raw")
    assert code == 0 and '"type": "result"' in d["data"]["text"]
    code, d, _ = ff("logs", rid, "post")
    assert code == 0 and d["data"]["text"] == "(no output)"  # the shell node wrote only $FF_OUTPUTS

    # output
    code, d, _ = ff("output", rid, "calc")
    assert code == 0 and d["data"]["outputs"] == {"energy": -5.25} and d["data"]["summary"] == GOOD["summary"]
    code, d, _ = ff("output", rid, "calc", "energy")
    assert code == 0 and d["data"] == -5.25
    code, d, _ = ff("output", rid, "post", "double")
    assert code == 0 and d["data"] == 2

    # report
    code, d, _ = ff("report", rid)
    assert code == 0
    md = Path(d["data"]["md"]).read_text()
    html = Path(d["data"]["html"]).read_text()
    for section in ("## Results", "## Decisions and approvals", "## Timeline", "## Provenance", "## Workflow",
                    "## Node details"):
        assert section in md, section
    assert "**calc** — computed the energy" in md
    assert "- **Plan** approve by `test:pytest`" in md and "looks right" in md
    assert "rationale: PBE because the task said so" in md
    assert "agent session `" in md and "$0.2500" in md
    assert "```mermaid" in md
    assert html.startswith("<!doctype html>") and "<h2>Provenance</h2>" in html
    ext = set(re.findall(r'(?:src|href)=["\'](https?://[^"\']+)', html)) | set(re.findall(r'from ["\'](https?://[^"\']+)', html))
    assert ext == {"https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"}, ext
    assert "<script src" not in html and "<link" not in html

    # audit
    code, d, _ = ff("audit", rid)
    assert code == 0
    a = d["data"]
    assert a["schema"] == "forgeflow.audit/1"
    assert a["run"]["id"] == rid and a["run"]["status"] == "succeeded"
    assert a["plan"]["approved_by"] == "test:pytest" and a["plan"]["generation"] == 0
    assert a["nodes"]["calc"]["attempts"][0]["usage"]["cost_usd"] == pytest.approx(0.25)
    assert a["nodes"]["post"]["needs"] == ["calc"]
    assert a["gates"]["plan"]["decision"] == "approve"
    code, _, p = ff("audit", rid, json_out=False)
    assert code == 0 and json.loads(p.stdout)["schema"] == "forgeflow.audit/1"


def test_approve_without_wait_continues_in_background_driver(agent_plan):
    plan_path, _ = agent_plan
    code, d, _ = ff("run", plan_path)
    rid = d["data"]["run_id"]
    code, d, _ = ff("approve", rid)
    assert code == 0 and d["ok"] is True
    code, d, _ = ff("wait", rid, "--timeout", "60")
    assert code == 0 and d["data"]["status"] == "succeeded"


def test_run_yes_failed_agent_exits_1_with_next_hints(fake, tmp_path):
    f = fake("claude", [{"text": "Invalid API key · Please run /login", "is_error": True, "exit": 1}])
    p = write_plan(tmp_path / "p.yaml", make_plan([agent_node("calc", f.harness())], pid="cli-fail"))
    code, d, _ = ff("run", p, "--yes")
    assert code == 1
    assert d["ok"] is False and d["data"]["status"] == "failed"
    rid = d["data"]["run_id"]
    assert f"forgeflow show {rid} calc" in d["next"] and f"forgeflow rerun {rid} calc" in d["next"]
    (node,) = d["data"]["nodes"]
    assert node["error"]["error_class"] == "auth"
    code, d, _ = ff("wait", rid)
    assert code == 1
    code, _, p = ff("show", rid, "calc", json_out=False)
    assert code == 0 and "error [auth]" in p.stdout


def test_reject_plan(agent_plan):
    plan_path, f = agent_plan
    code, d, _ = ff("run", plan_path)
    rid = d["data"]["run_id"]
    code, d, _ = ff("reject", rid, "--text", "use PBEsol")
    assert code == 0
    code, d, _ = ff("status", rid)
    assert d["data"]["status"] == "rejected" and d["data"]["reason"] == "use PBEsol"
    code, d, _ = ff("wait", rid)
    assert code == 1
    assert f.calls() == []
    code, d, _ = ff("log", rid)
    assert code == 0 and [e["eventType"] for e in d["data"]][-1] == "plan.rejected"
    code, _, p = ff("log", rid, json_out=False)
    assert code == 0 and "plan REJECTED by test:pytest: use PBEsol" in p.stdout


def test_amendment_gate_via_cli(fake, tmp_path):
    amd = {"rationale": "need a check", "ops": [{"op": "add", "nodes": [
        {"id": "check", "kind": "shell", "needs": ["scout"], "run": "echo checked"}]}]}
    f = fake("script", [{"answer": {"summary": "scouted", "amendment": amd}}])
    p = write_plan(tmp_path / "p.yaml", make_plan([agent_node("scout", f.harness(), effects={"amend": True})],
                                                   pid="cli-amend"))
    code, d, _ = ff("run", p, "--yes")
    assert code == 3 and d["ok"] is True and d["data"]["status"] == "parked"
    rid = d["data"]["run_id"]
    (gate,) = d["data"]["open_gates"]
    assert gate["subject"] == "amendment" and gate["id"].startswith("amend-")
    assert any(n.startswith(f"forgeflow answer {rid} {gate['id']}") for n in d["next"])
    code, d, _ = ff("show", rid, "--gate", gate["id"])
    assert code == 0 and "PLAN CHANGE proposed by agent:scout" in d["data"]["message"]
    assert d["data"]["amendment_id"]
    code, d, _ = ff("approve", rid, gate["id"], "--wait")
    assert code == 0 and d["data"]["status"] == "succeeded" and d["data"]["generation"] == 1
    code, d, _ = ff("report", rid)
    md = Path(d["data"]["md"]).read_text()
    section = md.split("## How the plan changed", 1)[1].split("\n## ", 1)[0]
    assert "agent:scout" in section and "`check`" in section and "need a check" in section


# ====================================================================== error envelopes

def test_errors_without_runs_and_unknown_things(agent_plan):
    code, d, _ = ff("status")
    assert code == 2 and d["error"]["code"] == "no_runs"
    plan_path, _ = agent_plan
    code, d, _ = ff("run", plan_path)
    rid = d["data"]["run_id"]
    for args, err in [(("show", rid, "--gate", "nope"), "gate_not_found"),
                      (("show", rid, "ghost"), "node_not_found"),
                      (("logs", rid, "calc"), "no_attempt"),
                      (("output", rid, "calc"), "no_result"),
                      (("status", "no-such-run"), "run_not_found"),
                      (("answer", rid, "plan", "maybe"), "bad_decision")]:
        code, d, _ = ff(*args)
        assert code == 2, args
        assert_envelope(d, ok=False)
        assert d["error"]["code"] == err, (args, d)


def test_usage_errors_exit_2(tmp_path):
    code, _, p = ff("frobnicate", json_out=False)
    assert code == 2
    code, _, p = ff("run", json_out=False)
    assert code == 2
    code, _, p = ff(json_out=False)
    assert code == 2


# regression (was an uncaught KeyError -> traceback, no envelope; fixed upstream during testing)
def test_output_missing_key_is_a_clean_error(agent_plan):
    plan_path, _ = agent_plan
    code, d, _ = ff("run", plan_path, "--yes")
    rid = d["data"]["run_id"]
    code, d, p = ff("output", rid, "calc", "no.such.key")
    assert code == 2 and d is not None and d["ok"] is False, p.stderr[-300:]
    assert d["error"]["code"] == "no_key"


# regression (was an uncaught StopIteration; fixed upstream during testing)
def test_logs_unknown_attempt_is_a_clean_error(agent_plan):
    plan_path, _ = agent_plan
    code, d, _ = ff("run", plan_path, "--yes")
    rid = d["data"]["run_id"]
    code, d, p = ff("logs", rid, "calc", "--attempt", "9")
    assert code == 2 and d is not None and d["ok"] is False, p.stderr[-300:]
    assert d["error"]["code"] == "no_attempt"
    code, d, _ = ff("logs", rid, "calc", "--attempt", "1")
    assert code == 0 and "=== turn 0 (main) ===" in d["data"]["text"]


# ====================================================================== skill + doctor

def test_skill_install_targets(tmp_path, ff_home):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home)}
    code, d, _ = ff("skill", "claude", env=env)
    assert code == 0 and d["data"] == [str(home / ".claude" / "skills" / "forgeflow" / "SKILL.md")]
    skill = (home / ".claude" / "skills" / "forgeflow" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: forgeflow") and "FORGEFLOW_INSIDE_RUN" in skill
    assert "Never approve on your own judgement" in skill
    assert (home / ".claude" / "skills" / "forgeflow" / "PLAN_REFERENCE.md").exists()
    code, d, _ = ff("skill", "all", env=env)
    assert code == 0 and len(d["data"]) == 3
    for sub in (".claude", ".codex", ".agents"):
        assert (home / sub / "skills" / "forgeflow" / "SKILL.md").exists()
    code, d, _ = ff("skill", "project", env=env)
    assert code == 0
    assert (ff_home / ".claude" / "skills" / "forgeflow" / "SKILL.md").exists()
    assert (ff_home / ".agents" / "skills" / "forgeflow" / "SKILL.md").exists()
    code, d, _ = ff("init", tmp_path / "proj", "--skill", "project", env=env)
    assert code == 0


def test_doctor_with_fake_harnesses_on_path(tmp_path):
    bindir = tmp_path / "doctor-bin"
    bindir.mkdir()
    for exe in ("claude", "codex", "pi"):
        p = bindir / exe
        p.write_text(f"#!/bin/sh\necho '{exe} 9.9.9-fake'\n")
        p.chmod(0o755)
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    code, d, _ = ff("doctor", env=env)
    assert code == 0
    checks = {c["check"]: c for c in d["data"]}
    for exe in ("claude", "codex", "pi"):
        assert checks[f"harness {exe}"]["ok"] is True
        assert f"{exe} 9.9.9-fake" in checks[f"harness {exe}"]["detail"]
    assert checks["python"]["ok"] is True and checks["project"]["ok"] is True


def test_agent_inside_a_node_cannot_answer_the_users_gate(tmp_path, ff_home):
    import sys
    from agentkit import FF_BIN
    from forgeflow.engine import Engine
    from forgeflow.rundir import RunPaths
    code = ("import os, subprocess\n"
            f"subprocess.run([{str(FF_BIN)!r}, 'answer', os.environ['FF_RUN_ID'], 'review#a1', 'approve',"
            " '--text', 'lgtm', '--no-continue'], capture_output=True)\n"
            "print('{\"summary\": \"did my task\"}')\n")
    plan = make_plan([{"id": "review", "kind": "gate", "message": "Human: is the structure right?"},
                      agent_node("sneaky", {"name": "script", "command": [sys.executable, "-c", code]})],
                     pid="inside-guard")
    p = write_plan(tmp_path / "p.yaml", plan)
    rc, d, _ = ff("run", p, "--yes")
    rid = d["data"]["run_id"]
    st = Engine(RunPaths(ff_home, rid)).state()
    assert st.nodes["sneaky"].status == "succeeded"
    assert st.gates["review#a1"].status == "open", f"gate answered from inside a node by {st.gates['review#a1'].by}"
