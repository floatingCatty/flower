"""Codex CLI (``codex exec --json``) adapter.

``codex exec --json --skip-git-repo-check -c approval_policy="never" --sandbox <mode> [-m M]
[-c model_reasoning_effort=E] -o <proc>/last.txt -`` with the prompt on stdin; repair turns use
``codex exec resume <thread_id> … -`` (resume rejects --sandbox/--output-schema/--cd).
``--output-schema`` is NOT used for agentic nodes: it constrains the model to a final JSON and blocks
tool calls (Smithers CodexAgent note), so the schema is prompt-injected and extracted instead.
"""
from __future__ import annotations

from pathlib import Path

from .base import (AgentRequest, Harness, HarnessResult, Invocation, classify_error, iter_jsonl, parse_retry_after,
                   readable_error)

SANDBOX = {"bypass": "danger-full-access", "edits": "workspace-write", "default": "workspace-write",
           "readonly": "read-only"}


class CodexHarness(Harness):
    name = "codex"
    executable = "codex"
    native_schema = False
    can_resume = True

    def build(self, req: AgentRequest) -> Invocation:
        exe = [self.resolve_exe(req), *(req.command[1:] if req.command else [])]
        last = str(Path(req.proc_dir) / "last.txt")
        if req.resume:
            argv = exe + ["exec", "resume", req.resume, "--json", "--skip-git-repo-check",
                          "-c", 'approval_policy="never"', "-o", last]
        else:
            argv = exe + ["exec", "--json", "--skip-git-repo-check", "-c", 'approval_policy="never"',
                          "--sandbox", SANDBOX.get(req.permission, "workspace-write"), "-C", req.cwd, "-o", last]
        if req.model:
            argv += ["-m", req.model]
        if req.effort:
            argv += ["-c", f"model_reasoning_effort={req.effort}"]
        argv += list(req.extra_args or [])
        argv.append("-")
        prompt = req.prompt if not req.system_append or req.resume else f"{req.system_append}\n\n{req.prompt}"
        return Invocation(argv=argv, env=dict(req.env or {}), stdin_text=prompt)

    def _scan(self, proc_dir: Path) -> dict:
        info: dict = {"actions": [], "thread": None, "final": "", "usage": {}, "failed": None, "errors": []}
        for ev in iter_jsonl(proc_dir / "stdout.log"):
            t = ev.get("type")
            if t == "thread.started":
                info["thread"] = ev.get("thread_id")
            elif t in ("item.completed", "item.started"):
                item = ev.get("item") or {}
                it = item.get("type") or item.get("item_type")
                if it == "agent_message" and t == "item.completed":
                    info["final"] = item.get("text") or info["final"]
                    info["actions"].append({"kind": "message", "title": (item.get("text") or "").strip()[:100]})
                elif it == "command_execution" and t == "item.started":
                    info["actions"].append({"kind": "command", "title": str(item.get("command"))[:100]})
                elif it == "file_change" and t == "item.completed":
                    for ch in item.get("changes") or []:
                        info["actions"].append({"kind": "file_change", "title": f"{ch.get('kind')} {ch.get('path')}"})
                elif it in ("mcp_tool_call", "web_search") and t == "item.started":
                    info["actions"].append({"kind": "tool", "title": f"{it}: {item.get('tool') or item.get('query') or ''}"[:100]})
                info["actions"] = info["actions"][-30:]
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                acc = info["usage"]
                acc["input_tokens"] = acc.get("input_tokens", 0) + (u.get("input_tokens") or 0)
                acc["cache_read_tokens"] = acc.get("cache_read_tokens", 0) + (u.get("cached_input_tokens") or 0)
                acc["output_tokens"] = acc.get("output_tokens", 0) + (u.get("output_tokens") or 0)
            elif t == "turn.failed":
                info["failed"] = (ev.get("error") or {}).get("message") or "turn failed"
            elif t == "error":
                msg = ev.get("message") or ""
                if "reconnecting" not in msg.lower():
                    info["errors"].append(msg)
        return info

    def live(self, proc_dir: Path) -> dict:
        info = self._scan(proc_dir)
        acts = info["actions"]
        return {"last_action": acts[-1]["title"] if acts else None, "actions": len(acts), "session": info["thread"]}

    def parse(self, proc_dir: Path, exit_info: dict) -> HarnessResult:
        info = self._scan(proc_dir)
        final = info["final"]
        try:
            final = (proc_dir / "last.txt").read_text() or final
        except FileNotFoundError:
            pass
        usage = dict(info["usage"])
        usage["cost_source"] = "unpriced"
        stderr = ""
        try:
            stderr = (proc_dir / "stderr.log").read_text(errors="replace")[-3000:]
        except FileNotFoundError:
            pass
        failure = info["failed"] or (info["errors"][-1] if info["errors"] else None)
        rc = exit_info.get("returncode")
        if failure or (rc not in (0, None) and not final):
            text = readable_error(failure or stderr or f"codex exited {rc}")
            return HarnessResult(ok=False, text=final, session_id=info["thread"], usage=usage,
                                 error_class=classify_error(text + stderr) or "agent_error", message=text.strip()[:400],
                                 actions=info["actions"], retry_after_s=parse_retry_after(text))
        return HarnessResult(ok=True, text=final, session_id=info["thread"], usage=usage, actions=info["actions"],
                             stop_reason="complete")
