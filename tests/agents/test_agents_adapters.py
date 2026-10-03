"""Unit tests: harness adapters' pure ``build()`` and ``parse()`` against recorded-style fixtures."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentkit import FIXTURES
from flower.harness import HARNESSES, get_harness
from flower.harness.base import AgentRequest
from flower.transcript import render_transcript


def req(tmp_path, **kw) -> AgentRequest:
    base = dict(prompt="Do the thing.\nWith a second line and 'quotes' and $VARS.", cwd=str(tmp_path / "work"),
                proc_dir=str(tmp_path / "a1" / "proc"))
    base.update(kw)
    return AgentRequest(**base)


def proc_with(tmp_path, fixture: str, stderr: str = "", last: str | None = None) -> Path:
    p = tmp_path / "a1" / "proc"
    p.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / fixture, p / "stdout.log")
    (p / "stderr.log").write_text(stderr)
    if last is not None:
        (p / "last.txt").write_text(last)
    return p


def test_registry_and_unknown_harness():
    assert set(HARNESSES) == {"claude", "codex", "pi", "script"}
    with pytest.raises(ValueError, match="unknown harness"):
        get_harness("gemini")


# ====================================================================== claude build

def test_claude_build_unsets_only_session_bound_env(tmp_path, monkeypatch):
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(k, "x")
    for k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(k, "keep")
    monkeypatch.delenv("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", raising=False)
    inv = get_harness("claude").build(req(tmp_path, command=["claude"]))
    assert {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SESSION_ID"} <= set(inv.unset_env)
    assert not {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BASE_URL"} & set(inv.unset_env)
    # the parent's host-managed proxy is dropped only when the parent is a host-auth session
    monkeypatch.setenv("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "1")
    inv = get_harness("claude").build(req(tmp_path, command=["claude"]))
    assert "ANTHROPIC_BASE_URL" in inv.unset_env
    # other harnesses do not touch the environment
    for h in ("codex", "pi"):
        assert get_harness(h).build(req(tmp_path, command=[h])).unset_env == []


def test_claude_build_fresh_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    schema = {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}
    r = req(tmp_path, model="sonnet", effort="high", session_id="sid-123", output_schema=schema,
            system_append="Be terse.", tools_allow=["Bash", "Read"], tools_deny=["WebFetch"], budget_usd=2.5,
            extra_args=["--setting-sources", "project"], command=["/opt/fake/claude", "--debug-x"])
    inv = get_harness("claude").build(r)
    a = inv.argv
    assert a[:2] == ["/opt/fake/claude", "--debug-x"]
    assert a[2:6] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert a[a.index("--permission-mode") + 1] == "bypassPermissions"
    assert a[a.index("--model") + 1] == "sonnet"
    assert a[a.index("--effort") + 1] == "high"
    assert a[a.index("--session-id") + 1] == "sid-123"
    assert "--resume" not in a
    assert json.loads(a[a.index("--json-schema") + 1]) == schema
    assert a[a.index("--append-system-prompt") + 1] == "Be terse."
    assert a[a.index("--allowed-tools") + 1] == "Bash,Read"
    assert a[a.index("--disallowed-tools") + 1] == "WebFetch"
    assert a[a.index("--max-budget-usd") + 1] == "2.5"
    assert a[-2:] == ["--setting-sources", "project"]
    # prompt goes via stdin, never argv
    assert inv.stdin_text == r.prompt
    assert all(r.prompt not in x for x in a)
    assert set(inv.unset_env) >= {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}


def test_claude_build_resume_wins_over_session_id(tmp_path):
    inv = get_harness("claude").build(req(tmp_path, session_id="new", resume="old-sess"))
    assert inv.argv[inv.argv.index("--resume") + 1] == "old-sess"
    assert "--session-id" not in inv.argv


@pytest.mark.parametrize("perm,flag", [("bypass", "bypassPermissions"), ("edits", "acceptEdits"),
                                       ("default", "default"), ("plan", "plan"), ("dontAsk", "dontAsk")])
def test_claude_permission_mapping(tmp_path, perm, flag):
    inv = get_harness("claude").build(req(tmp_path, permission=perm))
    assert inv.argv[inv.argv.index("--permission-mode") + 1] == flag


def test_claude_build_minimal_has_no_optional_flags(tmp_path):
    inv = get_harness("claude").build(req(tmp_path, command=["claude"]))
    for f in ("--model", "--effort", "--json-schema", "--append-system-prompt", "--allowed-tools",
              "--disallowed-tools", "--max-budget-usd", "--resume", "--session-id"):
        assert f not in inv.argv


def test_build_passes_env_through_without_mutating_request(tmp_path):
    r = req(tmp_path, env={"A": "1"}, command=["claude"])
    inv = get_harness("claude").build(r)
    inv.env["B"] = "2"
    assert r.env == {"A": "1"}


# ====================================================================== codex build

def test_codex_build_fresh(tmp_path):
    r = req(tmp_path, model="gpt-5-codex", effort="high", system_append="SYS", permission="bypass",
            command=["/x/codex"], extra_args=["--add-dir", "/data"])
    inv = get_harness("codex").build(r)
    a = inv.argv
    assert a[0] == "/x/codex" and a[1:3] == ["exec", "--json"]
    assert "--skip-git-repo-check" in a
    assert a[a.index("-c") + 1] == 'approval_policy="never"'
    assert a[a.index("--sandbox") + 1] == "danger-full-access"
    assert a[a.index("-C") + 1] == r.cwd
    assert a[a.index("-o") + 1] == str(Path(r.proc_dir) / "last.txt")
    assert a[a.index("-m") + 1] == "gpt-5-codex"
    assert "model_reasoning_effort=high" in a
    assert "--output-schema" not in a, "codex --output-schema blocks tool calls; must not be used for agent nodes"
    assert a[-1] == "-"
    assert a[-3:-1] == ["--add-dir", "/data"]
    assert inv.stdin_text.startswith("SYS\n\n") and inv.stdin_text.endswith(r.prompt)


@pytest.mark.parametrize("perm,sandbox", [("edits", "workspace-write"), ("default", "workspace-write"),
                                          ("readonly", "read-only"), ("weird", "workspace-write")])
def test_codex_sandbox_mapping(tmp_path, perm, sandbox):
    a = get_harness("codex").build(req(tmp_path, permission=perm)).argv
    assert a[a.index("--sandbox") + 1] == sandbox


def test_codex_build_resume(tmp_path):
    r = req(tmp_path, resume="thread-9", system_append="SYS", command=["codex"])
    inv = get_harness("codex").build(r)
    a = inv.argv
    assert a[1:4] == ["exec", "resume", "thread-9"]
    for banned in ("--sandbox", "-C", "--output-schema", "--cd"):
        assert banned not in a, f"`codex exec resume` rejects {banned}"
    assert "-o" in a and a[-1] == "-"
    assert inv.stdin_text == r.prompt  # system prompt is not re-sent on resume


# ====================================================================== pi build

def test_pi_build_fresh(tmp_path):
    r = req(tmp_path, session_id="ps-1", model="anthropic/claude-sonnet-4-5", effort="medium",
            system_append="SYS", tools_allow=["read", "bash"], command=["/x/pi"])
    inv = get_harness("pi").build(r)
    a = inv.argv
    assert a[0] == "/x/pi" and a[1:3] == ["--mode", "json"]
    assert a[a.index("--session-dir") + 1] == str(Path(r.proc_dir).parent / "pi-sessions")
    assert "--no-approve" in a
    assert a[a.index("--session-id") + 1] == "ps-1"
    assert a[a.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    assert a[a.index("--thinking") + 1] == "medium"
    assert a[a.index("--append-system-prompt") + 1] == "SYS"
    assert a[a.index("--tools") + 1] == "read,bash"
    assert inv.stdin_text == r.prompt


def test_pi_build_resume_reuses_session_dir(tmp_path):
    r0 = req(tmp_path, session_id="ps-1")
    r1 = req(tmp_path, resume="ps-1", proc_dir=str(tmp_path / "a1" / "proc-r1"))
    a0 = get_harness("pi").build(r0).argv
    a1 = get_harness("pi").build(r1).argv
    assert a1[a1.index("--session") + 1] == "ps-1"
    assert "--session-id" not in a1
    assert a0[a0.index("--session-dir") + 1] == a1[a1.index("--session-dir") + 1]


# ====================================================================== script build

def test_script_build(tmp_path):
    schema = {"type": "object"}
    r = req(tmp_path, command=["python3", "agent.py", "--x"], extra_args=["--y"], output_schema=schema, model="m1",
            env={"K": "V"})
    inv = get_harness("script").build(r)
    assert inv.argv == ["python3", "agent.py", "--x", "--y"]
    assert inv.stdin_text == r.prompt
    assert json.loads(inv.env["FLOWER_OUTPUT_SCHEMA"]) == schema
    assert inv.env["FLOWER_MODEL"] == "m1" and inv.env["K"] == "V"


def test_script_build_requires_command(tmp_path):
    with pytest.raises(ValueError, match="command"):
        get_harness("script").build(req(tmp_path))


# ====================================================================== claude parse

def test_claude_parse_success(tmp_path):
    p = proc_with(tmp_path, "claude_success.jsonl")
    hr = get_harness("claude").parse(p, {"returncode": 0})
    assert hr.ok and hr.error_class is None
    assert hr.session_id == "1f0c9e7a-3b7e-4a51-9d55-6c1e1d2f0a11"
    assert hr.structured == {"energy": -10.84, "summary": "relaxed Si", "rationale": "PBE, 8x8x8"}
    assert hr.text.endswith('"rationale": "PBE, 8x8x8"}')
    assert hr.usage["cost_usd"] == pytest.approx(0.4321)
    assert hr.usage["input_tokens"] == 1200 + 300 + 5000
    assert hr.usage["cache_read_tokens"] == 5000 and hr.usage["cache_write_tokens"] == 300
    assert hr.usage["output_tokens"] == 800 and hr.turns == 4 and hr.usage["turns"] == 4
    assert hr.model == "claude-sonnet-4-5"
    titles = [x["title"] for x in hr.actions]
    assert any(t.startswith("Bash: cat POSCAR") for t in titles)
    assert any(t.startswith("Write: /work/result.txt") for t in titles)


def test_claude_live_view(tmp_path):
    p = proc_with(tmp_path, "claude_no_result.jsonl")
    live = get_harness("claude").live(p)
    assert live["session"] == "1f0c9e7a-3b7e-4a51-9d55-6c1e1d2f0a11"
    assert live["actions"] == 2 and live["last_action"].startswith("Bash")


def test_claude_parse_exit0_usage_banner_is_quota(tmp_path):
    hr = get_harness("claude").parse(proc_with(tmp_path, "claude_banner.jsonl"), {"returncode": 0})
    assert not hr.ok and hr.error_class == "quota"
    assert "session limit" in hr.message


def test_claude_parse_is_error_auth(tmp_path):
    hr = get_harness("claude").parse(proc_with(tmp_path, "claude_auth.jsonl"), {"returncode": 1})
    assert not hr.ok and hr.error_class == "auth"


def test_claude_parse_max_turns_and_budget(tmp_path):
    hr = get_harness("claude").parse(proc_with(tmp_path, "claude_max_turns.jsonl"), {"returncode": 1})
    assert (hr.ok, hr.error_class, hr.stop_reason) == (False, "max_turns", "error_max_turns")
    assert hr.usage["cost_usd"] == 1.5
    hr = get_harness("claude").parse(proc_with(tmp_path / "b", "claude_budget.jsonl"), {"returncode": 1})
    assert (hr.ok, hr.error_class) == (False, "budget")


def test_claude_parse_rate_limited_error_with_retry_after(tmp_path):
    hr = get_harness("claude").parse(proc_with(tmp_path, "claude_rate_limited.jsonl"), {"returncode": 1})
    assert not hr.ok and hr.error_class == "quota"
    assert hr.retry_after_s == 120.0


def test_claude_parse_no_result_event_uses_stderr(tmp_path):
    p = proc_with(tmp_path, "claude_no_result.jsonl", stderr="boot\nError: Invalid API key · Please run /login\n")
    hr = get_harness("claude").parse(p, {"returncode": 1})
    assert not hr.ok and hr.error_class == "auth"
    assert "no result event" in hr.message and "Invalid API key" in hr.message
    assert hr.session_id == "1f0c9e7a-3b7e-4a51-9d55-6c1e1d2f0a11"


def test_claude_parse_empty_output(tmp_path):
    p = tmp_path / "proc"
    p.mkdir()
    hr = get_harness("claude").parse(p, {"returncode": 0})
    assert not hr.ok and hr.error_class == "harness"


def test_claude_long_answer_mentioning_rate_limits_is_not_a_banner(tmp_path):
    text = ("Implemented retry logic for HTTP 429 Too Many Requests and 'rate limit exceeded' responses. " * 6
            + '\n{"summary": "added backoff", "rationale": "x"}')
    p = tmp_path / "proc"
    p.mkdir()
    (p / "stdout.log").write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                              "result": text, "session_id": "s", "usage": {}}) + "\n")
    assert get_harness("claude").parse(p, {"returncode": 0}).ok


def test_claude_short_valid_answer_mentioning_429_is_not_quota(tmp_path):
    answer = {"summary": "Handled HTTP 429 Too Many Requests with exponential backoff", "rationale": "rate limit exceeded"}
    p = tmp_path / "proc"
    p.mkdir()
    (p / "stdout.log").write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                              "result": json.dumps(answer), "structured_output": answer,
                                              "session_id": "s", "usage": {}}) + "\n")
    hr = get_harness("claude").parse(p, {"returncode": 0})
    assert hr.ok, f"valid structured answer classified as {hr.error_class}: {hr.message}"


# ====================================================================== codex parse

def test_codex_parse_success_prefers_last_message_file(tmp_path):
    p = proc_with(tmp_path, "codex_success.jsonl", last='FINAL FROM FILE {"ok": true, "summary": "from file"}')
    hr = get_harness("codex").parse(p, {"returncode": 0})
    assert hr.ok and hr.session_id == "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
    assert hr.text.startswith("FINAL FROM FILE")
    assert hr.usage["input_tokens"] == 24000 and hr.usage["cache_read_tokens"] == 20000
    assert hr.usage["output_tokens"] == 1500 and hr.usage["cost_source"] == "unpriced"
    kinds = [a["kind"] for a in hr.actions]
    assert {"command", "file_change", "tool", "message"} <= set(kinds)
    assert any(a["title"] == "add /work/INCAR" for a in hr.actions)


def test_codex_parse_success_falls_back_to_agent_message(tmp_path):
    p = proc_with(tmp_path, "codex_success.jsonl")
    hr = get_harness("codex").parse(p, {"returncode": 0})
    assert hr.ok and hr.text.startswith("Wrote inputs.")


def test_codex_reconnecting_error_is_benign(tmp_path):
    hr = get_harness("codex").parse(proc_with(tmp_path, "codex_success.jsonl"), {"returncode": 0})
    assert hr.ok and hr.error_class is None


def test_codex_parse_quota_turn_failed(tmp_path):
    hr = get_harness("codex").parse(proc_with(tmp_path, "codex_quota.jsonl"), {"returncode": 1})
    assert not hr.ok and hr.error_class == "quota"
    assert hr.retry_after_s == 3 * 3600
    assert hr.session_id == "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


def test_codex_parse_crash_nonzero_without_answer(tmp_path):
    p = proc_with(tmp_path, "codex_crash.jsonl", stderr="Error: unexpected argument '--frob' found\n")
    hr = get_harness("codex").parse(p, {"returncode": 2})
    assert not hr.ok and hr.error_class == "config"


def test_codex_parse_auth_from_stderr(tmp_path):
    p = proc_with(tmp_path, "codex_crash.jsonl", stderr="ERROR: 401 Unauthorized: please run codex login\n")
    hr = get_harness("codex").parse(p, {"returncode": 1})
    assert not hr.ok and hr.error_class == "auth"


# ====================================================================== pi parse

def test_pi_parse_success(tmp_path):
    hr = get_harness("pi").parse(proc_with(tmp_path, "pi_success.jsonl"), {"returncode": 0})
    assert hr.ok and hr.session_id == "pi-sess-42" and hr.stop_reason == "stop"
    assert hr.text.startswith("All set.") and '"user"' not in hr.text  # user messages are ignored
    assert hr.usage["cost_usd"] == pytest.approx(0.010)
    assert hr.usage["input_tokens"] == 2000 and hr.usage["output_tokens"] == 400
    assert any(a["title"].startswith("bash: ls -la") for a in hr.actions)


def test_pi_parse_exit0_with_stop_reason_error_fails(tmp_path):
    hr = get_harness("pi").parse(proc_with(tmp_path, "pi_error_exit0.jsonl"), {"returncode": 0})
    assert not hr.ok and hr.stop_reason == "error"
    assert hr.error_class == "quota" and "rate_limit" in hr.message


def test_pi_parse_aborted_fails(tmp_path):
    hr = get_harness("pi").parse(proc_with(tmp_path, "pi_aborted.jsonl"), {"returncode": 0})
    assert not hr.ok and hr.error_class == "agent_error" and "aborted" in hr.message


def test_pi_parse_nonzero_exit_without_answer(tmp_path):
    p = tmp_path / "proc"
    p.mkdir()
    (p / "stdout.log").write_text("")
    hr = get_harness("pi").parse(p, {"returncode": 1})
    assert not hr.ok and "exit 1" in hr.message


# ====================================================================== script parse

def test_script_parse_strips_action_lines(tmp_path):
    p = tmp_path / "proc"
    p.mkdir()
    (p / "stdout.log").write_text('{"flower_action": "step one"}\nhello\n{"flower_action": "step two"}\n{"a": 1}\n')
    h = get_harness("script")
    hr = h.parse(p, {"returncode": 0})
    assert hr.ok and hr.text == 'hello\n{"a": 1}'
    assert [a["title"] for a in hr.actions] == ["step one", "step two"]
    assert h.live(p) == {"last_action": "step two", "actions": 2}


def test_script_parse_failure_uses_stderr(tmp_path):
    p = tmp_path / "proc"
    p.mkdir()
    (p / "stdout.log").write_text("partial\n")
    (p / "stderr.log").write_text("Traceback...\nRuntimeError: OAuth token has expired\n")
    hr = get_harness("script").parse(p, {"returncode": 3})
    assert not hr.ok and hr.error_class == "auth" and "OAuth" in hr.message


# ====================================================================== transcript rendering

def _attempt(tmp_path, harness, fixtures: list[str]) -> Path:
    adir = tmp_path / "attempt"
    adir.mkdir(parents=True)
    (adir / "prompt.md").write_text("THE PROMPT")
    for i, fx in enumerate(fixtures):
        p = adir / ("proc" if i == 0 else f"proc-r{i}")
        p.mkdir()
        shutil.copy(FIXTURES / fx, p / "stdout.log")
        (p / "argv.json").write_text(json.dumps({"argv": [harness, "--flag", "x" * 300]}))
    return adir


def test_transcript_claude(tmp_path):
    adir = _attempt(tmp_path, "claude", ["claude_success.jsonl"])
    (adir / "answer.json").write_text('{"energy": -10.84}')
    t = render_transcript(adir, "claude")
    assert "=== prompt ===\nTHE PROMPT" in t
    assert "=== turn 0 (main) ===" in t
    assert "[session 1f0c9e7a-3b7e-4a51-9d55-6c1e1d2f0a11 · model claude-sonnet-4-5]" in t
    assert "assistant: I'll inspect the inputs first." in t
    assert "  → Bash: cat POSCAR" in t
    assert "  ← Si ⏎ 1.0" in t           # list-shaped tool_result content
    assert "  ✗ permission denied" in t  # is_error tool result
    assert "[result: success · 4 turns · $0.4321]" in t
    assert "=== final structured answer ===" in t
    assert "x" * 300 not in t  # long argv items are shortened


def test_transcript_orders_repair_turns_numerically(tmp_path):
    fx = ["claude_success.jsonl"] * 12
    adir = _attempt(tmp_path, "claude", fx)
    t = render_transcript(adir, "claude")
    pos = [t.index(f"=== proc-r{i} (repair turn) ===") for i in range(1, 12)]
    assert pos == sorted(pos) and t.index("=== turn 0 (main) ===") < pos[0]


def test_transcript_codex_and_pi_and_script(tmp_path):
    t = render_transcript(_attempt(tmp_path / "c", "codex", ["codex_success.jsonl"]), "codex")
    assert "[thread 0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b]" in t
    assert "  $ bash -lc 'ls -la'" in t and "    exit 0: total 8" in t
    assert "  ✎ add /work/INCAR" in t and "(thinking) **Planning**" in t and "[turn done · usage" in t
    t = render_transcript(_attempt(tmp_path / "p", "pi", ["pi_success.jsonl"]), "pi")
    assert "assistant: All set." in t and "  → bash: ls -la /work" in t
    assert "do it" not in t  # user turn not rendered as assistant
    t = render_transcript(_attempt(tmp_path / "s", "script", ["pi_success.jsonl"]), "script")
    assert '"type": "session"' in t  # unknown harness: raw stdout


def test_transcript_missing_dir_and_stderr(tmp_path):
    assert render_transcript(tmp_path / "nope", "claude") == ""
    adir = tmp_path / "a"
    (adir / "proc").mkdir(parents=True)
    (adir / "proc" / "stderr.log").write_text("boom\n")
    t = render_transcript(adir, "claude")
    assert "(no output)" in t and "stderr:\nboom" in t
