# flower core: bugs found by `tests/core`

> **Status (2026-10-03): all bugs below are FIXED.** Their tests were converted from strict xfail into
> ordinary regression tests; a few tests that encoded the old behaviour were updated to the corrected design
> (poll backoff, duplicate detection via sacct ExitCode 97, sticky signals, child_rc recovery). Kept as a record.

Each bug has a test marked `xfail(strict=True)`. When the bug is fixed, the test XPASSes, which fails
the suite under `strict=True`. Delete the marker at that point.
Line numbers refer to `src/flower/` at the time of testing. The source was being edited while the
suite was written.

Run with: `.venv/bin/pytest -q tests/core` (about 110 s). Add `--runxfail` to see the real failures.

## Engine

### 1. A branch skipped by `when:` fails the whole run
- **Test:** `test_core_engine_flow.py::test_when_branch_not_taken_does_not_fail_run`
- **Observed:** `check -> heavy(when: false) -> post`. `heavy` is skipped ("condition false"). `post` is
  skipped with "upstream heavy skipped (condition false …)". The run ends **failed**: "1 node(s) skipped
  after upstream failure".
- **Expected:** the run succeeds. Nothing failed; a branch was simply not taken. DEV_PLAN D9 treats
  `when:` as the normal way to branch.
- **Root cause:** `engine.py:1045` treats every skip whose reason starts with `"upstream"` as a failure.
  It never checks why the upstream node was skipped. The same thing happens below a node that was
  stopped by an amendment, and below an empty `foreach`.
- **Fix:** only count skips that trace back to a failed or cancelled node. Record
  `{"cause": "failure"|"condition"|"stopped"|...}` in the `node.skipped` payload and propagate the root
  cause in `_dep_state`. Then count only `cause == "failure"` in `_update_run_status`.

### 2. A rejected gate is satisfied from its own cache when the rework is identical
- **Test:** `test_core_engine_flow.py::test_gate_reject_loop_reasks_even_if_rework_is_identical`
- **Observed:** `draft -> review(gate, on_reject.rerun: [draft]) -> publish`. Answer `reject`. `draft`
  re-runs (forced) and produces the same output. The gate is **not asked again**: `_start` reuses the
  gate's previous result, whose decision was *reject*, as `node.succeeded reused_from a1`. `publish`
  then runs and the run succeeds, although nobody ever approved.
- **Root cause:** `plan.py:137` defaults `cache: True` for every non-agent kind, including `gate` and
  `wait`. `engine.py:633-634` reuses `ns.result` when decl and input hashes match. The gate is in the
  stale cone with `force: False` (`engine.py:946`).
- **Fix:** never cache-reuse `gate` nodes. Set `cache: False` for gates in `normalize_node`, or skip the
  reuse branch when `kind == "gate"`. Also emit `force: True` for the gate itself in
  `_resolve_node_gate`. Consider the same for `wait` nodes, since a reused timer or signal is rarely
  intended.

### 3. Cancelling a run before approval does nothing
- **Test:** `test_core_engine_control.py::test_cancel_run_awaiting_approval`
- **Observed:** `create_run(approve=False)`, then `eng.cancel()` (accepted; `run.cancel_requested` is
  journalled), then `tick()`. The status stays `awaiting_approval` forever.
- **Root cause:** `engine.py:336-351`. The `awaiting_approval` branch returns before the
  `cancel_requested` check at `engine.py:357`.
- **Fix:** check `st.cancel_requested` before the approval branch. Then withdraw the plan gate and emit
  `run.completed{status: cancelled}`. Alternatively, have `cancel()` reject the plan gate when the run
  is not yet approved.

### 4. Cancelling a node that waits on a gate leaves the gate open, and the run parks forever
- **Test:** `test_core_engine_control.py::test_cancel_gate_node_closes_gate`
- **Observed:** `cancel(node="g")` on a waiting gate node emits `node.cancelled`. Gate `g#a1` stays
  `open`. `_update_run_status` sees an open gate and parks the run forever. `pending/g_a1.request.json`
  also stays on disk.
- **Root cause:** `engine.py:270-279`. For `gate` (and `wait`) kinds, `cancel()` only emits
  `node.cancelled`. Only `_cancel_everything` (`engine.py:~999`) withdraws gates.
- **Fix:** in `cancel()`, when the node has an open gate (`ns.gate_id`), emit
  `gate.answered{decision: "withdrawn"}` (key `gate.answered:<id>`) and remove the request file.

### 5. Rerun or amendment after a run cancel hangs in `running` forever
- **Test:** `test_core_engine_control.py::test_rerun_after_run_cancel_does_not_hang`
- **Observed:** cancel the run, then `rerun("a")`. That is accepted and `run.reopened` sets the status
  to `running`. Every later tick runs `_cancel_everything` again, which skips `a`. Its
  `run.completed` uses the fixed idempotency key `run.completed:cancel`, so it is deduped. The run
  stays `running` with nothing running, and `drive` never returns.
- **Root cause:** `state.py:279-280`. `cancel_requested` is set and never cleared, not even by
  `run.reopened`. `engine.py:1002` uses a constant idempotency key.
- **Fix:** either refuse `rerun` and amendments on a `cancelled` run (`FlowerError("run_cancelled")`),
  or clear `cancel_requested` on `run.reopened` in the fold. In either case, key `run.completed` on the
  seq (as the other completions do).

### 6. `replace … supersede: true` on a running node orphans its process
- **Test:** `test_core_engine_control.py::test_supersede_running_node_does_not_orphan_process`
- **Observed:** a node is running `sleep 30`. An auto-approved `replace` with `supersede: true` is
  accepted. The node gets `node.stale`, goes back to pending, and attempt 2 is launched. The attempt 1
  process keeps running and is never polled, cancelled or recorded.
- **Root cause:** `plan.py:464-466,492`. `is_pending` is false for `running`, but `supersede` lets
  every non-pending status through, not just finished ones. `engine.py:911-913` then emits
  `node.stale` for a running node.
- **Fix:** in `apply_amendment`, allow `supersede` only for terminal nodes (`succeeded`, `failed`,
  `skipped`, `cancelled`). Refuse `running`, `waiting` and `retrying` with "cancel it first".
  Otherwise, cancel the running attempt in `_commit_amendment` before emitting `node.stale`.

### 7. Two `foreach` expansions in one pass: the second generation silently drops the first
- **Test:** `test_core_engine_flow.py::test_foreach_generations_never_drop_nodes`. The functional
  outcome still passes in `test_two_foreach_nodes_expanding_in_the_same_tick`.
- **Observed:** `f1` and `f2` are both ready. Generation 1 adds `f1[0..1]`. Generation 2 is computed
  from the stale generation-0 plan, so it contains `f2[*]` but **not** `f1[*]`. The scheduler later
  re-expands `f1` as generation 3, with a new amendment id. The run ends correctly, but the audit trail
  shows nodes vanishing without any amendment, and duplicate expansions.
- **Root cause:** `engine.py:586-588`. After `_handle_foreach`, the loop does `continue` instead of
  `break`-and-refold, so `st` is stale. `engine.py:772` applies the ops to `st.plan`.
  `_commit_amendment` (`engine.py:901-910`) never checks `parent_digest` against the current digest:
  the compare-and-swap promised in DEV_PLAN M4 is missing.
- **Fix:** `break` after a successful `_handle_foreach` (as `_start` does). In `_commit_amendment`,
  refuse or recompute when `st.digest != parent_digest`.

### 8. A foreach is never re-expanded after its item list changes
- **Test:** `test_core_engine_control.py::test_rerun_upstream_of_foreach_reexpands`
- **Observed:** `gen` first outputs `items: [1]`. After `rerun gen` it outputs `[1, 2, 3]`. The
  collector still reports `count: 1`. The old child (`bind.item == 1`) is re-run or reused, and the new
  items are silently ignored.
- **Root cause:** `engine.py:740`. Once `expanded_from` children exist, `_handle_foreach` only collects.
  The expansion is frozen into the plan and the bound items are never compared with the re-rendered
  list.
- **Fix:** when a stale collector starts, re-render `foreach`. If the list differs from the children's
  `bind.item`s, commit a new expansion amendment that drops the old pending children and adds new
  ones. The old ones can be dropped because `rerun` made them stale (pending). Unchanged items keep
  their ids, so the cache still applies.

## Plan and template

### 9. Wrong-typed `retry` / `outputs` crash validation instead of being reported
- **Tests:** `test_core_plan.py::test_non_dict_retry_reports_issue_instead_of_crashing` and
  `test_non_dict_outputs_spec_reports_issue_instead_of_crashing`
- **Observed:** `retry: 3` raises `TypeError: 'int' object is not iterable`. `outputs: [a, b]` raises
  `AttributeError`. `plan validate` and `run` crash with a traceback (exit 1) instead of returning
  `plan_invalid` (exit 2) with the list of issues.
- **Root cause:** `plan.py:123` (`retry.update(n.get("retry"))`) and `plan.py:147`
  (`n.get("outputs").items()`). `normalize_node` runs before `validate` and assumes the types are
  right. The same happens for `timeout: [..]`, a non-dict `harness`, and similar.
- **Fix:** make `normalize_node` type-tolerant: copy only dict values, and leave others for `validate`
  to report as `retry` / `outputs` / `timeout` issues. Alternatively, run a shape check that collects
  issues before normalising.

### 10. Expressions rewrite `true`/`false` inside string literals
- **Test:** `test_core_template.py::test_true_false_inside_string_literals_untouched`
- **Observed:** `when: ${x.outputs.status} == "true"` is always false, because the literal becomes
  `"True"`. `'false'` evaluates to `'False'`.
- **Root cause:** `template.py:195`. A regex substitution on the whole source rewrites the words.
- **Fix:** delete the regex. `_eval` already maps the names `true`/`false`/`null` in `ast.Name`
  (`template.py:~127`).

### 11. The `$${` escape is also applied to substituted values
- **Test:** `test_core_template.py::test_escape_not_applied_to_substituted_values`
- **Observed:** an input value `"$${x}"` interpolated as `"v=${inputs.dollar}"` becomes `"v=${x}"`. As
  a whole-string reference it stays `"$${x}"`. Data is altered, and the two cases disagree.
- **Root cause:** `template.py:87`. `.replace("$${", "${")` runs on the result of `REF.sub`, after the
  values have been substituted.
- **Fix:** handle the escape inside the regex pass. Use one pattern with alternatives
  `\$\$\{` → `${` and `\$\{…\}` → value, so substituted text is never rescanned.

### 12. The "safe" expression evaluator allows unbounded computation (DoS)
- **Test:** `test_core_template.py::test_expression_resource_bomb_is_bounded`
- **Observed:** `when: "9 ** 9 ** 9 > 0"` passes `check_expr` and validation, then hangs the tick
  (bounded at 2 s in the test). `"x" * 10**10` would exhaust memory in the same way.
- **Root cause:** `template.py:113-114`. `ast.Pow` and `ast.Mult` are applied without bounds.
- **Fix:** cap the size of `**` operands (for example exponent ≤ 64 and |base| ≤ 1e6), and cap sequence
  repetition (`len * n ≤ 1e6`). Alternatively, drop `Pow` from `_BIN`.

## CLI

### 13. `flower output RUN NODE KEY` crashes on a missing key
- **Test:** `test_core_cli.py::test_output_unknown_key_is_a_clean_error`
- **Observed:** an uncaught `KeyError` / `ValueError` / `IndexError` prints a traceback and exits 1, with
  no JSON envelope.
- **Root cause:** `cli.py:473`.
- **Fix:** wrap the walk in `try/except (KeyError, IndexError, ValueError, TypeError)` and raise
  `FlowerError("no_key", …, "keys: …")`.

### 14. `flower logs RUN NODE --attempt N` crashes on an unknown attempt
- **Test:** `test_core_cli.py::test_logs_unknown_attempt_is_a_clean_error`
- **Observed:** an uncaught `StopIteration`.
- **Root cause:** `cli.py:434`. `next(...)` is called without a default.
- **Fix:** use `next(..., None)` and raise `FlowerError("no_attempt", …)`.

### 15. `flower reject RUN <unknown gate>` crashes
- **Test:** `test_core_cli.py::test_reject_unknown_gate_is_a_clean_error`
- **Observed:** `AttributeError: 'NoneType' object has no attribute 'decisions'`.
- **Root cause:** `cli.py:501`. When `g is None`, the `--reject` branch evaluates `g.decisions[-1]`.
- **Fix:** if `g is None`, raise `FlowerError("gate_not_found", …)` before choosing a decision. In
  the non-reject branch, `eng.answer` already reports this.

## Found by the ABACUS benchmark (`benchmark/si-dos-fermi`, 2026-10-03)

Both bugs were found in a real study. Both are fixed and covered by ordinary regression tests.

### 16. `rerun` of a foreach node does not re-execute its items
- **Tests:** `test_core_engine_control.py::test_rerun_foreach_parent_reexecutes_its_children`,
  `::test_rerun_foreach_parent_only_skips_downstream`, `::test_rerun_foreach_parent_refuses_while_a_child_runs`
- **Observed:** `flower rerun RUN fermi` (a foreach over k-meshes) re-ran only the collector. The
  collector gathered the old `fermi[i]` results again, and downstream `converge` was reused from cache.
  The fix to the analysis code was silently not applied.
- **Root cause:** `Engine.rerun` marked `[node] + descendants(node)` stale. Foreach children are
  *upstream* of their collector, because the collector `needs` them, so they were never included.
- **Fix:** the targets are the node, its expansion children (`expanded_from == node`) and the
  descendants. `force` applies to the node and its children. The busy check covers the children too.

### 17. A `function` node's cache key ignores the code it calls
- **Tests:** `test_core_engine_control.py::test_function_cache_keys_on_module_source`,
  `::test_function_cache_keys_on_whole_local_package`,
  `test_core_fork_provenance.py::test_fork_does_not_reuse_function_result_after_its_module_changed`
- **Observed:** after the analysis module was edited, the function nodes still counted as "unchanged".
  A downstream rerun reused the old results, and so did a `fork`, which replayed results computed by
  the old code.
- **Root cause:** `decl_hash(spec)` hashes only the YAML node. The `call:` target is a string.
- **Fix:** for `function` nodes, `_start` mixes `util.source_fingerprint(module, dirs)` into the decl
  hash. This is a content digest of the called module's top-level package, or of the single
  `<module>.py`. The module is resolved on the node's `pythonpath` and then the plan directory, in import
  order. Installed libraries are not tracked.
- **Not covered:** `shell` and `job` scripts that run `python ${plan.dir}/x.py` are not covered. Their
  script text is hashed, but the files it names are not.

## Observations (no xfail: questionable rather than certainly wrong)

- **`on_reject.max_attempts` counts reworks, not attempts.** `engine.py:934` uses
  `rerun_count < max_attempts`. With `max_attempts: 1` the gate is asked twice and the target runs
  twice (`test_gate_reject_loop_exhausted_fails`). `retry.max_attempts` counts *total* attempts, so the
  same key means different things. Also, DEV_PLAN D9 says "when a loop or budget is exhausted, the run
  **suspends** with a gate instead of failing". The implementation fails the gate node and the run.
- **The runner dies but the child finishes, and the result is lost.** `executors/base.py:159-164`. If
  the `_runner` is SIGKILLed while its child keeps going, the node stays `running`, as intended. When
  the child exits (even 0, with `outputs.json` written), there is no `exit.json`, so the attempt
  becomes `lost`. With retries the work is redone and side effects are duplicated
  (`test_runner_killed_child_survives_keeps_running_then_lost`). Suggestion: have the child write its
  own return code. For example, exec through `sh -c '"$@"; echo $? > proc/child_rc'`, or have the
  runner write `child_rc` from a `wait` in a double-forked reaper. Then `poll_process` can recover the
  verdict.
- **`BASH_ENV` leaks into shell nodes.** The runner copies the full environment of whoever ticks. On
  this machine `BASH_ENV=~/.bashrc` made every shell node source the user's bashrc (about 0.5 s each,
  and not reproducible). The suite unsets it. Consider adding `BASH_ENV`/`ENV` to `unset_env` for shell
  nodes, or recording the environment in the attempt for provenance.
- **`flower status` always exits 0**, even for failed or parked runs (`cli.py:311-312`). `wait` and
  `run` carry the verdict. This is documented by `test_status_exit_code_is_informational`. If `status`
  is meant to follow the exit-code vocabulary, it should use `run_status_code(st.status)`.
- **A missing `flower:` key is silently accepted.** `normalize` defaults it to 1, so the `version`
  issue only fires for an explicit wrong value.
- **A signal sent before its `wait` node is armed is ignored** (`since_seq`). This is by design, but it
  is easy to trip over. It is documented by
  `test_signal_sent_before_wait_is_armed_is_not_consumed`. `--token`-scoped signals from DEV_PLAN D9
  are not implemented: the wait node ignores `token`.
