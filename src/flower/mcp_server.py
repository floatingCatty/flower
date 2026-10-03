"""Minimal MCP server over stdio (JSON-RPC 2.0, protocol 2024-11-05), no dependencies.

Exposes the same verbs as the CLI for hosts without a shell. Decision verbs (approve / answer /
reject) are deliberately NOT exposed: an agent must not be able to approve its own plan or answer
the user's gates through a tool call (Smithers 1.0 withholds them for the same reason). The human
answers with the CLI, a gate answer file, or via the agent relaying an explicit instruction in a shell.
"""
from __future__ import annotations

import json
import subprocess
import sys

from . import __version__

TOOLS = [
    ("validate_plan", "Validate a plan YAML file; returns every issue with a fix hint.", {"path": "string"}, ["path"], True),
    ("show_plan", "Human-readable overview of a plan file (what the user approves).", {"path": "string"}, ["path"], True),
    ("start_run", "Create a run from a plan. It waits for the USER's approval (never auto-approved).",
     {"plan_path": "string", "inputs": "object"}, ["plan_path"], False),
    ("list_runs", "List runs in this project.", {}, [], True),
    ("status", "Status of a run: nodes, open decisions, cost.", {"run": "string"}, [], True),
    ("wait", "Block up to timeout seconds (max 55) until the run finishes or needs a decision.",
     {"run": "string", "timeout": "number"}, [], True),
    ("show_node", "Attempts, outputs, files, errors and rationale of one node.", {"run": "string", "node": "string"},
     ["node"], True),
    ("show_gate", "Full text of an open decision.", {"run": "string", "gate": "string"}, ["gate"], True),
    ("log", "Human timeline of what happened.", {"run": "string"}, [], True),
    ("node_logs", "Agent transcript / job output / shell logs of a node.", {"run": "string", "node": "string"},
     ["run", "node"], True),
    ("rerun", "Re-run a node and everything downstream.", {"run": "string", "node": "string", "only": "boolean"},
     ["run", "node"], False),
    ("cancel", "Cancel a run or one node.", {"run": "string", "node": "string"}, [], False),
    ("propose_amendment", "Propose a plan change (needs the user's approval).",
     {"run": "string", "rationale": "string", "ops": "array"}, ["run", "rationale", "ops"], False),
    ("signal", "Send a named signal to waiting nodes.", {"run": "string", "name": "string", "data": "object"},
     ["run", "name"], False),
    ("report", "Write report.md/report.html for a run.", {"run": "string"}, [], False),
]


def _cli(args: list[str]) -> dict:
    p = subprocess.run([sys.executable, "-m", "flower", *args, "--json"], capture_output=True, text=True, timeout=120)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": {"code": "cli", "message": (p.stderr or p.stdout)[-2000:]}}


READ_ONLY = {"on": False}


def call(name: str, a: dict) -> dict:
    run = [a["run"]] if a.get("run") else []
    if name == "validate_plan":
        return _cli(["plan", "validate", a["path"]])
    if name == "show_plan":
        return _cli(["plan", "show", a["path"]])
    if name == "start_run":
        args = ["run", a["plan_path"], "--no-prompt"]
        for k, v in (a.get("inputs") or {}).items():
            args += ["-i", f"{k}={json.dumps(v) if not isinstance(v, str) else v}"]
        return _cli(args)
    if name == "list_runs":
        return _cli(["ls"])
    if name == "status":
        return _cli(["status", *run, "--no-tick"])  # observing never advances a run
    if name == "wait":
        args = ["wait", *run, "--timeout", str(min(float(a.get("timeout") or 50), 55))]
        return _cli(args + (["--no-tick"] if READ_ONLY["on"] else []))
    if name == "show_node":
        return _cli(["show", a.get("run") or "last", a["node"]])
    if name == "show_gate":
        return _cli(["show", *run, "--gate", a["gate"]])
    if name == "log":
        return _cli(["log", *run])
    if name == "node_logs":
        return _cli(["logs", a["run"], a["node"]])
    if name == "rerun":
        return _cli(["rerun", a["run"], a["node"]] + (["--only"] if a.get("only") else []))
    if name == "cancel":
        return _cli(["cancel", *run] + (["--node", a["node"]] if a.get("node") else []))
    if name == "propose_amendment":
        import tempfile
        import yaml
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump({"rationale": a["rationale"], "ops": a["ops"]}, fh)
        return _cli(["amend", a["run"], fh.name])
    if name == "signal":
        return _cli(["signal", a["run"], a["name"]] + (["--data", json.dumps(a["data"])] if a.get("data") is not None else []))
    if name == "report":
        return _cli(["report", *run])
    return {"ok": False, "error": {"code": "unknown_tool", "message": name}}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {k: {"type": v} for k, v in props.items()}, "required": required}


def _reply(mid, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def handle(msg, tools) -> tuple[object, dict | None, dict | None] | None:
    """-> (id, result, error) or None for notifications. Never raises."""
    if not isinstance(msg, dict):
        return None, None, {"code": -32600, "message": "invalid request: expected a JSON object"}
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None  # notification
    params = msg.get("params") if msg.get("params") is not None else {}
    if not isinstance(params, dict):
        return mid, None, {"code": -32602, "message": "params must be an object"}
    if method == "initialize":
        return mid, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                     "serverInfo": {"name": "flower", "version": __version__},
                     "instructions": "Durable workflows. Decisions (plan approval, gate answers) belong to the user "
                                     "and are made with the flower CLI, never through these tools."}, None
    if method == "ping":
        return mid, {}, None
    if method == "tools/list":
        return mid, {"tools": [{"name": f"flower_{n}", "description": d, "inputSchema": _schema(p, r),
                                "annotations": {"readOnlyHint": ro}} for n, d, p, r, ro in tools]}, None
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if params.get("arguments") is not None else {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return mid, None, {"code": -32602, "message": "tools/call needs a string name and object arguments"}
        name = name[len("flower_"):] if name.startswith("flower_") else name
        if name not in {t[0] for t in tools}:
            res = {"ok": False, "error": {"code": "unknown_tool", "message": name}}
        else:
            try:
                res = call(name, args)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": {"code": "exception", "message": f"{type(exc).__name__}: {exc}"}}
        return mid, {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, default=str)}],
                     "isError": not res.get("ok", False) and res.get("error") is not None}, None
    return mid, None, {"code": -32601, "message": f"no method {method}"}


def serve(read_only: bool = False) -> int:
    READ_ONLY["on"] = read_only
    tools = [t for t in TOOLS if t[4] or not read_only]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # not JSON (stray output on the pipe): ignore rather than add noise
        try:
            out = handle(msg, tools)
        except Exception as exc:  # noqa: BLE001 - the server must survive anything a client sends
            out = (msg.get("id") if isinstance(msg, dict) else None, None, {"code": -32603, "message": str(exc)})
        if out is None:
            continue
        mid, result, error = out
        _reply(mid, result, error)
    return 0
