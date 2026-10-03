"""Claude Code (``claude -p``) adapter.

Invocation (verified against claude 2.1.284; conventions from orc/Smithers/Eleforge):
``claude -p --output-format stream-json --verbose --permission-mode bypassPermissions
[--model M] [--effort E] (--session-id UUID | --resume ID) [--json-schema S]
[--append-system-prompt …] [--allowed-tools …] [--disallowed-tools …] [--max-budget-usd X]``
with the prompt on stdin (no argv length limit, nothing secret in ``ps``).

Pitfalls handled (Smithers/Eleforge lessons): nested-session env vars are removed; the final answer
is the ``result`` event (never concatenated stream text); usage/session-limit banners that exit 0
are classified as quota; ``is_error``/error subtypes fail even with exit code 0.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import (AgentRequest, Harness, HarnessResult, Invocation, classify_error, extract_json, is_limit_banner,
                   iter_jsonl, parse_retry_after, readable_error, summarize_tool)

PERMISSION = {"bypass": "bypassPermissions", "edits": "acceptEdits", "default": "default", "plan": "plan"}

# Variables that bind a process to the *outer* Claude Code session (when flower itself is driven by
# Claude Code / the desktop app). A nested `claude -p` that inherits them refuses to start ("nested
# session") or authenticates against the parent's host bridge and gets 401. User-level configuration
# (CLAUDE_CONFIG_DIR, CLAUDE_CODE_USE_BEDROCK/VERTEX, CLAUDE_CODE_OAUTH_TOKEN, …) is kept.
_SESSION_BOUND = re.compile(
    r"^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|CLAUDE_SSH_.*|CLAUDE_AGENT_SDK_VERSION|CLAUDE_PREVIEW_.*|"
    r"CLAUDE_CODE_(ENTRYPOINT|SSE_PORT|SESSION_.*|SESSION_ID|HOST_.*|CHILD_SESSION|MESSAGING_.*|EXECPATH|"
    r"SDK_HAS_HOST_AUTH_REFRESH|DESKTOP_.*|TERMINAL_MCP_TOOLS|REPORT_FINDINGS|ENABLE_SDK_FILE_CHECKPOINTING|"
    r"EMIT_TOOL_USE_SUMMARIES|ENABLE_ASK_USER_QUESTION_TOOL|DISABLE_CRON|EAGER_FLUSH|OAUTH_SCOPES|"
    r"ACCOUNT_UUID|ORGANIZATION_UUID|USER_EMAIL|DISABLE_TERMINAL_TITLE))$")


def session_bound_env(environ: dict | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    names = [k for k in env if _SESSION_BOUND.match(k)]
    if env.get("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH") and "ANTHROPIC_BASE_URL" in env:
        names.append("ANTHROPIC_BASE_URL")  # host-managed proxy of the parent session, not the user's gateway
    return sorted(names)


class ClaudeHarness(Harness):
    name = "claude"
    executable = "claude"
    native_schema = True
    can_resume = True

    def build(self, req: AgentRequest) -> Invocation:
        argv = [self.resolve_exe(req), *(req.command[1:] if req.command else []), "-p",
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", PERMISSION.get(req.permission, req.permission)]
        if req.model:
            argv += ["--model", req.model]
        if req.effort:
            argv += ["--effort", str(req.effort)]
        if req.resume:
            argv += ["--resume", req.resume]
        elif req.session_id:
            argv += ["--session-id", req.session_id]
        if req.output_schema:
            argv += ["--json-schema", json.dumps(req.output_schema, separators=(",", ":"))]
        if req.system_append:
            argv += ["--append-system-prompt", req.system_append]
        if req.tools_allow:
            argv += ["--allowed-tools", ",".join(req.tools_allow)]
        if req.tools_deny:
            argv += ["--disallowed-tools", ",".join(req.tools_deny)]
        if req.budget_usd:
            argv += ["--max-budget-usd", str(req.budget_usd)]
        argv += list(req.extra_args or [])
        return Invocation(argv=argv, env=dict(req.env or {}), stdin_text=req.prompt, unset_env=session_bound_env())

    def _scan(self, proc_dir: Path) -> dict:
        info: dict = {"actions": [], "result": None, "session": None, "model": None, "assistant_text": ""}
        for ev in iter_jsonl(proc_dir / "stdout.log"):
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                info["session"] = ev.get("session_id") or info["session"]
                info["model"] = ev.get("model")
            elif t == "assistant":
                msg = ev.get("message") or {}
                for block in msg.get("content") or []:
                    if block.get("type") == "tool_use":
                        info["actions"].append({"kind": "tool", "title": summarize_tool(block.get("name", "tool"),
                                                                                        block.get("input"))})
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        info["assistant_text"] = block["text"]
                        info["actions"].append({"kind": "message", "title": block["text"].strip().splitlines()[0][:100]})
                info["actions"] = info["actions"][-30:]
            elif t == "result":
                info["result"] = ev
                info["session"] = ev.get("session_id") or info["session"]
        return info

    def live(self, proc_dir: Path) -> dict:
        info = self._scan(proc_dir)
        acts = info["actions"]
        return {"last_action": acts[-1]["title"] if acts else None, "actions": len(acts),
                "session": info["session"], "model": info["model"]}

    def parse(self, proc_dir: Path, exit_info: dict) -> HarnessResult:
        info = self._scan(proc_dir)
        res = info["result"]
        stderr = ""
        try:
            stderr = (proc_dir / "stderr.log").read_text(errors="replace")[-4000:]
        except FileNotFoundError:
            pass
        if res is None:
            text = info["assistant_text"] or stderr
            cls = classify_error(text + "\n" + stderr) or "harness"
            return HarnessResult(ok=False, text=info["assistant_text"], session_id=info["session"],
                                 error_class=cls, actions=info["actions"],
                                 message=f"claude produced no result event (exit {exit_info.get('returncode')})"
                                         + (f": {stderr.strip().splitlines()[-1][:300]}" if stderr.strip() else ""),
                                 retry_after_s=parse_retry_after(text + stderr))
        u = res.get("usage") or {}
        usage = {"input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                 + (u.get("cache_creation_input_tokens") or 0),
                 "output_tokens": u.get("output_tokens") or 0,
                 "cache_read_tokens": u.get("cache_read_input_tokens") or 0,
                 "cache_write_tokens": u.get("cache_creation_input_tokens") or 0,
                 "cost_usd": res.get("total_cost_usd"), "cost_source": "harness",
                 "turns": res.get("num_turns"), "duration_ms": res.get("duration_ms")}
        text = res.get("result") or ""
        sub = res.get("subtype") or ""
        out = HarnessResult(ok=not res.get("is_error") and sub == "success", text=text,
                            structured=res.get("structured_output"), session_id=res.get("session_id") or info["session"],
                            usage=usage, stop_reason=sub, actions=info["actions"], turns=res.get("num_turns"),
                            model=info["model"])
        banner = out.ok and res.get("structured_output") is None and is_limit_banner(text) and extract_json(text) is None
        if banner:
            out.ok, out.error_class, out.message = False, "quota", text.strip()[:300]
            out.retry_after_s = parse_retry_after(text)
        if not out.ok and not out.error_class:
            if sub == "error_max_budget_usd":
                out.error_class, out.message = "budget", "agent stopped: budget limit reached"
            elif sub == "error_max_turns":
                out.error_class, out.message = "max_turns", "agent stopped: max turns reached"
            else:
                out.error_class = classify_error(text + "\n" + stderr) or "agent_error"
                out.message = readable_error(text or stderr or sub or "claude reported an error").strip()[:400]
                out.retry_after_s = parse_retry_after(text + stderr)
        return out
