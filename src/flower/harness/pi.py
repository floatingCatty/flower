"""pi (earendil-works/pi) adapter — ``pi --mode json``.

``pi --mode json --session-dir <proc>/sessions --session-id <id> --no-approve [--model provider/id]
[--thinking L] [--append-system-prompt …]`` with the prompt on stdin, which is then closed (pi waits
on an open stdin). pi exits 0 even when the agent errored, so the verdict comes from ``stopReason``
(agent-runners.md §5). Resume uses ``--session <id>`` in the same session dir.
"""
from __future__ import annotations

from pathlib import Path

from .base import AgentRequest, Harness, HarnessResult, Invocation, classify_error, iter_jsonl, summarize_tool


class PiHarness(Harness):
    name = "pi"
    executable = "pi"
    native_schema = False
    can_resume = True

    def build(self, req: AgentRequest) -> Invocation:
        sess_dir = str(Path(req.proc_dir).parent / "pi-sessions")
        argv = [self.resolve_exe(req), *(req.command[1:] if req.command else []), "--mode", "json",
                "--session-dir", sess_dir, "--no-approve" if req.permission == "bypass" else "--approve"]
        if req.resume:
            argv += ["--session", req.resume]
        elif req.session_id:
            argv += ["--session-id", req.session_id]
        if req.model:
            argv += ["--model", req.model]
        if req.effort:
            argv += ["--thinking", str(req.effort)]
        if req.system_append:
            argv += ["--append-system-prompt", req.system_append]
        if req.tools_allow:
            argv += ["--tools", ",".join(req.tools_allow)]
        argv += list(req.extra_args or [])
        return Invocation(argv=argv, env=dict(req.env or {}), stdin_text=req.prompt)

    def _scan(self, proc_dir: Path) -> dict:
        info: dict = {"actions": [], "session": None, "final": "", "usage": {"input_tokens": 0, "output_tokens": 0,
                                                                              "cost_usd": 0.0},
                      "stop": None, "error": None}
        for ev in iter_jsonl(proc_dir / "stdout.log"):
            t = ev.get("type")
            if t == "session":
                info["session"] = ev.get("id")
            elif t == "message_end":
                msg = ev.get("message") or {}
                if msg.get("role") == "assistant":
                    texts = [c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text"]
                    if any(x.strip() for x in texts):
                        info["final"] = "\n".join(texts)
                        info["actions"].append({"kind": "message", "title": info["final"].strip()[:100]})
                    u = msg.get("usage") or {}
                    info["usage"]["input_tokens"] += u.get("input") or 0
                    info["usage"]["output_tokens"] += u.get("output") or 0
                    info["usage"]["cost_usd"] += ((u.get("cost") or {}).get("total") or 0.0)
                    if msg.get("stopReason"):
                        info["stop"] = msg["stopReason"]
                    if msg.get("errorMessage"):
                        info["error"] = msg["errorMessage"]
            elif t == "tool_execution_start":
                info["actions"].append({"kind": "tool", "title": summarize_tool(ev.get("toolName", "tool"), ev.get("args"))})
            elif t == "agent_end":
                for m in ev.get("messages") or []:
                    if m.get("role") == "assistant" and m.get("stopReason"):
                        info["stop"] = m["stopReason"]
                        info["error"] = m.get("errorMessage") or info["error"]
            info["actions"] = info["actions"][-30:]
        return info

    def live(self, proc_dir: Path) -> dict:
        info = self._scan(proc_dir)
        acts = info["actions"]
        return {"last_action": acts[-1]["title"] if acts else None, "actions": len(acts), "session": info["session"]}

    def parse(self, proc_dir: Path, exit_info: dict) -> HarnessResult:
        info = self._scan(proc_dir)
        usage = dict(info["usage"])
        usage["cost_source"] = "harness"
        if info["stop"] in ("error", "aborted") or (exit_info.get("returncode") not in (0, None) and not info["final"]):
            msg = info["error"] or f"pi stopped: {info['stop'] or 'exit ' + str(exit_info.get('returncode'))}"
            return HarnessResult(ok=False, text=info["final"], session_id=info["session"], usage=usage,
                                 error_class=classify_error(msg) or "agent_error", message=msg[:400],
                                 actions=info["actions"], stop_reason=info["stop"])
        return HarnessResult(ok=True, text=info["final"], session_id=info["session"], usage=usage,
                             actions=info["actions"], stop_reason=info["stop"] or "complete")
