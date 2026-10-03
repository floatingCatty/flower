"""`forgeflow mcp`: stdio JSON-RPC (initialize, tools/list, tools/call); approval verbs must not exist."""
from __future__ import annotations

import json
import subprocess
import time

import pytest

from agentkit import FF_BIN, agent_node, events, ff, make_plan, write_plan

DECISION_WORDS = ("approve", "answer", "reject", "decide", "accept")
MUTATING = {"forgeflow_start_run", "forgeflow_rerun", "forgeflow_cancel", "forgeflow_propose_amendment",
            "forgeflow_signal", "forgeflow_report"}


def mcp(requests: list, read_only: bool = False, timeout: float = 120) -> tuple[dict, subprocess.CompletedProcess]:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in requests]
    argv = [str(FF_BIN), "mcp"] + (["--read-only"] if read_only else [])
    p = subprocess.run(argv, input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=timeout)
    out = {}
    for line in p.stdout.splitlines():
        if line.strip():
            msg = json.loads(line)
            assert msg.get("jsonrpc") == "2.0"
            out[msg["id"]] = msg
    return out, p


def call(i, name, **args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


def tool_result(msg) -> tuple[dict, bool]:
    res = msg["result"]
    (content,) = res["content"]
    assert content["type"] == "text"
    return json.loads(content["text"]), res["isError"]


INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def test_initialize_and_tool_list_withholds_decision_verbs():
    out, p = mcp([INIT, INITIALIZED, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                  {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}])
    assert p.returncode == 0
    init = out[0]["result"]
    assert init["protocolVersion"] == "2024-11-05" and init["serverInfo"]["name"] == "forgeflow"
    assert "tools" in init["capabilities"]
    assert "never through these tools" in init["instructions"]
    tools = out[1]["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"forgeflow_status", "forgeflow_validate_plan", "forgeflow_start_run", "forgeflow_wait"} <= names
    for n in names:
        assert not any(w in n for w in DECISION_WORDS), f"decision verb exposed over MCP: {n}"
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and isinstance(t["annotations"]["readOnlyHint"], bool)
        assert t["description"]
    assert all(t["annotations"]["readOnlyHint"] is False for t in tools if t["name"] in MUTATING)
    assert out[2]["result"] == {}
    assert out[3]["error"]["code"] == -32601
    assert len(out) == 4  # the notification got no response


def test_read_only_hides_mutating_tools_and_refuses_calls(tmp_path, ff_home):
    plan = write_plan(tmp_path / "p.yaml", make_plan([{"id": "a", "kind": "shell", "run": "true"}], pid="mcp-ro"))
    out, _ = mcp([INIT, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  call(2, "forgeflow_start_run", plan_path=str(plan)),
                  call(3, "forgeflow_validate_plan", path=str(plan))], read_only=True)
    names = {t["name"] for t in out[1]["result"]["tools"]}
    assert not names & MUTATING
    assert all(t["annotations"]["readOnlyHint"] for t in out[1]["result"]["tools"])
    res, is_err = tool_result(out[2])
    assert is_err and res["error"]["code"] == "unknown_tool"
    res, is_err = tool_result(out[3])
    assert not is_err and res["data"]["valid"] is True
    from forgeflow.rundir import list_runs
    assert list_runs(ff_home) == []


@pytest.mark.parametrize("name", ["forgeflow_approve", "forgeflow_answer", "forgeflow_reject", "approve",
                                  "forgeflow_answer_gate", "forgeflow_amend_approve"])
def test_decision_tools_cannot_be_called(name):
    out, _ = mcp([INIT, call(1, name, run="last", gate="plan", decision="approve")])
    res, is_err = tool_result(out[1])
    assert is_err and res["error"]["code"] == "unknown_tool"


def test_validate_start_status_flow_never_approves(fake, tmp_path, ff_home):
    f = fake("script", [{"answer": {"summary": "s"}}])
    good = write_plan(tmp_path / "good.yaml", make_plan([agent_node("a", f.harness())], pid="mcp-flow"))
    bad = write_plan(tmp_path / "bad.yaml", {"forgeflow": 1, "id": "x", "nodes": [{"id": "a", "kind": "agent"}]})
    out, p = mcp([INIT,
                  call(1, "forgeflow_validate_plan", path=str(good)),
                  call(2, "forgeflow_validate_plan", path=str(bad)),
                  call(3, "forgeflow_show_plan", path=str(good)),
                  call(4, "forgeflow_start_run", plan_path=str(good), inputs={}),
                  call(5, "forgeflow_status"),
                  call(6, "forgeflow_list_runs"),
                  call(7, "forgeflow_show_gate", gate="plan"),
                  call(8, "forgeflow_wait", timeout=1)])
    res, is_err = tool_result(out[1])
    assert not is_err and res["ok"] and res["data"]["valid"]
    res, is_err = tool_result(out[2])
    assert is_err and res["error"]["code"] == "plan_invalid" and res["error"]["details"]
    res, is_err = tool_result(out[3])
    assert not is_err and "overview" in res["data"]
    res, is_err = tool_result(out[4])
    assert not is_err and res["ok"] is True  # parked for approval is not an error
    rid = res["data"]["run_id"]
    assert res["data"]["status"] == "awaiting_approval"
    res, _ = tool_result(out[5])
    assert res["data"]["run_id"] == rid and res["data"]["status"] == "awaiting_approval"
    assert [g["id"] for g in res["data"]["open_gates"]] == ["plan"]
    res, _ = tool_result(out[6])
    assert [r["run_id"] for r in res["data"]] == [rid]
    res, _ = tool_result(out[7])
    assert res["data"]["status"] == "open"
    res, _ = tool_result(out[8])
    assert res["data"]["status"] == "awaiting_approval"
    evs = (ff_home / ".forgeflow" / "runs" / rid / "events.jsonl").read_text()
    assert "plan.approved" not in evs and "gate.answered" not in evs
    assert f.calls() == []


def test_start_run_inputs_cannot_smuggle_approval_flags(tmp_path, ff_home):
    plan = write_plan(tmp_path / "p.yaml", make_plan([{"id": "a", "kind": "shell", "run": "true"}], pid="mcp-inj",
                                                      inputs={"x": {"type": "string", "default": "d"}}))
    out, _ = mcp([INIT,
                  call(1, "forgeflow_start_run", plan_path="--yes"),
                  call(2, "forgeflow_start_run", plan_path=str(plan), inputs={"-y": "1"}),
                  call(3, "forgeflow_start_run", plan_path=str(plan), inputs={"x": "--yes"}),
                  call(4, "forgeflow_start_run", plan_path="-y" + str(plan))])
    from forgeflow.engine import Engine
    from forgeflow.rundir import RunPaths, list_runs
    for rid in list_runs(ff_home):
        st = Engine(RunPaths(ff_home, rid)).state()
        assert not st.approved, f"run {rid} got approved through MCP inputs"
    res, _ = tool_result(out[3])
    assert res["ok"] and res["data"]["status"] == "awaiting_approval"


def test_node_logs_and_report_tools(fake, tmp_path):
    f = fake("claude", [{"answer": {"summary": "agent finished", "rationale": "r"}}])
    plan = write_plan(tmp_path / "p.yaml", make_plan([agent_node("calc", f.harness())], pid="mcp-logs"))
    code, d, _ = ff("run", plan, "--yes")
    assert code == 0, d
    rid = d["data"]["run_id"]
    out, _ = mcp([INIT, call(1, "forgeflow_node_logs", run=rid, node="calc"),
                  call(2, "forgeflow_show_node", run=rid, node="calc"),
                  call(3, "forgeflow_log", run=rid),
                  call(4, "forgeflow_report", run=rid)])
    res, is_err = tool_result(out[1])
    assert not is_err and "=== turn 0 (main) ===" in res["data"]["text"]
    res, _ = tool_result(out[2])
    assert res["data"]["attempts"][0]["summary"] == "agent finished"
    res, is_err = tool_result(out[3])
    assert not is_err
    res, is_err = tool_result(out[4])
    assert not is_err and res["data"]["md"].endswith("report.md")


def test_propose_amendment_tool_opens_a_gate_but_cannot_approve(tmp_path, ff_home):
    plan = write_plan(tmp_path / "p.yaml", make_plan([{"id": "a", "kind": "shell", "run": "true"},
                                                       {"id": "w", "kind": "wait", "signal": "go", "needs": ["a"]}],
                                                      pid="mcp-amend"))
    code, d, _ = ff("run", plan, "--yes")
    assert code == 3
    rid = d["data"]["run_id"]
    ops = [{"op": "add", "nodes": [{"id": "extra", "kind": "shell", "run": "true", "needs": ["a"]}]}]
    out, _ = mcp([INIT, call(1, "forgeflow_propose_amendment", run=rid, rationale="more", ops=ops)])
    res, is_err = tool_result(out[1])
    assert not is_err and res["data"]["gate"].startswith("amend-")
    from forgeflow.engine import Engine
    from forgeflow.rundir import RunPaths
    st = Engine(RunPaths(ff_home, rid)).state()
    assert st.generation == 0 and st.gates[res["data"]["gate"]].status == "open"
    assert "extra" not in st.graph().nodes


@pytest.mark.parametrize("bad", ['[1, 2]', '42', '"hello"',
                                 json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": None}}),
                                 json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]})])
def test_malformed_requests_do_not_kill_the_server(bad):
    out, p = mcp([INIT, bad, {"jsonrpc": "2.0", "id": 99, "method": "ping"}])
    assert 99 in out, f"server died: {p.stderr[-300:]}"


def test_garbage_lines_and_notifications_are_ignored():
    out, p = mcp(["not json at all", "", {"jsonrpc": "2.0", "method": "notifications/cancelled"},
                  {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
    assert p.returncode == 0 and set(out) == {5}


def test_read_only_status_does_not_advance_the_run(fake, ff_home):
    from forgeflow.engine import create_run
    fa = fake("script", [{"answer": {"summary": "a"}}], name="a")
    fb = fake("script", [{"answer": {"summary": "b"}}], name="b")
    eng = create_run(make_plan([agent_node("a", fa.harness()), agent_node("b", fb.harness(), needs=["a"])],
                               pid="mcp-adv"), {}, root=ff_home, approve=True)
    eng.tick()  # starts a
    exit_file = eng.paths.attempt_dir("a", 1) / "proc" / "exit.json"
    deadline = time.time() + 20
    while not exit_file.exists() and time.time() < deadline:
        time.sleep(0.1)
    before = len(eng.journal.read())
    out, _ = mcp([INIT, call(1, "forgeflow_status", run=eng.paths.run_id)], read_only=True)
    assert not events(eng, "node.started", "b"), "read-only MCP status started node b"
    assert len(eng.journal.read()) == before
