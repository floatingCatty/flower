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

### 18. Stale foreach children run once with their old item before the collector re-expands
- **Test:** `test_core_engine_control.py::test_rerun_upstream_children_wait_for_their_new_items`
- **Observed:** this came up in the benchmark run `si-dos-fermi-remote`. A rerun of `scf` from the web
  UI made each `fermi[i]` run first on the *previous* attempt's DOS folder. Then `fermi` re-expanded
  ("item list changed (4 → 4)"), superseded them, and ran them all again.
- **Root cause:** a collector waited for all its needs, including its children, before re-evaluating
  its item list. The stale children only waited for the upstream nodes.
- **Fix:**
  - A collector waits only for its non-child dependencies.
  - `_handle_foreach` re-checks the item list first.
  - A child whose collector is pending and whose bound item differs from the current list waits
    (`Engine._item_outdated`). Re-expansion then replaces it with the new item.

### 19. Clock times were UTC without saying so
- **Test:** `test_core_time.py`
- **Observed:** `flower log` showed "14:30:30" for an event at 10:30:30 EDT. The same applied to the
  report, `watch` and the UI timeline.
- **Root cause:** every display cut `HH:MM:SS` out of the UTC ISO stamp. On this host Python's C
  library also ignores `/etc/localtime` when `TZ` is unset, so `astimezone()` alone reports UTC.
- **Fix:**
  - The journal stays UTC.
  - Displays use `util.local_clock` / `local_stamp`, which resolve `$TZ` or `/etc/localtime` with
    zoneinfo.
  - The timeline and report state the zone.
  - The UI shows browser-local time, with the UTC stamp in the tooltip.

### 20. An approved amendment on a finished run was never applied
- **Test:** `test_core_devloop.py::test_default_policy_asks_before_applying_an_edit`
- **Observed:** `flower amend` (or `rerun` picking up a plan edit) on a failed run, followed by
  `flower approve RUN amend-…`. The amendment stayed `proposed` forever.
- **Root cause:** `_tick` returned early for runs in a terminal state, *before* processing answered
  amendment gates. Auto-approved amendments worked only because they are applied immediately.
- **Fix:** amendment gates are processed before the terminal-state return, except for rejected or
  cancelled runs. Applying the amendment reopens the run (existing behaviour of `_commit_amendment`).

## Found by the TTG reproduction (`benchmark/ttg-twistons`, 2026-10-03)

### 21. The agent worked beside the run instead of in it
- **Test:** `test_core_devloop.py` (start / add / init / hook tests)
- **Observed:** reproducing a paper, the coordinating agent (inside the flower repo, after building the
  dev loop) read the paper, prototyped and validated code by hand for a while before writing a plan; the
  user saw nothing in the UI.
- **Root cause:** not a code defect but a design gap. (a) The instructions lived only in a skill that was
  not installed in that session. (b) Nothing existed until a plan with steps did, so the early phase had
  no home. (c) Running a command by hand was cheaper than recording it (write YAML, then rerun).
- **Fix:** `flower start` (a run from minute one, empty draft plan, parked until it has steps);
  `flower add RUN ID -- <command>` (writes the step and runs it); new `inputs:` / `clusters:` in the plan
  file are picked up by add/rerun (amendment ops `add_inputs`, `add_clusters`); `flower init` installs
  the skill and an `AGENTS.md` block in the project, `--hook` a Claude Code PostToolUse reminder.

### 22. `flower remote exec --env` could not run the recipe being written
- **Test:** `tests/hpc/test_envs.py::test_cli_explore_freeze_replay`
- **Observed:** `bash "$FLOWER_ENV_DIR/check.sh"` during exploration: no such file.
- **Root cause:** the exploration prefix's recipe directory was created but never filled.
- **Fix:** the current recipe files are sent with every `remote exec --env` (base64 in the preamble).

### 23. Editing a foreach step (not its items) re-collected the old children
- **Test:** `test_core_devloop.py::test_editing_a_foreach_steps_template_reruns_its_items`
- **Observed:** adding a declared file to the TTG `relax-analysis` foreach step and `flower rerun RUN
  relax-analysis`: the collector was superseded and immediately "succeeded" with the old children's
  results; the new file never appeared.
- **Root cause:** `_handle_foreach` re-expanded only when the *item list* changed.
- **Fix:** each existing child is compared with what the current step definition would produce for its item
  (ignoring id/title/needs/bind); children that differ are replaced (superseded) like changed items.

## Found by the Si thermal-expansion reproduction (`benchmark/si-nte`, 2026-10-03)

### 24. `flower cancel` left a time-limited payload running (`scheduler: none`)
- **Test:** `tests/hpc/test_direct.py::test_direct_cancel_reaches_a_payload_under_a_time_limit`
- **Observed:** cancelling a hung environment step: the step was marked cancelled, `ld1.x` kept running.
- **Root cause:** with a time limit the payload runs under coreutils `timeout`, which puts it in a process group
  of its own; cancel signalled only the session leader's group.
- **Fix:** cancel and kill signal the whole session (`pkill -s`), then the group, then the PID.

### 25. A shell step's cache key ignored the script it runs
- **Test:** `test_core_devloop.py::test_an_edited_script_is_not_served_from_cache`
- **Observed:** after editing `ttg_hf.py`, a rerun returned the old results for the unchanged items.
- **Root cause:** for shell/job steps only the command text was hashed (function steps already hashed their
  module). Also affected `fork`/`--reuse`: results from an older script were reused.
- **Fix:** the decl hash includes a fingerprint of the plan-directory files the step references in
  run/script/stage_in/env (a `.py` brings its sibling modules; a directory such as PYTHONPATH its `.py` files).

### 26. `rerun` after a plan edit did not re-run unchanged foreach items
- **Test:** `test_core_devloop.py::test_rerun_after_an_edit_still_reruns_unchanged_items`
- **Root cause:** when the edit had already queued the step, `rerun` reported "already pending" and skipped
  forcing it, so its unchanged items were re-collected from cache.
- **Fix:** `rerun` always forces, unless the step is actually running.

### 27. An environment check that hangs blocked its step for the whole setup budget
- **Test:** `tests/hpc/test_envs.py::test_a_hanging_check_fails_fast_and_says_so`
- **Observed:** the frozen QE recipe, valid on a 32-core host, hung in `check.sh` on a 96-core host (`ld1.x`
  started one OpenMP thread per core); the step sat silent for 10+ minutes of its 30.
- **Fix:** `check.sh` runs under its own limit (`env.yaml: check_timeout`, default 10 min) and the log says so
  when it is hit; the QE recipe's `activate.sh` defaults `OMP_NUM_THREADS=1`.

## Found while preparing the water-diffusion benchmark (2026-10-03)

### 28. `retry: {on: [exit_nonzero]}` was ignored on `scheduler: none` clusters
- **Test:** `tests/hpc/test_direct.py::test_state_dir_survives_retries_and_a_rerun_starts_fresh`
- **Root cause:** the direct backend reports a nonzero exit as explicitly non-retryable, and an explicit verdict
  overrode the step's own list of retryable classes.
- **Fix:** a class the step lists in `retry.on` is retried (its author decided).

### 29. No way to resume a long job after a retry (feature gap)
- **Test:** same test.
- **Observed:** every attempt has a fresh directory; a lost or timed-out MD run restarted from zero.
- **Fix:** `$FLOWER_STATE_DIR`, kept across the retries of one start, new on a deliberate rerun or an edit.

### 30. A lost job's retry lost its slot to fresh work
- **Test:** `tests/hpc/test_direct.py::test_a_retry_keeps_its_slot_and_goes_first`
- **Observed:** water MD, `max_jobs: 3`: a crashed 2-ns run (checkpointed at 150 ps) waited behind new items.
- **Fix:** due retries are scheduled before pending steps, and a retry in its backoff keeps its cluster slot.

## Found running the S22 and water campaigns side by side (2026-10-03)

### 31. A driver died on half-written flower code, and `--follow` then waited forever
- **Observed:** while flower's source was being edited, a background driver crashed importing a half-written
  module (`driver.stopped`, status still running); the follower in `flower add` hung with nobody driving.
- **Fix:** the driver logs an exception, pauses and carries on (gives up after 20 in a row), and re-executes
  itself when flower's code changes; `--follow` restarts a driver that is not alive.

### 32. Items not started yet ran with the old definition after an edit of their step
- **Observed:** S22, `tmpdir: job` added while 6 items ran: pending items kept starting with `/tmp`.
- **Root cause:** `_item_outdated` compared only the item value, not the step's definition.
- **Fix:** a pending item whose step was edited waits for the re-expansion.

### 33. `rerun --cached` refused to apply an edit while items were running
- **Fix:** with `--cached`, the edit is applied and running work continues (no forced rerun).

### 34. A setting that does not change results forced every finished item to be recomputed (feature gap)
- **Fix:** `tmpdir` joins `resources`/`timeout`/`retry` outside the cache key; `FLOWER_MEM_MB` and `FLOWER_CPUS`
  are exported from `resources` so a payload can size itself without changing its definition.

### 35. `--reuse` / fork reused steps marked `cache: false`
- **Test:** `tests/core/test_core_fork_provenance.py::test_reuse_respects_cache_false`
- **Observed:** the Si study re-run with another pseudopotential (`flower run --reuse`): its environment check,
  which must always run, was taken from the earlier run.
- **Fix:** reuse from other runs obeys `cache: false` and a forced rerun, like the run's own cache.

### 36. New driver events were not in the journal's vocabulary: every reloading driver died (campaigns stalled)
- **Test:** `tests/core/test_core_event_vocabulary.py`
- **Observed:** after the self-reload of #31, the S22, water and Si runs had no driver for about an hour: the
  first reload emitted `driver.reloaded`, the journal rejected the unknown type, the driver exited. `resume`
  printed a PID as if all was well; `status` showed waiting steps and gave no hint.
- **Fix:** the types are registered (and a test checks every emitted type is); `spawn_driver` waits briefly and
  reports a driver that exits at once with its log; `flower status` starts a driver for a running run that has
  none, and says so (`--no-tick` only warns).

### 37. Runs on the same machine oversubscribed it (no batch system to arbitrate)
- **Test:** `tests/hpc/test_direct.py::test_cpu_budget_is_shared_by_runs_on_the_same_machine`
- **Observed:** S22 (6 × 16 threads + 24), the Si phonons (5 × 8 ranks) and another user's job on 96 cores:
  everything slowed several-fold; `max_jobs` limits one run only.
- **Fix:** a cluster `cpus:` budget per machine, shared by all runs of the project (each run publishes its usage
  in `.flower/usage/<host>/<run>.json`); steps count `resources.cpus_per_task`.

## Found auditing the benchmarks for someone else to run (2026-10-03)

### 38. `$FLOWER_INPUTS` was not set on clusters
- **Test:** `tests/hpc/test_direct.py::test_cluster_step_reads_its_inputs_file`
- **Root cause:** `inputs.json` was staged into the job directory, but the job script never exported the variable.

### 39. Local steps could not use an environment recipe, so every plan named an interpreter path
- **Test:** `tests/hpc/test_envs.py::test_a_local_step_can_name_an_environment`, `..._amendment_adds_a_local_step...`
- **Observed:** all six benchmark plans ran their analysis with `/nessa/users/.../envs/abacus/bin/python`;
  nobody else could run them.
- **Fix:** `environment:` on a shell/function step without `cluster:` uses the implicit cluster `local` (this
  machine, no scheduler), defined whenever a step uses it (also for steps added by amendment). The benchmarks'
  analysis steps use the frozen `envs/analysis` recipe.

### 40. Concurrent installs of one environment corrupted it
- **Test:** `tests/hpc/test_envs.py::test_concurrent_installs_of_one_environment_wait_for_each_other`
- **Observed:** three runs gained `environment: analysis` at the same moment; all found the prefix missing and
  ran `conda create` into it together ("critical libmamba filesystem error"); two runs' steps failed.
- **Fix:** the install takes a lock per prefix (`flock`, else a lock directory) and re-checks after waiting.

## Found reproducing the J1-J2 chain (Eggert 1996) (2026-10-04)

### 41. A running plan's cluster could not be given a cpu budget
- **Test:** `tests/core/test_core_devloop.py::test_sync_tunes_a_cluster_without_rerunning`
- **Observed:** `cpus: 16` was added to the `remote` cluster of a running draft, then a step with
  `--cpus 8` was added: three 8-core items started at once. A run's clusters were fixed at creation, and
  the edit was reported only as a note that `--json` output dropped, and that a `| tail` cut off.
- **Fix:** a cluster's pacing settings (`cpus`, `max_jobs`, `min_poll`) may change in a running plan
  (amendment op `tune_clusters`); host, paths and prelude stay fixed. `flower sync RUN` applies the plan
  file's edits without re-running anything (`rerun` would have restarted the step). Text notes now
  appear in `--json` output as `message`. `flower add` gained `--cpus` / `--mem` (`resources:`).

### 42. Finished steps with an environment were seen as edited
- **Test:** `tests/core/test_core_devloop.py::test_an_added_environment_step_is_not_seen_as_edited`
- **Observed:** `flower sync` proposed to re-run two finished, unedited steps, and asked for approval. In
  the run, a step with `environment:` (on the implicit cluster `local`) carries `stage_in: []`,
  `retrieve: []` and `resources: {}`; the plan file's copy does not. The proposal listed them as
  "+ None".
- **Fix:** the edit comparison treats empty values as absent. The amendment overview names replaced
  steps, and lists tuned or new clusters and new inputs.

### 43. A rerun silently skipped plan-file edits outside its reach
- **Test:** `tests/core/test_core_devloop.py::test_rerun_names_edits_it_does_not_apply`
- **Observed:** two steps (`preview`, `analysis`) were edited; `flower rerun RUN preview` applied only the
  preview's edit. `analysis` kept its old definition, ran with it when its inputs were ready, and the report
  silently used the wrong fit.
- **Fix:** a rerun names the edited steps it does not apply and points to `flower sync RUN`.

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
