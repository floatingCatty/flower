# Bugs found by `tests/agents` (agent nodes, harness adapters, amendments, CLI, MCP, report)

> **Status (2026-10-03): all bugs below are FIXED.** Their tests were converted from strict xfail into
> ordinary regression tests; a few tests that encoded the old behaviour were updated to the corrected design
> (poll backoff, duplicate detection via sacct ExitCode 97, sticky signals, child_rc recovery). Kept as a record.

Each bug has a test marked `xfail(strict=True, reason="BUG: …")`. When a bug is fixed, its test XPASSes
and the suite fails, so remove the marker. Line numbers are from the source tree as of 2026-10-03 (the tree was changing while these tests were written).

Run: `.venv/bin/pytest -q tests/agents` (about 2.5 min, no real LLM: fake `claude`/`codex`/`pi` CLIs in
`tests/agents/fakes/fake_harness.py`; poisoned real executables on `PATH`).

Severity: **H** = wrong verdict or a policy/safety bypass · **M** = crash, data loss or a contract
violation · **L** = robustness.

---

## H1. Short, valid Claude answers that mention rate limits are treated as quota banners
- **Test:** `test_agents_adapters.py::test_claude_short_valid_answer_mentioning_429_is_not_quota`
- **Observed:** A successful `result` event (`subtype: success`, with `structured_output`) whose text is
  under 400 chars and contains `429`, `rate limit exceeded` or `too many requests` comes back as
  `ok=False, error_class="quota"`. The node then fails with `quota_retry`, for example an agent that
  "added handling for HTTP 429".
- **Expected:** A reply that carries the answer (structured output, or text that parses as the JSON
  answer) is never a banner.
- **Root cause:** `src/forgeflow/harness/claude.py:134`. `banner = classify_error(text) if len(text) < 400`
  runs every `QUOTA_PATTERNS` regex (`\b429\b`, `rate[_ ]limit…`, `too many requests`) over successful
  answers.
- **Fix:** Apply the banner heuristic only when `res.get("structured_output") is None` and
  `extract_json(text) is None`. Use a dedicated, anchored banner list (`^you've hit your .*limit`,
  `usage limit reached`, `out of usage credits`) instead of the generic quota patterns.

## H2. `effects.amend.kinds` can be bypassed with `{op: add, node: {...}}`
- **Test:** `test_agents_amendments.py::test_kinds_policy_cannot_be_bypassed_with_singular_node_key`
- **Observed:** With the policy `kinds: [shell]`, an agent's proposal
  `{op: add, node: {id: x, kind: function, …}}` is auto-approved and applied.
- **Expected:** It goes to an `amend-…` gate.
- **Root cause:** `src/forgeflow/engine.py:926` builds `kinds` only from `op.get("nodes")`.
  `plan.apply_amendment` also accepts `op["node"]` for `add`/`drop`/`stop`, and `op["with"]` for
  `replace`. The same blind spot applies to `replace … with: {kind: job}` when `replace` is an allowed op.
- **Fix:** Derive the policy facts from the *result*, not the raw ops. Run
  `planmod.apply_amendment(st.plan, ops, status)` first, then check `effects["added"] + effects["changed"]`
  against the new plan's node kinds and count. Treat any op shape the check does not understand as
  "needs a gate".

## H3. An auto-approved node can grant itself a wider amend policy (escalation)
- **Test:** `test_agents_amendments.py::test_auto_approved_nodes_cannot_escalate_their_amend_policy`
- **Observed:** A node with `{auto_approve: true, max_nodes: 1, kinds: [agent]}` adds one agent node that
  carries `effects: {amend: {auto_approve: true}}` with no limits. That child then auto-adds 5 more
  nodes, and nobody approves any of it.
- **Expected:** A pre-authorised policy ("agent may add ≤ N nodes …", DEV_PLAN D5) cannot be widened by
  the nodes it creates.
- **Root cause:** `src/forgeflow/engine.py:911-937` (`_agent_amendment`) never inspects the added specs'
  `effects`.
- **Fix:** In the auto path, set `auto = False` if any added or replaced spec has `effects.amend`.
  Alternatively, clamp it to the parent's policy (intersection of kinds and ops, `max_nodes` taken from
  the parent's remaining budget) and track a cumulative budget per original grant.

## M1. Malformed agent amendment + `auto_approve` crashes the tick and loses the proposal
- **Test:** `test_agents_amendments.py::test_auto_approve_with_malformed_ops_does_not_crash_the_tick`
- **Observed:** An answer with `amendment: {ops: ["add a node please"]}` (or `ops` as a dict, or
  `nodes: 5`) raises `AttributeError: 'str' object has no attribute 'get'` out of `Engine.tick()`. The
  CLI driver dies with a traceback. `node.succeeded` was already journalled, so the proposal is silently
  dropped and no `plan.amendment.rejected` is recorded. Without auto-approve the same input is handled
  correctly: it is rejected with issues.
- **Root cause:** `src/forgeflow/engine.py:924-926` calls `op.get(...)` and `len(op.get("nodes"))` on
  untrusted agent JSON before validation. It is also reached from `_record_outcome` (`engine.py:883`),
  which is not covered by the tick's "never crash" `try` (`engine.py:436-439`).
- **Fix:** Before the policy checks: `if not isinstance(ops, list) or not all(isinstance(o, dict) for o in ops): auto = False`.
  Also wrap `_agent_amendment` in `try/except Exception` that journals `plan.amendment.rejected` with
  the error.

## M2. `forgeflow report` crashes when a rejected amendment has non-object ops
- **Test:** `test_agents_report.py::test_report_survives_rejected_malformed_agent_amendment`
- **Observed:** `AttributeError: 'str' object has no attribute 'get'` in `build_markdown`. The proposal
  is journalled verbatim (`plan.amendment.proposed.ops = ["…"]`) and then rejected.
- **Root cause:** `src/forgeflow/report.py:156-157` (`for op in am.ops: op.get("nodes")`).
- **Fix:** Add `if not isinstance(op, dict): continue`. Apply the same guard anywhere else that iterates
  `am.ops`.

## M3. A multi-line agent `rationale` rewrites the report's structure
- **Test:** `test_agents_report.py::test_multiline_rationale_cannot_break_report_structure`
- **Observed:** The rationale `"Chose PBE.\n```\n…\n## Provenance\n- forged"` is pasted verbatim. The
  stray fence swallows the following sections, so `report.html` loses its real **Timeline** and
  **Provenance** headings and gains an agent-authored "Provenance" heading. Agents control this text.
- **Root cause:** `src/forgeflow/report.py:187`
  (`md.append(f"- rationale: {truncate(a.rationale, 1200)}")`). Error messages (`a.error["message"]`)
  and table cells have the same problem.
- **Fix:** Render free text from agents as an indented or quoted block: prefix every line with `  > `,
  or replace newlines with `⏎`, and neutralise lines that start with ```` ``` ```` or `#`. `md_to_html`
  should treat list-item continuation lines as text.

## M4. Approving a gated amendment ignores the generation it was reviewed against (no CAS)
- **Test:** `test_agents_amendments.py::test_gated_amendment_is_bound_to_the_plan_generation_it_was_reviewed_against`
- **Observed:** At generation 0 an agent proposes `detour after scout`, and the gate's diff shows only
  `added: [k]`. Then a second amendment moves the plan to generation 1 by adding node `c` with
  `needs: [scout, w]`. When the gate is approved, the ops are re-applied to generation 1, and `c` is
  silently re-wired to need `k`. The reviewer never saw that change.
- **Expected (DEV_PLAN D5):** "An amendment carries (parentGeneration, parentDigest). It is applied by
  compare-and-swap."
- **Root cause:** `src/forgeflow/engine.py:965-970` (`_apply_amendment`) re-applies `am.ops` to
  `st.plan` without comparing `am.parent_generation`/`parent_digest` with the current generation.
- **Fix:** If `am.parent_generation != st.generation`, reject the amendment with
  `stale: proposed against generation N, plan is at M; re-propose`. Alternatively, re-run
  `apply_amendment` and re-open a fresh gate with the recomputed diff, and approve only if the diff is
  identical.

## M5. A quota or usage-limit banner fails the node under the default retry policy
- **Test:** `test_agents_executor.py::test_quota_banner_does_not_fail_the_node_by_default`
- **Observed:** With the default `retry.max_attempts: 1`, a banner yields `node.failed{error_class: quota_retry, retryable: true}`
  and the run fails. It only waits and retries if the plan raised the retry budget. That path works:
  `test_quota_banner_retry_honours_retry_after_then_succeeds`.
- **Expected (DEV_PLAN D8):** "detect quota or session-limit banners that exit 0 (park until the reset
  time; do not fail)".
- **Root cause:** `src/forgeflow/executors/agent.py:176` maps quota to an ordinary failure class.
  `src/forgeflow/engine.py:894-899` then spends the normal task-retry budget on it.
- **Fix:** Do not count `quota_retry` against `max_attempts`. Always schedule
  `node.retry_scheduled{not_before = retry_after or a default such as 15m}`, with a separate cap
  (e.g. `defaults.quota_max_wait: 24h`). When that cap is exhausted, park on a gate instead of failing.

## M6. `details.text_tail` holds the head of the agent's text
- **Test:** `test_agents_executor.py::test_schema_invalid_details_keep_the_tail_of_long_output`
- **Observed:** For a long reply that never matches the contract, `details.text_tail` holds the first
  1,200 chars. The end of the reply, where the final answer or the explanation is, is lost. The same
  happens for quota and auth failures (600 chars). The non-resumable repair re-prompt also sends only
  the head (`Your previous answer (truncated)`, 3,000 chars), which drops the JSON attempt at the end.
- **Root cause:** `src/forgeflow/executors/agent.py:180,194,213` use `util.truncate`, which keeps the
  prefix. DEV_PLAN D8: "keep the stdout **tail** when output is capped".
- **Fix:** Add a `tail(text, n)` helper (`"…" + text[-(n-1):]`) and use it for `text_tail`. For the
  repair prompt use head + tail (Smithers: 1 KB head + 1 KB tail).

## M7. A declared output named `summary`, `rationale` or `amendment` can never be satisfied
- **Test:** `test_agents_executor.py::test_declared_output_named_summary_can_be_satisfied`
- **Observed:** With `outputs: {summary: string}`, every answer is judged "summary: required". The
  repair turns are spent and the node ends in `schema_invalid`. Plan validation accepts these names.
- **Root cause:** `src/forgeflow/executors/agent.py:26,187,195`. `EXTRA_KEYS` are stripped from the
  answer before `check_outputs`, whatever the declared outputs are.
- **Fix:** Strip only extras that are not declared:
  `extras = set(EXTRA_KEYS) - set(node["outputs"])`. Alternatively, reject these names in
  `plan.validate` for agent nodes.

## M8. The MCP server dies on a malformed request
- **Test:** `test_agents_mcp.py::test_malformed_requests_do_not_kill_the_server` (5 cases)
- **Observed:** Each of these kills `forgeflow mcp` with a traceback, and later requests get no answer:
  - a JSON line that is not an object (`[1,2]`, `42`, `"x"`);
  - `tools/call` with `params.name: null`;
  - `params` that is a list.
- **Root cause:** `src/forgeflow/mcp_server.py:106` (`msg.get` on a non-dict) and `:120`
  (`.get("name", "").removeprefix` on `None`, `.get` on a list). Neither is inside `try`.
- **Fix:** For a non-dict message, reply with `{"error": {"code": -32600}}` (or skip it when it has no
  id). Validate that `params` is a dict and `name` is a str, and return `-32602` otherwise. Wrap each
  request's handling in `try/except Exception` that returns `-32603`.

## M9. `forgeflow mcp --read-only` still advances runs
- **Test:** `test_agents_mcp.py::test_read_only_status_does_not_advance_the_run`
- **Observed:** The `forgeflow_status` tool, annotated `readOnlyHint: true` and kept in read-only mode,
  ran `forgeflow status`. That ticks the engine, which journals events and starts the next node's
  process.
- **Root cause:** `src/forgeflow/mcp_server.py:63-64` calls `status` without `--no-tick`, and
  `src/forgeflow/cli.py:308-309` ticks by default. `wait` also drives the run.
- **Fix:** Over MCP, always call `status … --no-tick`. In `--read-only` mode, either drop `wait` or make
  it observe only (poll the state without ticking).

## M10. The recursion guard is not enforced: an agent inside a node can decide the user's gates
- **Test:** `test_agents_cli.py::test_agent_inside_a_node_cannot_answer_the_users_gate`
- **Observed:** A node process (`FORGEFLOW_INSIDE_RUN=1`, `FF_RUN_ID` set) runs
  `forgeflow answer $FF_RUN_ID review#a1 approve`, and the human review gate is approved. The answer is
  attributed to the human actor (`test:pytest`), because the node inherits `FORGEFLOW_ACTOR`. The MCP
  server withholds decision verbs for exactly this reason, but the CLI, which every agent has, does not.
- **Root cause:** No enforcement anywhere. `FORGEFLOW_INSIDE_RUN` is only set
  (`src/forgeflow/executors/base.py:69`) and mentioned in the skill. `cmd_answer`/`cmd_approve`
  (`src/forgeflow/cli.py`) and `Engine.answer` accept the call. `NodeCtx.base_env` passes the parent's
  `FORGEFLOW_ACTOR` through.
- **Fix:**
  - In `cli.main`, refuse the decision verbs (`approve`, `reject`, `answer`, `amend --yes`) with exit 2
    and code `inside_run` when `FORGEFLOW_INSIDE_RUN` is set.
  - Drop `FORGEFLOW_ACTOR` from node environments, or set it to `agent:<run>/<node>`, so any action is
    attributed correctly.
  - Optionally, have `_ingest_files` ignore answer files whose `by` names an agent.

## M11. Regression: a missing harness executable is no longer a `spawn` failure
- **Test:** `test_agents_executor.py::test_missing_harness_executable_is_a_spawn_error[claude|script]`
- **Observed:** This appeared while the tests were being written (`_runner.py` changed at 00:31). The
  runner now starts every payload as `/bin/sh -c 'trap "" HUP; "$@"; …' child_rc argv…`, so `Popen`
  no longer raises `OSError` for a missing executable, and the `spawn_error` branch is dead code. The
  shell exits 127 and the agent node is classified as `harness` ("claude produced no result event")
  or `agent_error` (script). A typo in `harness.command` therefore looks like an agent misbehaving.
  The node is still not retried, and the message does mention "not found", which
  `test_missing_harness_executable_fails_once_and_says_so` still asserts.
- **Root cause:** `src/forgeflow/_runner.py:83-86` (the sh wrapper) together with
  `executors/base.py:222` (`spawn_error` is never set now).
- **Fix:** Before wrapping, resolve `argv[0]` in the runner (`shutil.which(argv[0], path=env["PATH"])`,
  or `os.access(argv[0], os.X_OK)` for paths). Write the `spawn_error` exit record if it is missing.
  Alternatively, have the wrapper run `command -v "$1" >/dev/null || { echo spawn > "$0.spawn"; exit 127; }`.

## L1. `extract_json` prefers an earlier fenced example over the bare final answer
- **Test:** `test_agents_extract.py::test_final_bare_answer_beats_earlier_fenced_example`
- **Observed:** Given `"…example:\n```json\n{example}\n```\n…Final answer:\n{real}"`, the function
  returns `{example}`. The contract tells the agent to end with ONE JSON object, and the docstring says
  the search runs "from the END so the required final answer wins".
- **Root cause:** `src/forgeflow/harness/base.py:172-177`. Any fence wins before the balanced-object
  pass runs.
- **Fix:** Compute the last fence (its end offset) and the last balanced object (its end offset), and
  return whichever ends later. Equivalently, accept a fence only if no balanced object starts after it.

## L2. `extract_json` is quadratic on brace-heavy output
- **Test:** `test_agents_extract.py::test_extraction_is_not_quadratic_on_brace_heavy_output`
  (subprocess, 4 s cap)
- **Observed:** About 75 KB of code-like text with unmatched `}` and no JSON takes about 30 s. That
  happens inside `tick()`, under the run lock. Script-harness `text` is the whole stdout.
- **Root cause:** `src/forgeflow/harness/base.py:178-202`. For every `}` from the end, the loop scans
  backwards to the start of the text when depth never returns to 0.
- **Fix:** Do one forward pass with a string-aware stack that records the `(start, end)` spans of
  balanced objects. Try `json.loads` on those spans from the last to the first, with a bounded number
  of attempts. Also cap the scanned text, for example to the last 256 KB.

---

## Fixed upstream while testing (now plain regression tests)
- `forgeflow output RUN NODE <missing.key>` raised an uncaught `KeyError` (traceback, no envelope). Test:
  `test_agents_cli.py::test_output_missing_key_is_a_clean_error`.
- `forgeflow logs RUN NODE --attempt 9` raised an uncaught `StopIteration`. Test:
  `test_agents_cli.py::test_logs_unknown_attempt_is_a_clean_error`.

## Observations (no failing test; worth a look)
- `harness.max_turns` is accepted and passed into `AgentRequest.max_turns`, but `ClaudeHarness.build`
  never emits `--max-turns`, so the limit is silently ignored.
- Codex (`codex.py`, `rc != 0 and not final`) and pi (`pi.py`, the same rule) treat a **non-zero exit
  that still produced text** as success. A crash right after an intermediate message counts as a valid
  answer.
- `classify_error` runs over arbitrary stderr. Any `\b401\b` or "unauthorized" (for example in a test log
  the agent printed) makes a script, codex or claude failure non-retryable `auth`.
- `effects.amend.auto_approve: "no"` (a quoted string) is truthy, and policy fields are not type-checked
  by `plan.validate`.
- `forgeflow skill all` installs only the home-level targets (claude, codex and agents), not the
  project ones, but it still creates `.forgeflow/` in the current directory.
- `--json` is only accepted after the subcommand: `forgeflow --json status` is a usage error (exit 2).
