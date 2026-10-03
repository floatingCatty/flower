"""Render an agent attempt's raw harness stream as a readable transcript."""
from __future__ import annotations

import json
from pathlib import Path

from .harness.base import iter_jsonl, summarize_tool
from .util import read_json, truncate


def _claude(path: Path) -> list[str]:
    out = []
    for ev in iter_jsonl(path):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            out.append(f"[session {ev.get('session_id')} · model {ev.get('model')}]")
        elif t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append("assistant: " + b["text"].strip())
                elif b.get("type") == "tool_use":
                    out.append("  → " + summarize_tool(b.get("name", "tool"), b.get("input")))
        elif t == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    flag = "  ✗ " if b.get("is_error") else "  ← "
                    out.append(flag + truncate(str(c).replace("\n", " ⏎ "), 200))
        elif t == "result":
            out.append(f"[result: {ev.get('subtype')} · {ev.get('num_turns')} turns · "
                       f"${ev.get('total_cost_usd') or 0:.4f}]")
    return out


def _codex(path: Path) -> list[str]:
    out = []
    for ev in iter_jsonl(path):
        t = ev.get("type")
        item = ev.get("item") or {}
        it = item.get("type")
        if t == "thread.started":
            out.append(f"[thread {ev.get('thread_id')}]")
        elif t == "item.completed" and it == "agent_message":
            out.append("assistant: " + (item.get("text") or "").strip())
        elif t == "item.completed" and it == "reasoning":
            out.append("  (thinking) " + truncate((item.get("text") or "").replace("\n", " "), 200))
        elif t == "item.started" and it == "command_execution":
            out.append("  $ " + str(item.get("command")))
        elif t == "item.completed" and it == "command_execution":
            out.append(f"    exit {item.get('exit_code')}: " + truncate((item.get("aggregated_output") or "").replace("\n", " ⏎ "), 200))
        elif t == "item.completed" and it == "file_change":
            for ch in item.get("changes") or []:
                out.append(f"  ✎ {ch.get('kind')} {ch.get('path')}")
        elif t == "turn.completed":
            out.append(f"[turn done · usage {json.dumps(ev.get('usage'))}]")
        elif t in ("turn.failed", "error"):
            out.append(f"[error] {ev.get('message') or (ev.get('error') or {}).get('message')}")
    return out


def _pi(path: Path) -> list[str]:
    out = []
    for ev in iter_jsonl(path):
        t = ev.get("type")
        if t == "message_end":
            m = ev.get("message") or {}
            if m.get("role") == "assistant":
                txt = "\n".join(c.get("text", "") for c in m.get("content") or [] if c.get("type") == "text").strip()
                if txt:
                    out.append("assistant: " + txt)
        elif t == "tool_execution_start":
            out.append("  → " + summarize_tool(ev.get("toolName", "tool"), ev.get("args")))
    return out


def render_transcript(attempt_dir: Path, harness: str) -> str:
    parts = []
    prompt = attempt_dir / "prompt.md"
    if prompt.exists():
        parts.append("=== prompt ===\n" + prompt.read_text())
    procs = sorted([p for p in attempt_dir.iterdir() if p.is_dir() and p.name.startswith("proc")],
                   key=lambda p: (len(p.name), p.name)) if attempt_dir.exists() else []
    for proc in procs:
        argv = read_json(proc / "argv.json") or {}
        title = "=== turn 0 (main) ===" if proc.name == "proc" else f"=== {proc.name} (repair turn) ==="
        parts.append(title)
        if argv.get("argv"):
            parts.append("$ " + " ".join(str(a) if len(str(a)) < 120 else str(a)[:117] + "…" for a in argv["argv"]))
        fn = {"claude": _claude, "codex": _codex, "pi": _pi}.get(harness)
        lines = fn(proc / "stdout.log") if fn else (proc / "stdout.log").read_text(errors="replace").splitlines() \
            if (proc / "stdout.log").exists() else []
        parts.append("\n".join(lines) if lines else "(no output)")
        err = proc / "stderr.log"
        if err.exists() and err.stat().st_size:
            parts.append("stderr:\n" + err.read_text(errors="replace")[-3000:])
    ans = attempt_dir / "answer.json"
    if ans.exists():
        parts.append("=== final structured answer ===\n" + ans.read_text())
    return "\n\n".join(parts)
