"""Harness adapter interface (agent-runners.md §7.3, Smithers BaseCliAgent, yak AgentAdapter).

An adapter is pure translation: ``build`` turns an :class:`AgentRequest` into an argv/env/stdin
invocation (journalled as ``argv.json`` for audit), and ``parse`` turns the captured stdout/stderr +
exit record into a :class:`HarnessResult`. Spawning, timeouts, cancellation, schema repair and retry
belong to the engine so adapters stay small and testable against recorded fixtures.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentRequest:
    prompt: str
    cwd: str
    proc_dir: str
    model: str | None = None
    effort: str | None = None
    system_append: str | None = None
    output_schema: dict | None = None      # enforced natively when the harness supports it
    session_id: str | None = None          # pre-assigned id for a fresh session
    resume: str | None = None              # session to continue (repair turns)
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    max_turns: int | None = None
    budget_usd: float | None = None
    permission: str = "bypass"             # bypass | edits | default
    extra_args: list[str] = field(default_factory=list)
    command: list[str] | None = None       # override executable (or the full argv for `script`)
    env: dict = field(default_factory=dict)


@dataclass
class Invocation:
    argv: list[str]
    env: dict = field(default_factory=dict)
    stdin_text: str | None = None
    unset_env: list[str] = field(default_factory=list)


@dataclass
class HarnessResult:
    ok: bool
    text: str = ""
    structured: Any = None
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    error_class: str | None = None
    message: str | None = None
    stop_reason: str | None = None
    actions: list[dict] = field(default_factory=list)   # [{kind, title}] most recent last (bounded)
    turns: int | None = None
    model: str | None = None
    retry_after_s: float | None = None


class Harness:
    name = "base"
    executable = "true"
    native_schema = False      # can enforce a JSON schema on the final answer
    can_resume = False         # can continue a session (used for schema repair)

    def resolve_exe(self, req: AgentRequest) -> str:
        if req.command:
            return req.command[0]
        return shutil.which(self.executable) or self.executable

    def build(self, req: AgentRequest) -> Invocation:  # pragma: no cover - interface
        raise NotImplementedError

    def parse(self, proc_dir: Path, exit_info: dict) -> HarnessResult:  # pragma: no cover - interface
        raise NotImplementedError

    def live(self, proc_dir: Path) -> dict:
        """Cheap view of progress while running (last action, turns). Never authoritative."""
        return {}


# ---------------------------------------------------------------- shared helpers

QUOTA_PATTERNS = [
    r"you'?ve hit your (session |usage |weekly )?limit", r"usage limit (reached|exceeded)",
    r"out of usage credits", r"rate[_ ]limit(ed| exceeded)", r"too many requests", r"\b429\b",
    r"insufficient[_ ]quota", r"quota exceeded",
]
AUTH_PATTERNS = [
    r"invalid api key", r"please run /login", r"not logged in", r"authentication[_ ]failed",
    r"oauth token (has )?expired", r"\b401\b", r"invalid_authentication", r"api key .*expired",
    r"unauthorized",
]
CONFIG_PATTERNS = [r"unknown model", r"model .* (not found|does not exist)", r"unknown option", r"unexpected argument",
                   r"error: unrecognized", r"invalid value for", r"requires a newer version", r"not supported when using",
                   r"model is not supported", r"invalid_request_error"]


def readable_error(text: str) -> str:
    """Pull the human message out of JSON-shaped API errors (`{"error": {"message": …}}`)."""
    t = (text or "").strip()
    for cand in (t, t[t.find("{"):] if "{" in t else ""):
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        for path in (("error", "message"), ("message",), ("error",)):
            v = obj
            for k in path:
                v = v.get(k) if isinstance(v, dict) else None
            if isinstance(v, str) and v.strip():
                status = obj.get("status") if isinstance(obj, dict) else None
                return (f"HTTP {status}: " if status else "") + v.strip()
    return t


BANNER_PATTERNS = [r"^\s*you'?ve hit your (session |usage |weekly )?limit", r"^\s*(claude )?usage limit reached",
                   r"^\s*you'?re out of (usage )?credits", r"usage limit reached\b.*\breset",
                   r"^\s*(rate limit|too many requests)[^\n]{0,80}(try again|retry|reset)"]


def is_limit_banner(text: str) -> bool:
    """A usage/session-limit banner printed *instead of* an answer (exit 0). Anchored and narrow on purpose."""
    t = (text or "").strip().lower()
    return len(t) < 400 and any(re.search(p, t) for p in BANNER_PATTERNS)


def classify_error(text: str) -> str | None:
    t = (text or "").lower()
    for p in AUTH_PATTERNS:
        if re.search(p, t):
            return "auth"
    for p in QUOTA_PATTERNS:
        if re.search(p, t):
            return "quota"
    for p in CONFIG_PATTERNS:
        if re.search(p, t):
            return "config"
    return None


def parse_retry_after(text: str) -> float | None:
    m = re.search(r"retry after (\d+)\s*s", text or "", re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"try again in (\d+)\s*(second|minute|hour)", text or "", re.I)
    if m:
        return float(m.group(1)) * {"second": 1, "minute": 60, "hour": 3600}[m.group(2).lower()]
    return None


def iter_jsonl(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return


MAX_SCAN = 256 * 1024


def _balanced_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every top-level balanced {...} in one forward, string-aware pass (linear time)."""
    spans, stack = [], []
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and stack:
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            s = stack.pop()
            if not stack:
                spans.append((s, i + 1))
    return spans


def extract_json(text: str) -> Any:
    """Final-answer JSON extraction. The agent is told to END with one JSON object, so the candidate that
    ends last wins: the whole text, the last ```json fence, or the last balanced top-level object."""
    if not text:
        return None
    t = text.strip().lstrip("\ufeff")
    if t[:1] in "{[":
        try:
            return json.loads(t)
        except ValueError:
            pass
    offset = max(0, len(text) - MAX_SCAN)
    tail = text[offset:]
    cands: list[tuple[int, str]] = []
    for m in re.finditer(r"```(?:json|JSON)?\s*\n(.*?)```", tail, re.S):
        cands.append((m.end(), m.group(1).strip()))
    for s, e in _balanced_spans(tail)[-50:]:
        cands.append((e, tail[s:e]))
    for _, body in sorted(cands, key=lambda c: c[0], reverse=True)[:60]:
        try:
            return json.loads(body)
        except ValueError:
            continue
    return None


def summarize_tool(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        if args.get(key):
            v = str(args[key]).replace("\n", " ")
            return f"{name}: {v[:90]}"
    return name
