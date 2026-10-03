# HPC `job` node: bugs found by tests/hpc

> **Status (2026-10-03): all bugs below are FIXED.** Their tests were converted from strict xfail into
> ordinary regression tests; a few tests that encoded the old behaviour were updated to the corrected design
> (poll backoff, duplicate detection via sacct ExitCode 97, sticky signals, child_rc recovery). Kept as a record.

Each bug has a test marked `xfail(strict=True)`. Run `pytest tests/hpc --runxfail` to see them fail.
Line numbers refer to the source as of 2026-10-03.

Severity:
- **P1** loses or duplicates work, orphans Slurm jobs, or wedges a run.
- **P2** gives a wrong verdict or class, or behaves differently from the design in `context/notes/hpc-execution.md` §8.
- **P3** hardening.

---

## P1-1. A Slurm outage (squeue + sacct both failing) marks RUNNING jobs LOST

- **Test:** `test_job_robustness.py::test_squeue_and_sacct_down_never_declares_lost`
- **Observed:** the job is RUNNING. `squeue` fails ("Unable to contact slurm controller") and `sacct` fails
  ("Problem talking to the database"). After 5 polls longer than `lost_after`, `job.lost` is emitted. The
  attempt fails as `lost`, and with a retry policy a second copy is submitted while the first is still running.
- **Expected:** a poll that cannot reach the scheduler is a poll error. It must never change the verdict
  (DEV_PLAN D7, §8.4), and it must not count as a miss.
- **Root cause:**
  - `hpc/slurm.py:188` `observe()` treats an empty squeue/sacct section as "job absent" even when
    `parse_poll` set `squeue_ok=False` / `sacct_ok=False`.
  - `executors/job.py:219` `_advance()` then counts the miss.
  - The poll script swallows stderr (`2>/dev/null || echo @@SQUEUE_ERR`), so the reason is lost too.
- **Fix:**
  - In `_advance`, when `obs["state"] == "UNKNOWN"`, increment `misses` only when `poll["squeue_ok"] and poll["sacct_ok"]`.
  - Otherwise emit `job.remote_error{op: "poll"}` and return `None`.
  - Also return `squeue_ok`/`sacct_ok` from `observe` and keep the stderr text: `2>&1 >/dev/null` into an
    `@@SQUEUE_ERR <msg>` line.

## P1-2. Truncated poll output (shell died, rc != 0) is treated as authoritative and leads to LOST

- **Test:** `test_job_robustness.py::test_truncated_poll_output_is_a_poll_error`
- **Observed:** the remote shell dies in the middle of the poll. This is simulated with `kill -9 $PPID` from sacct,
  which is the same as an ssh drop with partial stdout. The output stops after `@@SACCT`, with no `@@EV` sections.
  The job is no longer in squeue, so it is UNKNOWN on every poll and is declared LOST, even though
  `.flower/ec` = 0 exists.
- **Expected:** a poll that did not complete is a poll error.
- **Root cause:** `executors/job.py:199`: `if r.rc != 0 and not r.out:`. Only a failure with *empty* output is
  treated as a poll error.
- **Fix:**
  - End `poll_command` with `echo @@END`.
  - Treat `r.rc != 0 or "@@END" not in r.out` as a poll error, the same as the existing branch.
  - Alternatively, in `parse_poll`, flag every job id that has no `@@EV` section as "not observed", not as "missing".

## P1-3. Submit dedupe can submit a second Slurm job for the same attempt, and the node then fails while the original job is orphaned

- **Test:** `test_job_robustness.py::test_crash_before_jobid_written_with_squeue_down_does_not_duplicate`
- **Observed:**
  1. sbatch succeeded and its id is in `.flower/jobid.tmp`, but the process died before
     `mv jobid.tmp jobid`.
  2. On re-submit, `squeue` fails transiently and sacct has not seen the job yet (slurmdbd lag).
  3. `submit_command` reads the empty lookup as "nothing exists" and runs sbatch again. There are now 2 Slurm jobs
     with the same submit key.
  4. The duplicate correctly exits 97, but flower tracks the duplicate's id. The node fails as
     `exit_nonzero` ("Slurm reports FAILED (exit 97:0)") while job #1 keeps RUNNING, untracked.
- **Expected:** exactly one Slurm job per attempt. Re-attach to job #1.
- **Root cause:** `hpc/slurm.py:110-113`:
  - The lookup is `squeue … 2>/dev/null | … || true`, so a squeue *failure* is the same as "not found".
  - `.flower/jobid.tmp` (written by `sbatch --parsable`) and `.flower/owner/id` are never consulted.
- **Fix:** in `submit_command`, before calling sbatch:
  1. If `.flower/jobid.tmp` is non-empty and parses as an id, `mv` it to `jobid` and print `EXISTING`.
  2. If `.flower/owner/id` exists, print `EXISTING $(cat owner/id)`.
  3. Run squeue without `|| true` and capture its status. If squeue fails, `exit 75` (transient) instead of
     falling through to sbatch, and do the same when sacct fails.
  4. In `verdict`/`_advance`, if the tracked job ended with sacct ExitCode 97 and `.flower/owner/id` names
     another id, switch `job_id` to the owner and emit `job.orphan_detected`. Do not fail the node.

## P1-4. Cancelling inside the crash window reports "cancelled before submission" and leaks a running Slurm job

- **Test:** `test_job_robustness.py::test_cancel_in_crash_window_does_not_leak_job`
- **Observed:** sbatch ran but `job.submitted` never reached the journal (crash). `flower cancel --node a`
  marks the node `cancelled` ("cancelled before submission"). The Slurm job keeps running to COMPLETED, using
  allocation, and nothing tracks it.
- **Expected:** the job is found (jobid file or name lookup) and `scancel`'d, or it never starts its payload.
- **Root cause:**
  - `executors/job.py:296-308` `cancel()` only scancels when `ctx.job["job_id"]` is known.
  - `poll_many` (`job.py:177`) short-circuits on `stage/cancel` without re-attaching.
  - The batch script (`slurm.py:69` `render_job_script`) never checks `.flower/cancelled`.
- **Fix:**
  - In `cancel()`, when `job_id` is unknown but `job_dir` is known, run one remote command: read
    `.flower/jobid` (or `jobid.tmp`), or `squeue`/`sacct --name=<submit_key>`, and `scancel` what it finds.
  - Add `[ -e .flower/cancelled ] && { echo cancelled > .flower/ec.tmp; …; exit 0; }` to `job.sh`, right
    after the owner guard, so a queued orphan exits immediately.

## P1-5. Crash between `node.started` and `job.submit_intent` wedges the run (KeyError on every tick)

- **Test:** `test_job_robustness.py::test_crash_before_submit_intent_recovers`
- **Observed:** the process dies inside `JobExecutor.start` before `job.submit_intent`. Every later `tick()` raises
  `KeyError: 'job_dir'`, and the run can never progress or be inspected through `tick`.
- **Root cause:**
  - `executors/job.py:187` `poll_many` calls `self._submit()` for any attempt with no job_id.
  - `job.py:138` `_submit` then reads `ctx.job["job_dir"]`, but `ctx.job` is empty because no intent was recorded.
- **Fix:** in `poll_many`, if `ctx.job` has no `submit_key`, nothing remote can have happened (the write-ahead
  rule), so there are two options:
  - (a) re-run `self.start(ctx)`, which emits the intent and then submits; or
  - (b) return `Outcome.fail("lost", "crashed before submit intent", retryable=True)`.

  (a) is the better choice.

## P1-6. Any exception inside `JobExecutor.poll_many` crashes the whole tick, every tick

- **Test:** `test_job_robustness.py::test_exception_during_resubmit_does_not_crash_tick`
- **Observed:** the first sbatch hits a transient error. Before the backoff retry, a `stage_in` source disappears.
  The retry raises `FlowerError("stage_in")` out of `poll_many` and out of `Engine.tick()`. The same happens on
  every following tick, so the run is wedged.
  - Other exceptions reach the same path: an `OSError` from `LocalTransport.put` (`shutil` errors are not
    wrapped), outputs.json permission errors in `_collect`, and P1-5.
- **Root cause:**
  - `engine.py:413` calls `outcomes = ex.poll_many(ctxs)` with no try/except. Only the per-ctx `poll()` path has
    the "never crash the tick" guard.
  - `job.py:146` catches only `_Remote`, so `FlowerError`/`OSError` raised by `_stage` escape.
- **Fix:**
  - In `JobExecutor.poll_many`, wrap the per-ctx resubmit and `_advance` in try/except.
  - Turn `FlowerError` into `Outcome.fail(exc.code, exc.message, retryable=exc.code in RETRYABLE_DEFAULT)`,
    and anything else into `Outcome.fail("internal", …)`.
  - Defensively, wrap the `poll_many` call in `engine.py:413` too.

## P1-7. A transient failure while retrieving a COMPLETED job's outputs throws the results away

- **Test:** `test_ssh.py::test_ssh_retrieve_blip_does_not_discard_completed_job`
- **Observed:** with an ssh transport, a single rsync download failure after the job COMPLETED fails the attempt as
  `remote`.
  - With the default policy (`max_attempts: 1`), the node fails.
  - With `retry`, the **whole job is resubmitted**. The test sees 2 attempts and 2 Slurm jobs, even though the
    first job's results are on the cluster.
- **Expected:** REMOTE_ERROR is retried with backoff as an infra step and never consumes job attempts
  (§8.1/§8.5: "retry redoes a stuck infra step").
- **Root cause:** `executors/job.py:258`:
  `return Outcome.fail("remote", "could not retrieve outputs …", retryable=True)`.
- **Fix:** on retrieve failure:
  - emit `job.remote_error{op: "retrieve", retry_at}`;
  - leave the attempt `running` with `ctx.job["state"] == "EXITED"`;
  - return `None`;
  - on the next eligible tick, call `_collect` again (the `job.exited` event is already idempotent);
  - fail as `remote` only after `MAX_REMOTE_ERRORS`.

---

## P2-1. Permanent sbatch rejections are retried for about 50 minutes instead of failing at once

- **Test:** `test_job_robustness.py::test_permanent_sbatch_rejection_fails_fast`
- **Observed:** `sbatch: error: Batch job submission failed: Invalid partition name specified` (exit 1) is recorded
  as `job.remote_error{transient: false}`. It is then retried with 30/60/120/…/1200 s backoff, 8 times (about 52 min),
  before the node fails as `remote`. The same happens for a missing `remote:` stage_in source (`cp -r` fails).
- **Expected:** §8.6: "These [transient] become REMOTE_ERROR with backoff; everything else is a permanent step
  error."
- **Root cause:** `executors/job.py:146-157`. `res.transient` is computed and journalled but never consulted.
- **Fix:** in `_submit`, if `not res.transient`, raise/return `Outcome.fail("submit", msg, retryable=False)`. A
  FlowerError from `start()` works, and so does an outcome stored for `poll_many` to return. Back off only for
  transient errors.

## P2-2. Cluster unreachable when a job node starts: the node fails

- **Test:** `test_ssh.py::test_ssh_unreachable_at_start_is_retried`
- **Observed:** `start()` probes `$HOME` over ssh. On failure (rc 255, "Connection timed out") it raises
  `FlowerError("remote")`. The engine records an attempt failure, so with the default policy the node fails
  and the run fails.
- **Expected:** a REMOTE_ERROR with backoff, like a failed submit. It never consumes an attempt.
- **Root cause:** `executors/job.py:66-71`.
- **Fix:**
  - Emit `job.submit_intent` without a resolved job_dir, or defer the HOME probe into `_submit`/`_stage` so it goes
    through the `_Remote` backoff path.
  - Resolve `~` remotely instead. For example, keep `remote_root` with `~` and let the remote shell expand it via
    `cd ~/…` in `submit_command`/`poll_command`, recording the absolute path from the submit output.

## P2-3. Poll errors have no backoff: an outage floods the journal

- **Test:** `test_job_robustness.py::test_poll_errors_back_off`
- **Observed:** during an ssh outage, flower retries the poll at every `min_poll` and appends one
  `job.remote_error` event per live job per poll. The test sees 20 failed polls in 20 s for 3 jobs, which is 60 events.
  - With `min_poll: 30s` and 50 jobs, an 8 h outage writes about 48k events.
  - `Journal.append` re-parses the whole file on every append, so ticks slow down quadratically.
- **Expected:** §8.4: "on ssh/timeout/slurmctld error: job.remote_error(op=poll) w/ backoff (30,300,1200)".
- **Root cause:** `executors/job.py:199-207`. The error is stored in the cluster cache, but there is no
  `retry_at`, and the event is emitted per job.
- **Fix:**
  - Keep `poll_errors`/`retry_at` in `cache/cluster-<name>.json`, and skip polling until `retry_at`, with
    backoff 30, 300, 1200 s.
  - Emit at most one `job.remote_error{op: "poll"}` per cluster per state change, not one per job per poll.

## P2-4. The duplicate-guard class `duplicate` is unreachable, and a payload exiting 97 is misclassified

- **Tests:**
  - `test_slurm_unit.py::test_verdict_duplicate_guard_seen_via_sacct_exitcode`
  - `test_slurm_unit.py::test_verdict_payload_exit_97_is_exit_nonzero`
- **Observed:**
  - The in-job guard exits 97 *before* `.flower/ec` is written, so `verdict()` never sees `ec == 97` for a real
    duplicate. A real duplicate is reported as `exit_nonzero` ("Slurm reports FAILED (exit 97:0)"); see P1-3 for
    the end-to-end effect.
  - Conversely, a user payload that legitimately `exit 97`s writes `ec=97` and is misclassified as `duplicate`.
- **Root cause:**
  - `hpc/slurm.py:235`: `if ec == 97:` keys on the payload's ec.
  - sacct `ExitCode` (`obs["exit"]`) is never parsed.
- **Fix:**
  - Have the guard write `.flower/duplicate.$SLURM_JOB_ID` and report it as evidence.
  - Alternatively, parse the sacct `ExitCode` `"97:0"` into `obs["exit_code"]` and test
    `exit_code == 97 and ec is None` (the owner's ec belongs to another id).
  - Drop the `ec == 97` rule.

## P2-5. Relative `stage_in` paths resolve against the ticking process's cwd, not the plan directory

- **Test:** `test_job_robustness.py::test_relative_stage_in_resolves_against_plan_dir`
- **Observed:** a run created in `project/` with `stage_in: [POSCAR]`, then ticked from another directory (for
  example `flower tick --all` from cron, or a detached driver), fails with
  `stage_in: stage_in source POSCAR does not exist`.
- **Root cause:**
  - `executors/job.py:111-127` uses `Path(src)` / `os.path.abspath(src)` as given.
  - The engine resolves a relative `cwd` against the run's `plan_dir` (`engine.py` `_start`), but not `stage_in`.
- **Fix:** resolve relative non-`remote:` `from` paths against `st.meta["plan_dir"]` (or the plan `_source.dir`)
  in `Engine._render`, or pass `plan_dir` in `NodeCtx` and resolve in `_stage`.

## P2-6. Short state code `RS` (RESIZING) is mapped to QUEUED

- **Test:** `test_slurm_unit.py::test_observe_short_code_RS_is_resizing_running`
- **Root cause:** `hpc/slurm.py:18`. `"RS"` is in `QUEUED_STATES`, but in Slurm `RS` = RESIZING, which is a running
  job. `RD` (RESV_DEL_HOLD) and `RF` (REQUEUE_FED) are missing.
- **Fix:** move `"RS"` to `RUNNING_STATES`, and add `"RD"`, `"RF"` to `QUEUED_STATES`.

---

## P3-1. Fractional `time` minutes become `--time=0:00:00`, which Slurm reads as no limit

- **Test:** `test_slurm_unit.py::test_sbatch_time_fractional_minutes_not_truncated_to_unlimited`
- **Root cause:** `hpc/slurm.py:60`, `int(v)` truncation: `time: 0.5` → `0:00:00`, and `time: 1.5` → `0:01:00`.
- **Fix:** `secs = round(float(v) * 60)`, then format `H:MM:SS` from the seconds, with a minimum of 1 s. Reject
  `<= 0` in plan validation.

## P3-2. Resource values are not validated, so newlines inject shell lines into the batch script

- **Test:** `test_slurm_unit.py::test_sbatch_directives_reject_newline_injection`
- **Observed:** `resources: {partition: "debug\ntouch /tmp/pwned"}` renders a bare `touch /tmp/pwned` line into
  `job.sh`. `resources` is a template-rendered field (`engine.py:501` `RENDER_FIELDS`), so a value from an upstream
  agent's outputs reaches the script.
- **Root cause:** `hpc/slurm.py:61`, `lines.append(f"#SBATCH {flag}={v}")` with no validation (the same applies to
  `extra`).
- **Fix:** reject values containing `\n`/`\r` (ValueError → plan/contract error), or `shlex.quote` them. Validate
  that `extra` items start with `--`.

---

## Notes (no test, lower priority)

- `job.remote_error` events for **poll** errors also increment `job["remote_errors"]` in the fold
  (`state.py`, `job.*` branch). This is harmless today, because `MAX_REMOTE_ERRORS` is only checked before
  submission, but the counter is misleading in `status`.
- §8.3 says that when the name lookup finds more than one id, flower should keep the earliest and cancel the
  rest (`job.orphan_detected`). `submit_command` takes `head -1` silently, and `job.orphan_detected` is never
  emitted.
- On `lost`, flower does not `scancel` the old job id before retrying. If the lost verdict was wrong
  (P1-1/P1-2), two attempts then run concurrently in different directories.
- Environment note for running this suite: on this host `BASH_ENV=~/.bashrc` makes every `bash -c` cost ~0.5 s;
  the conftest unsets it. `/etc/profile` here resets PATH to an ancient `/bin/bash` 2.05b (no `pipefail`), so the
  fake ssh client runs `bash -lc` as `bash -c`.
- `ssh -o BatchMode=yes localhost true` fails on this host ("Host key verification failed"), so
  `test_real_ssh_localhost` is skipped. The ssh transport is covered end to end through a fake `ssh` client
  on PATH that also carries rsync.
