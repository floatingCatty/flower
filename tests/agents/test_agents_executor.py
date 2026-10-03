"""End-to-end agent nodes through the real engine + detached runner, driven by fake harness CLIs."""
from __future__ import annotations

import json
import time

import pytest

from agentkit import agent_node, events, last_attempt, make_plan
from forgeflow.executors.agent import answer_schema

GOOD = {"energy": -5.25, "summary": "computed the energy", "rationale": "PBE because the task said so"}
OUT = {"energy": "number"}


def failed_payload(eng, nid):
    evs = events(eng, "node.failed", nid)
    assert evs, f"{nid} did not fail"
    return evs[-1]["payload"]


def attempt_dir(eng, nid, n=1):
    return eng.paths.attempt_dir(nid, n)


# ====================================================================== happy paths per harness

def test_claude_happy_path_argv_env_and_contract(fake, run_plan, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    f = fake("claude", [{"answer": GOOD}])
    plan = make_plan([agent_node("calc", f.harness(model="sonnet", effort="high", tools={"allow": ["Bash"]},
                                                   budget_usd=3),
                                 prompt="Compute the energy of Si. Unicode ok: 能量 ✓",
                                 system="You are careful.", outputs=OUT, inputs={"topic": "si"})])
    eng, st = run_plan(plan)
    assert st.status == "succeeded", st.status_reason
    a = last_attempt(st, "calc")
    assert a.outputs == {"energy": -5.25}
    assert a.summary == "computed the energy" and a.rationale == "PBE because the task said so"
    assert a.usage["cost_usd"] == pytest.approx(0.25) and a.usage["cost_source"] == "harness"
    assert a.usage["input_tokens"] == 115 and a.usage["output_tokens"] == 50 and a.usage["turns"] == 3
    assert a.usage["harness"] == "claude" and a.usage["model"] == "sonnet"
    assert st.cost()["usd"] == pytest.approx(0.25)

    (call,) = f.calls()
    argv = call["argv"]
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "high"
    sid = argv[argv.index("--session-id") + 1]
    assert a.session == sid
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["required"] == ["energy", "summary"]
    assert argv[argv.index("--append-system-prompt") + 1] == "You are careful."
    assert argv[argv.index("--allowed-tools") + 1] == "Bash"
    assert argv[argv.index("--max-budget-usd") + 1] == "3"
    # prompt via stdin, with the node contract block
    stdin = call["stdin"]
    assert stdin.startswith("Compute the energy of Si. Unicode ok: 能量 ✓")
    assert "## forgeflow node contract" in stdin and "You are node `calc`" in stdin
    assert "Do not run `forgeflow` commands yourself" in stdin
    assert '"topic": "si"' in stdin and "amendment" not in stdin
    assert all("Compute the energy" not in x for x in argv)
    # environment: nested-session vars removed, recursion guard + node env set
    env = call["env"]
    assert env["CLAUDECODE"] is None and env["CLAUDE_CODE_ENTRYPOINT"] is None
    assert env["FORGEFLOW_INSIDE_RUN"] == "1" and env["FF_NODE_ID"] == "calc" and env["FF_ATTEMPT"] == "1"
    assert env["FF_IN_TOPIC"] == "si"
    assert call["cwd"] == str(attempt_dir(eng, "calc") / "work")
    # journalled artefacts
    adir = attempt_dir(eng, "calc")
    argv_json = json.loads((adir / "proc" / "argv.json").read_text())
    assert argv_json["harness"] == "claude" and "CLAUDECODE" in argv_json["unset"]
    assert json.loads((adir / "answer.json").read_text()) == GOOD
    assert (adir / "prompt.md").read_text() == stdin
    assert json.loads((adir / "inputs.json").read_text()) == {"topic": "si"}
    assert [e["payload"]["session_id"] for e in events(eng, "agent.session", "calc")][0] == sid


def test_claude_structured_output_is_preferred_over_text(fake, run_plan):
    f = fake("claude", [{"text": "I am done, no JSON in prose.", "structured": {"energy": 1.0, "summary": "s"}}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), outputs=OUT)]))
    assert st.status == "succeeded"
    assert last_attempt(st, "a").outputs == {"energy": 1.0}


def test_claude_native_schema_can_be_disabled_and_is_absent_without_outputs(fake, run_plan):
    f1 = fake("claude", [{"answer": GOOD}], name="c1")
    f2 = fake("claude", [{"answer": {"summary": "no outputs"}}], name="c2")
    plan = make_plan([agent_node("a", f1.harness(native_schema=False), outputs=OUT),
                      agent_node("b", f2.harness())])
    eng, st = run_plan(plan)
    assert st.status == "succeeded"
    assert "--json-schema" not in f1.calls()[0]["argv"]
    assert "--json-schema" not in f2.calls()[0]["argv"]
    assert last_attempt(st, "b").summary == "no outputs"


def test_codex_happy_path(fake, run_plan):
    f = fake("codex", [{"answer": GOOD, "thread": "thread-xyz", "reconnect": True}])
    plan = make_plan([agent_node("c", f.harness(model="gpt-5-codex", permission="edits"), outputs=OUT,
                                 system="SYS-PROMPT")])
    eng, st = run_plan(plan)
    assert st.status == "succeeded", st.status_reason
    a = last_attempt(st, "c")
    assert a.outputs == {"energy": -5.25}
    assert a.session == "thread-xyz"
    assert a.usage["input_tokens"] == 100 and a.usage["output_tokens"] == 30
    assert a.usage.get("cost_usd") is None  # codex reports tokens only
    assert st.cost()["complete"] is False
    argv = f.calls()[0]["argv"]
    assert argv[:2] == ["exec", "--json"] and argv[-1] == "-"
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == str(attempt_dir(eng, "c") / "work")
    assert argv[argv.index("-o") + 1] == str(attempt_dir(eng, "c") / "proc" / "last.txt")
    assert "--output-schema" not in argv
    assert f.calls()[0]["stdin"].startswith("SYS-PROMPT\n\nDo the task.")


def test_pi_happy_path(fake, run_plan):
    f = fake("pi", [{"answer": GOOD, "cost": 0.0125}])
    eng, st = run_plan(make_plan([agent_node("p", f.harness(model="anthropic/x", effort="low"), outputs=OUT)]))
    assert st.status == "succeeded", st.status_reason
    a = last_attempt(st, "p")
    argv = f.calls()[0]["argv"]
    assert argv[:2] == ["--mode", "json"] and "--no-approve" in argv
    assert argv[argv.index("--session-id") + 1] == a.session
    assert argv[argv.index("--thinking") + 1] == "low"
    assert argv[argv.index("--session-dir") + 1] == str(attempt_dir(eng, "p") / "pi-sessions")
    assert a.usage["cost_usd"] == pytest.approx(0.0125)
    assert a.outputs == {"energy": -5.25}


def test_script_happy_path_env_and_live_actions(fake, run_plan):
    f = fake("script", [{"answer": GOOD, "action": "running step 1"}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(model="tiny"), outputs=OUT)]))
    assert st.status == "succeeded"
    call = f.calls()[0]
    assert call["argv"] == []  # the command is used verbatim
    assert call["env"]["FF_MODEL"] == "tiny" and call["env"]["FF_OUTPUT_SCHEMA"] is None  # no native schema
    assert last_attempt(st, "s").usage["cost_usd"] == 0.0


@pytest.mark.parametrize("fmt", ["bare", "fenced", "only"])
def test_answer_formats_are_all_accepted(fake, run_plan, fmt):
    f = fake("script", [{"answer": GOOD, "format": fmt}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs=OUT)]))
    assert st.status == "succeeded" and last_attempt(st, "s").outputs == {"energy": -5.25}


# ====================================================================== repair turns

def test_claude_repair_resumes_same_session(fake, run_plan):
    f = fake("claude", [{"text": "I finished but forgot the JSON."}, {"answer": GOOD, "format": "only"}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), outputs=OUT)]))
    assert st.status == "succeeded", st.status_reason
    c0, c1 = f.calls()
    sid = c0["argv"][c0["argv"].index("--session-id") + 1]
    assert "--resume" not in c0["argv"]
    assert c1["argv"][c1["argv"].index("--resume") + 1] == sid
    assert "--session-id" not in c1["argv"]
    assert "did not satisfy the forgeflow output contract" in c1["stdin"]
    assert "the reply did not end with a JSON object" in c1["stdin"]
    assert "## forgeflow node contract" not in c1["stdin"]  # resumed: short correction only
    a = last_attempt(st, "a")
    assert a.repairs == 1 and a.session == sid
    assert a.usage["cost_usd"] == pytest.approx(0.5)  # both turns are billed
    assert a.usage["turns"] == 6
    rep = events(eng, "agent.repair", "a")
    assert len(rep) == 1 and rep[0]["payload"]["n"] == 1
    assert (attempt_dir(eng, "a") / "proc-r1" / "argv.json").exists()


def test_codex_repair_uses_exec_resume_thread(fake, run_plan):
    f = fake("codex", [{"text": "no json", "thread": "thread-777"}, {"answer": GOOD}])
    eng, st = run_plan(make_plan([agent_node("c", f.harness(), outputs=OUT, system="SYS")]))
    assert st.status == "succeeded", st.status_reason
    c0, c1 = f.calls()
    assert c0["argv"][:2] == ["exec", "--json"]
    assert c1["argv"][:3] == ["exec", "resume", "thread-777"]
    assert "--sandbox" not in c1["argv"] and "-C" not in c1["argv"]
    assert c1["argv"][c1["argv"].index("-o") + 1].endswith("proc-r1/last.txt")
    assert not c1["stdin"].startswith("SYS")
    assert last_attempt(st, "c").usage["input_tokens"] == 200


def test_pi_repair_uses_session_flag(fake, run_plan):
    f = fake("pi", [{"text": "nope"}, {"answer": GOOD}])
    eng, st = run_plan(make_plan([agent_node("p", f.harness(), outputs=OUT)]))
    assert st.status == "succeeded", st.status_reason
    c0, c1 = f.calls()
    sid = c0["argv"][c0["argv"].index("--session-id") + 1]
    assert c1["argv"][c1["argv"].index("--session") + 1] == sid
    assert last_attempt(st, "p").usage["cost_usd"] == pytest.approx(0.006)


def test_script_repair_reprompts_with_original_task(fake, run_plan):
    f = fake("script", [{"text": "first try: energy is about -5", "answer": None}, {"answer": GOOD}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), prompt="ORIGINAL TASK TEXT", outputs=OUT)]))
    assert st.status == "succeeded", st.status_reason
    c0, c1 = f.calls()
    assert c0["argv"] == c1["argv"] == []
    assert c1["stdin"].startswith("ORIGINAL TASK TEXT")
    assert "## forgeflow node contract" in c1["stdin"]
    assert "Your previous answer (head and tail):\nfirst try: energy is about -5" in c1["stdin"]
    assert "did not satisfy the forgeflow output contract" in c1["stdin"]


def test_repair_reports_type_errors(fake, run_plan):
    f = fake("claude", [{"answer": {"energy": "minus five", "summary": "s"}}, {"answer": GOOD}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(native_schema=False), outputs=OUT)]))
    assert st.status == "succeeded"
    assert "energy: 'minus five' is not of type 'number'" in f.calls()[1]["stdin"]
    assert "energy" in events(eng, "agent.repair", "a")[0]["payload"]["reason"]


def test_non_object_answer_triggers_repair(fake, run_plan):
    f = fake("script", [{"text": "[1, 2, 3]"}, {"answer": GOOD}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs=OUT)]))
    assert st.status == "succeeded"
    assert "did not end with a JSON object" in f.calls()[1]["stdin"]


def test_never_valid_becomes_schema_invalid_after_repair_budget(fake, run_plan):
    f = fake("claude", [{"text": "still no json"}])
    plan = make_plan([agent_node("a", f.harness(), outputs=OUT, repair_attempts=2, retry={"max_attempts": 3})])
    eng, st = run_plan(plan)
    assert st.status == "failed"
    assert len(f.calls()) == 3  # main turn + 2 repairs, and NOT retried as a task
    p = failed_payload(eng, "a")
    assert p["error_class"] == "schema_invalid" and p["retryable"] is False
    assert "never matched the output contract" in p["message"]
    assert len(st.nodes["a"].attempts) == 1
    assert last_attempt(st, "a").usage["cost_usd"] == pytest.approx(0.75)


def test_repair_attempts_zero_fails_immediately(fake, run_plan):
    f = fake("script", [{"text": "no json"}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs=OUT, repair_attempts=0)]))
    assert failed_payload(eng, "s")["error_class"] == "schema_invalid"
    assert len(f.calls()) == 1


def test_schema_invalid_details_keep_the_tail_of_long_output(fake, run_plan):
    long = "line of reasoning\n" * 400 + "FINAL-MARKER: I could not produce the JSON."
    f = fake("script", [{"text": long}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs=OUT, repair_attempts=0)]))
    assert "FINAL-MARKER" in failed_payload(eng, "s")["details"]["text_tail"]


def test_declared_output_named_summary_can_be_satisfied(fake, run_plan):
    f = fake("script", [{"answer": {"summary": "the summary IS the output", "rationale": "r"}}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs={"summary": "string"}, repair_attempts=0)]))
    assert st.status == "succeeded", st.status_reason
    assert last_attempt(st, "s").outputs.get("summary") == "the summary IS the output"


def test_missing_summary_falls_back_to_first_line(fake, run_plan):
    f = fake("script", [{"text": 'Short headline here\nmore\n{"energy": 2}'}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs=OUT)]))
    assert st.status == "succeeded"
    assert last_attempt(st, "s").summary == "Short headline here"


# ====================================================================== failure classes

def test_claude_is_error_generic(fake, run_plan):
    f = fake("claude", [{"text": "Something went wrong in the tool loop", "is_error": True,
                         "subtype": "error_during_execution", "exit": 1}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), outputs=OUT)]))
    p = failed_payload(eng, "a")
    assert p["error_class"] == "agent_error" and "went wrong" in p["message"]
    assert p["details"]["stop_reason"] == "error_during_execution"
    assert len(f.calls()) == 1  # an errored turn is not "repaired"


def test_claude_max_turns_is_non_retryable(fake, run_plan):
    f = fake("claude", [{"text": "", "is_error": True, "subtype": "error_max_turns"}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(max_turns=5), retry={"max_attempts": 3})]))
    p = failed_payload(eng, "a")
    assert p["error_class"] == "max_turns" and p["retryable"] is False
    assert len(f.calls()) == 1


def test_quota_banner_exit0_is_retryable_quota(fake, run_plan):
    f = fake("claude", [{"text": "You've hit your usage limit · resets 3pm"}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), outputs=OUT)]))
    p = failed_payload(eng, "a")
    assert p["error_class"] == "quota_retry" and p["retryable"] is True
    assert "usage limit" in p["message"]


def test_quota_banner_retry_honours_retry_after_then_succeeds(fake, run_plan):
    f = fake("claude", [{"text": "Usage limit reached. Please try again in 1 second."}, {"answer": GOOD}])
    plan = make_plan([agent_node("a", f.harness(), outputs=OUT, retry={"max_attempts": 2, "backoff": "0s"})])
    eng, st = run_plan(plan)
    assert st.status == "succeeded", st.status_reason
    rs = events(eng, "node.retry_scheduled", "a")
    assert len(rs) == 1 and "quota_retry" in rs[0]["payload"]["reason"]
    fail_t = events(eng, "node.failed", "a")[0]["occurredAtIso"]
    assert rs[0]["payload"]["not_before"] > fail_t  # waited at least retry-after
    assert [a.status for a in st.nodes["a"].attempts] == ["failed", "succeeded"]


def test_quota_banner_does_not_fail_the_node_by_default(fake, run_plan):
    f = fake("claude", [{"text": "You've hit your session limit · resets in 3 hours"}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), outputs=OUT)]), timeout=8)
    assert st.nodes["a"].status != "failed"


def test_auth_error_is_not_retried(fake, run_plan):
    f = fake("claude", [{"text": "Invalid API key · Please run /login", "is_error": True, "exit": 1}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), retry={"max_attempts": 3, "on": ["auth", "agent_error"]})]))
    p = failed_payload(eng, "a")
    assert p["error_class"] == "auth" and p["retryable"] is False
    assert len(f.calls()) == 1 and len(st.nodes["a"].attempts) == 1


def test_codex_turn_failed_auth(fake, run_plan):
    f = fake("codex", [{"fail": "401 Unauthorized: token expired, run codex login", "exit": 1}])
    eng, st = run_plan(make_plan([agent_node("c", f.harness())]))
    p = failed_payload(eng, "c")
    assert p["error_class"] == "auth" and p["retryable"] is False


def test_codex_quota_error_event(fake, run_plan):
    f = fake("codex", [{"error_event": "You've hit your usage limit. Try again in 2 hours.", "exit": 1}])
    eng, st = run_plan(make_plan([agent_node("c", f.harness())]))
    p = failed_payload(eng, "c")
    assert p["error_class"] == "quota_retry" and p["details"]["retry_after_s"] == 7200


def test_pi_exit0_with_stop_reason_error_fails(fake, run_plan):
    f = fake("pi", [{"text": "", "stop": "error", "error_message": "provider exploded: 500 internal"}])
    eng, st = run_plan(make_plan([agent_node("p", f.harness(), outputs=OUT)]))
    p = failed_payload(eng, "p")
    assert p["error_class"] == "agent_error" and "provider exploded" in p["message"]
    assert p["details"]["stop_reason"] == "error"
    assert len(f.calls()) == 1


def test_pi_aborted_with_valid_looking_json_still_fails(fake, run_plan):
    f = fake("pi", [{"answer": GOOD, "stop": "aborted"}])
    eng, st = run_plan(make_plan([agent_node("p", f.harness(), outputs=OUT)]))
    assert failed_payload(eng, "p")["error_class"] == "agent_error"


def test_script_nonzero_exit_classified_from_stderr(fake, run_plan):
    f = fake("script", [{"answer": GOOD, "exit": 2, "stderr": "fatal: quota exceeded for project"}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs=OUT)]))
    p = failed_payload(eng, "s")
    assert p["error_class"] == "quota_retry"


@pytest.mark.parametrize("flavor", ["claude", "script"])
def test_missing_harness_executable_fails_once_and_says_so(fake, run_plan, tmp_path, flavor):
    f = fake(flavor, [{"answer": GOOD}])
    h = f.harness()
    h["command"] = [str(tmp_path / "does-not-exist" / flavor)]
    eng, st = run_plan(make_plan([agent_node("a", h, retry={"max_attempts": 3})]))
    p = failed_payload(eng, "a")
    assert p["retryable"] is False
    assert len(st.nodes["a"].attempts) == 1
    assert "not found" in (p["message"] + json.dumps(p.get("details"))).lower() or "no such file" in p["message"].lower()


@pytest.mark.parametrize("flavor", ["claude", "script"])
def test_missing_harness_executable_is_a_spawn_error(fake, run_plan, tmp_path, flavor):
    f = fake(flavor, [{"answer": GOOD}])
    h = f.harness()
    h["command"] = [str(tmp_path / "does-not-exist" / flavor)]
    eng, st = run_plan(make_plan([agent_node("a", h)]))
    assert failed_payload(eng, "a")["error_class"] == "spawn"


def test_script_without_command_fails_cleanly_at_start(run_plan):
    eng, st = run_plan(make_plan([agent_node("s", {"name": "script"})]))
    p = failed_payload(eng, "s")
    assert p["error_class"] == "spawn" and "command" in p["message"]


# ====================================================================== timeouts, size, cancel

def test_hanging_harness_is_killed_by_idle_timeout(fake, run_plan):
    f = fake("claude", [{"answer": GOOD, "hang": 60}])
    t0 = time.time()
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), timeout={"idle": "1s"})]), timeout=30)
    assert time.time() - t0 < 20
    p = failed_payload(eng, "a")
    assert p["error_class"] == "idle_timeout"
    ex = json.loads((attempt_dir(eng, "a") / "proc" / "exit.json").read_text())
    assert ex["idle_timeout"] is True


def test_total_timeout(fake, run_plan):
    f = fake("codex", [{"answer": GOOD, "hang": 60}])
    eng, st = run_plan(make_plan([agent_node("c", f.harness(), timeout={"total": "1s"})]), timeout=30)
    assert failed_payload(eng, "c")["error_class"] == "timeout"


def test_huge_claude_stream_still_finds_final_result(fake, run_plan):
    f = fake("claude", [{"answer": GOOD, "noise_kb": 6000}])  # ~6 MB of tool_use events before the result
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), outputs=OUT)]), timeout=60)
    assert st.status == "succeeded", st.status_reason
    assert (attempt_dir(eng, "a") / "proc" / "stdout.log").stat().st_size > 6_000_000
    assert last_attempt(st, "a").outputs == {"energy": -5.25}


def test_huge_script_stdout_keeps_the_tail_answer(fake, run_plan):
    f = fake("script", [{"answer": GOOD, "noise_kb": 4000}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness(), outputs=OUT)]), timeout=60)
    assert st.status == "succeeded", st.status_reason


def test_cancel_running_agent_kills_the_harness(fake, ff_home):
    from forgeflow.engine import create_run
    f = fake("claude", [{"answer": GOOD, "hang": 120}])
    eng = create_run(make_plan([agent_node("a", f.harness())]), {}, root=ff_home, approve=True)
    eng.drive(timeout=2)
    assert eng.state().nodes["a"].status == "running"
    eng.cancel(node="a", reason="test")
    deadline = time.time() + 25
    while time.time() < deadline and eng.state().nodes["a"].status == "running":
        eng.tick()
        time.sleep(0.3)
    st = eng.state()
    assert st.nodes["a"].status == "cancelled"
    ex = json.loads((attempt_dir(eng, "a") / "proc" / "exit.json").read_text())
    assert ex["cancelled"] is True


# ====================================================================== contract plumbing

def test_declared_files_are_hashed_and_missing_files_fail_contract(fake, run_plan):
    f1 = fake("script", [{"answer": {"summary": "wrote it"}, "files": {"out/report.md": "# hi\n"}}], name="w")
    f2 = fake("script", [{"answer": {"summary": "forgot it"}}], name="m")
    plan = make_plan([agent_node("w", f1.harness(), files={"report": "out/report.md"}),
                      agent_node("m", f2.harness(), files={"report": "out/report.md"})])
    eng, st = run_plan(plan)
    fw = last_attempt(st, "w").files["report"]
    assert fw["bytes"] == 5 and fw["sha256"] and fw["path"].endswith("out/report.md")
    assert "Files you must create" in f1.calls()[0]["stdin"] and "`out/report.md`" in f1.calls()[0]["stdin"]
    p = failed_payload(eng, "m")
    assert p["error_class"] == "contract" and "missing declared file" in p["message"]


def test_upstream_results_and_references_reach_the_prompt(fake, run_plan):
    fa = fake("script", [{"answer": {"energy": 7, "summary": "UPSTREAM-SUMMARY"}}], name="a")
    fb = fake("script", [{"answer": {"summary": "used it"}}], name="b")
    plan = make_plan([agent_node("a", fa.harness(), outputs=OUT),
                      agent_node("b", fb.harness(), prompt="Energy was ${a.outputs.energy}; go.", needs=["a"])])
    eng, st = run_plan(plan)
    assert st.status == "succeeded"
    stdin = fb.calls()[0]["stdin"]
    assert stdin.startswith("Energy was 7; go.")
    assert "Upstream results available to you:" in stdin and "- `a`: UPSTREAM-SUMMARY" in stdin


def test_answer_schema_and_contract_mention_amendment_only_when_granted(fake, run_plan):
    s = answer_schema({"outputs": {"x": {"type": "integer", "required": False}}})
    assert "amendment" not in s["properties"] and s["required"] == ["summary"]
    s = answer_schema({"outputs": {}, "effects": {"amend": True}})
    assert s["properties"]["amendment"]["type"] == "object"
    f = fake("script", [{"answer": {"summary": "s"}}])
    plan = make_plan([agent_node("s", f.harness(), effects={"amend": {"auto_approve": True, "max_nodes": 2,
                                                                       "kinds": ["shell"]}})])
    run_plan(plan)
    stdin = f.calls()[0]["stdin"]
    assert '"amendment": {"rationale"' in stdin
    assert "applied automatically (max 2 nodes, kinds ['shell'])" in stdin


def test_agent_nodes_are_not_cached_across_reruns(fake, run_plan):
    f = fake("script", [{"answer": {"summary": "one"}}, {"answer": {"summary": "two"}}])
    eng, st = run_plan(make_plan([agent_node("s", f.harness())]))
    eng.rerun("s", force=False)
    eng.drive(timeout=20)
    st = eng.state()
    assert [a.summary for a in st.nodes["s"].attempts] == ["one", "two"]
    assert len(f.calls()) == 2


# ====================================================================== robustness

def test_killed_runner_and_harness_lead_to_clean_retry(fake, ff_home):
    import os
    import signal
    from forgeflow.engine import create_run
    f = fake("claude", [{"answer": GOOD, "hang": 120}, {"answer": GOOD}])
    plan = make_plan([agent_node("a", f.harness(), outputs=OUT, retry={"max_attempts": 2, "backoff": "0s"})])
    eng = create_run(plan, {}, root=ff_home, approve=True)
    eng.tick()
    rj = attempt_dir(eng, "a") / "proc" / "runner.json"
    deadline = time.time() + 20
    while not (rj.exists() and f.calls()) and time.time() < deadline:  # the fake has consumed turn 0
        time.sleep(0.1)
    info = json.loads(rj.read_text())
    os.killpg(info["child_pgid"], signal.SIGKILL)
    os.kill(info["runner_pid"], signal.SIGKILL)
    eng.drive(timeout=30)
    st = eng.state()
    assert st.status == "succeeded", st.status_reason
    a1, a2 = st.nodes["a"].attempts
    assert a1.status == "failed" and a1.error["error_class"] == "lost"
    assert a2.status == "succeeded" and a2.outputs == {"energy": -5.25}
    assert a1.session != a2.session  # a fresh session for the new attempt


def test_repair_follows_session_id_reported_by_harness(fake, run_plan):
    f = fake("claude", [{"text": "no json", "session": "server-assigned-session"}, {"answer": GOOD}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), outputs=OUT)]))
    assert st.status == "succeeded"
    c1 = f.calls()[1]
    assert c1["argv"][c1["argv"].index("--resume") + 1] == "server-assigned-session"
    assert last_attempt(st, "a").session == "server-assigned-session"


def test_large_prompt_goes_through_stdin(fake, run_plan):
    big = "Context line with data 0123456789.\n" * 9000  # ~315 KB, > MAX_ARG_STRLEN (128 KiB)
    f = fake("claude", [{"answer": GOOD}])
    eng, st = run_plan(make_plan([agent_node("a", f.harness(), prompt=big, outputs=OUT)]))
    assert st.status == "succeeded", st.status_reason
    assert f.calls()[0]["stdin"].startswith(big)


def test_status_view_shows_live_agent_activity(fake, ff_home):
    from forgeflow.engine import create_run
    from forgeflow.render import node_detail, status_view
    f = fake("claude", [{"answer": GOOD, "hang": 120}])
    eng = create_run(make_plan([agent_node("a", f.harness())]), {}, root=ff_home, approve=True)
    live = attempt_dir(eng, "a") / "live.json"
    deadline = time.time() + 20
    while time.time() < deadline:
        eng.tick()
        if live.exists() and json.loads(live.read_text()).get("actions"):
            break
        time.sleep(0.2)
    st = eng.state()
    view = status_view(st, eng.paths)
    assert "Bash: ls -la" in view and "actions" in view
    assert "agent session:" in node_detail(st, eng.paths, "a")
    eng.cancel(node="a")
    for _ in range(60):
        eng.tick()
        if eng.state().nodes["a"].status != "running":
            break
        time.sleep(0.3)
