# Durable HPC job execution: what mature projects do and a design for flower's `job` node

Scope: how seven projects (jobflow-remote, dpdispatcher, psij-python, snakemake-executor-plugin-slurm, catgo,
aiida-core, dflow) plus Eleforge's own `apps/backend/compute` submit, track, recover and cancel Slurm jobs over SSH.
The last section is a concrete design for flower, with each element tagged by the project it comes from.

Paths are relative to `flower/context/repos/` unless they start with `apps/` (Eleforge). catgo is AGPL-3.0, so
only its designs are described here and none of its code is copied.

---

## 0. TL;DR

- **Everyone converges on the same skeleton.** It runs stage-in, then submit, then the job sits with the scheduler,
  then it leaves the queue with no verdict yet, then retrieve, then parse/verdict. The "left the queue" state is the
  key one: jobflow-remote calls it `RUN_FINISHED`, aiida `JobState.DONE` ("There is no FAILED state"), and
  dpdispatcher `terminated` vs `finished`. Success is decided afterwards from files the job wrote itself: an exit-code
  file, a finish tag or an output JSON. The scheduler state alone does not decide it.
- **Double submission is a known, mostly unsolved hole.** aiida, jobflow-remote, catgo and dpdispatcher all record
  the job id *after* `sbatch` returns, so a crash in that window means either a resubmission (catgo: "no job_id →
  READY") or a lost job. Only **Eleforge** closes the hole properly. It writes a remote marker before `sbatch`, tags
  the job with a deterministic `--job-name=<submit_key>` and `--comment=<fingerprint>`, and searches
  `squeue`/`sacct` for that tag before every submit and on recovery. dpdispatcher adds a remote `<hash>_job_id`
  file. flower should combine the two.
- **Jobs that vanish from the queue are normal.** `squeue` drops a job after `MinJobAge` (default 300 s), and
  `sacct` can lag at submission or be missing entirely. The mature pattern is batched `squeue` for live jobs, then
  `sacct` for jobs that left the queue (snakemake, Eleforge, catgo), then evidence files in the work dir (Eleforge,
  psij, dpdispatcher). It also needs grace and miss-counters before calling a job lost (snakemake, Eleforge).
- **You don't need a daemon.** catgo's engine is an explicitly "stateless periodic scanner". dpdispatcher's
  `run_submission(exit_on_submit=True)` re-enters by recovering from its remote JSON. Re-entrant `tick` calls under
  a file lock, plus an optional cron/daemon wrapper, are enough.
- **Recommendation: don't depend on any of these libraries for the core.** Port and slim Eleforge's Slurm/OpenSSH
  code (the user's own code, about 600–900 LOC), borrowing named algorithms from aiida and snakemake (MIT, can copy
  with attribution) and designs from jobflow-remote (BSD-3). psij (MIT, light) is the only reasonable optional
  dependency, and only for a future "run flower on the login node" mode, because it has **no SSH**. dpdispatcher
  (LGPL-3.0) is usable but its blocking, hash-keyed, batch-of-tasks model fights an amendable DAG. aiida and
  jobflow-remote are far too heavy (ORM/RabbitMQ, MongoDB/supervisord).

---

## 1. Job state machines

### jobflow-remote (BSD-3): 16 states, runner-driven
`jobflow-remote/src/jobflow_remote/jobs/state.py:10`
```
WAITING, READY, CHECKED_OUT, UPLOADED, SUBMITTED, RUNNING, RUN_FINISHED, DOWNLOADED,
REMOTE_ERROR, COMPLETED, FAILED, PAUSED, STOPPED, USER_STOPPED, BATCH_SUBMITTED, BATCH_RUNNING
```
- Happy path: `READY → CHECKED_OUT → UPLOADED → SUBMITTED → RUNNING → RUN_FINISHED → DOWNLOADED → COMPLETED`.
  Each arrow is one locked "step" (`runner.py:629 advance_state` dispatches `upload`/`submit`/`download`/`complete_job`).
- `RUN_FINISHED` is deliberately verdict-free. From `doc/source/user/states.rst`: "No knowledge of whether this
  happened for an error or because the Job was completed correctly is available at this point."
- **`REMOTE_ERROR` vs `FAILED`** is the key split. REMOTE_ERROR means "An error occurred during the procedure to
  execute the Job … The Job may or may not be executed". FAILED means the job's own function raised. Infra errors
  never fail the Flow (`FlowState.from_jobs_states`: "REMOTE_ERROR state does not lead to a failed Flow … it might
  be a temporary problem").
- Groupings worth copying: `RUNNING_STATES`, `RESETTABLE_STATES`, `PAUSABLE_STATES`, `ERROR_STATES` (state.py:64–98).
- The `jf job retry` (redo the stuck remote step) vs `jf job rerun` (restart the job from READY with children) split
  is in `doc/source/user/errors.rst`.

### catgo (AGPL, design only): 17 states, DB-backed scanner
`catgo/server/catgo/workflow/states.py:8` (docstring says "14-state", enum has 17):
```
WAITING, READY, GENERATING, UPLOADING, SUBMITTED, QUEUED, RUNNING, COMPLETED_REMOTE, COLLECTING,
COMPLETED, FAILED, REMOTE_ERROR, PENDING_REVIEW, PAUSED, CANCELLED, SKIPPED, MAPPED
```
- It splits `SUBMITTED` ("sbatch done, got job_id") from `QUEUED` ("SLURM PENDING"). `COMPLETED_REMOTE` ("HPC done,
  results on remote") equals jobflow-remote's RUN_FINISHED.
- `PENDING_REVIEW` ("Local done, waiting for user confirm before HPC submit") is a gate fused into the job.
  flower keeps `gate` as a separate node kind, but the per-job "inputs generated, review before submit" stop is
  still useful.
- `REMOTE_ERROR` carries an `error_type` field. `"transient"` (SSH) errors "should NOT consume retries or escalate
  to FAILED" (`workflow/engine/error_handler.py`). `"compute"` errors go through retry and smart recovery.
- `MAPPED` is for fan-out controller nodes that spawn children.

### aiida-core (MIT): two orthogonal axes
- Engine sub-state: `CalcJobState` (`aiida-core/src/aiida/common/datastructures/_calcjob.py:47`) =
  `UPLOADING, SUBMITTING, WITHSCHEDULER, STASHING, UNSTASHING, RETRIEVING, PARSING`.
- Scheduler state: `JobState` (`common/datastructures/_scheduler.py:47`) = `UNDETERMINED, QUEUED, QUEUED_HELD,
  RUNNING, SUSPENDED, DONE`. The docstring reads: "There is no FAILED state as every completed job is put in DONE,
  regardless of success."
- The outcome is decided in PARSING and by `Scheduler.parse_output` mapping sacct `State` to exit codes
  (`schedulers/plugins/slurm.py` ~l.750: `OUT_OF_MEMORY → ERROR_SCHEDULER_OUT_OF_MEMORY`,
  `TIMEOUT → ERROR_SCHEDULER_OUT_OF_WALLTIME`, `NODE_FAIL → ERROR_SCHEDULER_NODE_FAILURE`).
- Lesson: **keep "our pipeline step" and "the scheduler's view" as separate fields.** Typed scheduler failure
  codes then drive the retry policy.

### dpdispatcher (LGPL-3.0)
`dpdispatcher/dpdispatcher/utils/job_status.py:6`:
`unsubmitted=1, waiting=2, running=3, terminated=4, finished=5, completing=6, failed=7, unknown=100`.
- `terminated` means "left the queue without the finish tag" and is auto-resubmitted. `finished` means "finish tag
  exists". `failed` means retries were exhausted. The scheduler's own state is used only for queued vs running.

### psij-python (MIT): the portable standard
`psij-python/src/psij/job_state.py:31–66`:
`NEW, QUEUED, STAGE_IN, ACTIVE, STAGE_OUT, CLEANUP, COMPLETED(final), FAILED(final), CANCELED(final)`.
- Each state has an `order` so stale updates can't move the state backwards (`is_greater_than`). COMPLETED, FAILED
  and CANCELED all have order 6. `_PREV_STATE` defines the canonical chain.
- COMPLETED means "completed *successfully* (i.e., with a zero exit code)". The verdict comes from the `.ec` file,
  not from Slurm.
- The Slurm mapping is `executors/batch/slurm.py:39 _STATE_MAP` (e.g. `CG→ACTIVE`, `TO→FAILED`, `PD/RD/RF/RH→QUEUED`,
  `OOM/NF/BF/DL→FAILED`).

### snakemake-executor-plugin-slurm (MIT)
There is no enum: the plugin keeps the job active or reports success or error. Its
`fail_stati = ("BOOT_FAIL","CANCELLED","DEADLINE","FAILED","NODE_FAIL","OUT_OF_MEMORY","TIMEOUT","ERROR")`
(`snakemake_executor_plugin_slurm/__init__.py:1270`) is a good canonical list. `PREEMPTED` and `NODE_FAIL`
with requeue stay active.

### dflow (LGPL-3.0)
dflow delegates everything to Argo. `SlurmRemoteExecutor` (`dflow/src/dflow/slurm.py:280`) runs a pod holding a Go
`bin/slurm` poller with `jobIdFile: /tmp/job_id.txt` (on a PVC if configured, so an Argo retry reattaches).
`DispatcherExecutor` wraps a dpdispatcher `run_submission` in a pod. **Anti-pattern for flower:** a live process
(pod) is held for the whole job duration.

### Eleforge (brief; covered in depth elsewhere)
`apps/backend/compute/runners/slurm/runner.py:472 _map_slurm_state` normalizes into the platform `JobStatus`.
Terminal state can also come from work-dir evidence: `canceled` marker, then `result.json`.

---

## 2. Submission idempotency and crash recovery

| Project | When the job id is persisted | Guard against double submit | Recovery after crash |
|---|---|---|---|
| aiida | After `sbatch` returns: `calculation.set_job_id(result)` (`engine/daemon/execmanager.py:428`) | Skips sbatch if the job id is already set: "this function was already executed once … we do not want to submit it a second time" (`execmanager.py:412–418`); `task_submit_job` skips if already `WITHSCHEDULER` (`calcjobs/tasks.py:134`) | The process is checkpointed in the DB and the daemon reloads it. **Window:** a crash between sbatch's return and `set_job_id` means a resubmission |
| jobflow-remote | After sbatch: `lock.update_on_release = {"remote.process_id": …, "state": SUBMITTED}` (`jobs/runner.py:822`) | A Mongo document lock per job (`lock_job_for_update`, `jobcontroller.py:4636`), with locks expiring after `lock_timeout=86400` (`config/base.py:60`). `SubmissionStatus.JOB_ID_UNKNOWN` → `RemoteError(no_retry=True)`: "submission succeeded but ID not known" (runner.py:860) | A stuck lock or step is reset by the user (`jf job retry`, `break_lock`). The same window exists |
| dpdispatcher | Right after sbatch the job id is written **remotely** to `<job_hash>_job_id` (`machines/slurm.py:98,137`; a separate SFTP write from the local process, not atomic with sbatch), and to `<submission_hash>.json` *after* the submit loop (`submission.py:396 submission_to_json`) | Content-hash identity: `submission_hash`/`job_hash` = sha1 of the static content (`submission.py:1983`). Re-running the same submission recovers instead of resubmitting | `try_recover_from_json()` (`submission.py:1041`) reads `<hash>.json` **from the remote root**, and finish tags reconcile per-task completion. A crash between sbatch and `submission_to_json` still resubmits (the `_job_id` file is write-only: grep shows no reader anywhere in the package) |
| catgo engine | After sbatch plus an immediate poll: `db.update_task(…, hpc_job_id=job_id)` (`workflow/engine/submitter.py:~413`) | None beyond the DB status | `_recover_transient_errors` (`scanner.py:476`): "If no hpc_job_id (submit never finished), reset to READY for re-submission" means **known double-submit risk** |
| catgo campaign skill | `STATUS.md` written after sbatch (`skills/campaign/scripts/campaign_lib.py:~521`) | "Idempotent re-entry guard: refuses if this calc already has a STATUS.md with state RUNNING/PENDING (a resumed agent must not double-submit a live job)" (l.~466). Correct instinct, but it is file-first and only guards *after* the record exists | `poll.py` re-reads STATUS.md files |
| snakemake slurm | In memory only (`SubmittedJobInfo`) | `--job-name <run_uuid>` per workflow run, `--comment <rule/wildcards>` (`submit_string.py:87–91`) | **None.** A restart doesn't reattach: "Leave Snakemake running, if possible. Otherwise Snakemake needs to restart this job upon a Snakemake restart." Incomplete outputs are rerun |
| psij | Caller's responsibility | — | `executor.attach(job, native_id)` (`batch_scheduler_executor.py:283`) re-registers a known native id with the poller. Exit code and output are recovered from `<native_id>.ec/.out` |
| **Eleforge** | Marker `<workdir>/.nanoforge-submit.json` written **before** sbatch (`runners/slurm/runner.py:565,600`) | **`_find_existing_slurm_job`** (runner.py:420) runs `squeue -h -o '%A|%j|%k'` and `sacct -n -P -X -o JobIDRaw,JobName,Comment,Submit`, filtered on `JobName == submit_key` and `Comment == fingerprint`, before every sbatch. A match reuses the existing id. Ambiguity → `reattach_ambiguity_failures` metric, return None | `recover_missing_remote_ref` (`jobs/manager_reconcile.py:214`) calls `runner.reattach(submit_key, fingerprint, …)` for snapshots that have a submit key but no `remote_ref` |

Lessons:
1. **Write intent before acting, and tag the scheduler job with a deterministic key** (Eleforge). sbatch is not
   idempotent, but `--job-name`/`--comment` let you *look the job up* afterwards. Caveat: sacct shows `Comment` only
   if the site sets `AccountingStoreFlags=job_comment`. Match on JobName first and treat Comment as a tiebreaker,
   which is what Eleforge does.
2. **Make the remote side write the job id itself** (dpdispatcher). Run `sbatch --parsable … > jobid.tmp && mv`
   inside the *same remote shell command*, so the id survives even if the local process or the SSH channel dies
   mid-call.
3. **Use `sbatch --parsable`** (dpdispatcher, snakemake). It outputs `<id>[;cluster]`, so split on `;`. snakemake
   still validates the id because "some cluster admin give convoluted sbatch outputs"
   (`__init__.py:1217 validate_or_get_slurm_job_id`).
4. **Never reset to READY just because the id is missing** (counter-example: catgo). Missing id plus remote
   evidence means reattach. Missing id plus no evidence after a grace period means resubmitting under the same
   attempt key is safe. Anything ambiguous should surface as `LOST`.

---

## 3. Status polling

### snakemake-executor-plugin-slurm (the most battle-tested poller)
`snakemake_executor_plugin_slurm/__init__.py:1254 check_active_jobs`, `job_status_query.py`
- **One query per cycle for all jobs of the run, filtered by job name = run UUID:**
  ```
  sacct -X --parsable2 --clusters all --noheader --format=JobIdRaw,State \
        --starttime <now-2days, hour-rounded> --endtime now --name <run_uuid>
  ```
  `-X` gives "only show main job, no substeps". The `starttime` format keeps compatibility with Slurm < 20.11
  (`job_status_query.py:108–135`). The squeue alternative is
  `squeue --format='%i|%T' --states=all --noheader --name <run_uuid>`.
- **Choice of command:** default is `sacct` if available. `squeue` is recommended only if `MinJobAge ≥ 120 s`,
  parsed from `scontrol show config` (`get_min_job_age`, `should_recommend_squeue_status_command`). The rationale is
  that "MinJobAge … defaults to 300, which implies that jobs will be removed from slurmctld within 6 minutes of
  finishing".
- **sacct lag:** a job not yet in sacct output stays active: "the job probably didn't make it into slurmdbd yet …
  should still be queueing or running" (l.~1470, `yield j`).
- **Jobs that disappear from sacct:** `active_jobs_seen_by_sacct` minus the current set gives
  `missing_sacct_status`. It retries up to `status_attempts=5` within the cycle, then warns "previously seen by
  sacct, but sacct doesn't report them any more … check slurmdbd" (l.1439).
- **Status `UNKNOWN`:** treated as success ("the job probably does not exist anymore"). This is too optimistic for
  flower.
- **Adaptive interval:** start at `init_seconds_before_status_checks` (default 40). Add 10 s for each cycle with no
  finished job, cap at `max_sleep_time = 180` ("conservative … with half that time" of MinJobAge), and reset when
  something finishes (l.1601–1607). There is also a framework-level `status_rate_limiter` (`throttler` dependency).
- **Timeout:** `asyncio.wait_for(process.communicate(), timeout=60)` per query (`job_status_query.py:~190`).
- **Failure detail:** for `NODE_FAIL` it records failed nodes and excludes them from future submissions, or leaves
  the requeue to Slurm if `--requeue` is set.

### aiida-core
- **Batched per (computer, user):** `JobsList` (`engine/processes/calcjobs/manager.py:30`): "the scheduler update
  command is not triggered for each job individually". It enforces `computer.get_minimum_job_poll_interval()`.
  It uses `squeue -u$USER` if the scheduler `can_query_by_user`, else `--jobs=a,b,c`.
- **The single-job squeue trick** (`schedulers/plugins/slurm.py:225–240`): with one id it appends the same id twice
  (`--jobs=123,123`). A single missing job makes squeue exit non-zero ("Invalid job id specified"), while with ≥2 ids
  it exits 0. The exit code can then be trusted to mean real errors (aiida issue #4326).
- `SLURM_TIME_FORMAT='standard' squeue --noheader -o '<fields joined by sep>'`.
- **A missing job means DONE:** "If the job is computed or not found assume it's done"
  (`calcjobs/tasks.py:~197`). Retrieval and parsing then decide the outcome. sacct is used only *after* the job is
  done, as `get_detailed_job_info` (`sacct --format=<all fields> --parsable --jobs=<id>`).

### psij-python
One background `_QueuePollThread` per executor (`batch_scheduler_executor.py:653`) with
`queue_polling_interval=30`, `initial_queue_polling_delay=2` and `queue_polling_error_threshold=2`.
- The command is `squeue -O JobArrayID,StateCompact,Reason -t all --me`: the user's whole queue in one call
  (`slurm.py:153`).
- "If the status of a registered job is not found in the output … it is assumed completed (or failed, depending on
  its exit code)". The verdict comes from `<native_id>.ec`.
- **Bad behavior for us:** after more than `queue_polling_error_threshold` errors it **fails all jobs**
  (`_handle_poll_error`, l.~733). A flaky connection must never mark jobs FAILED.

### jobflow-remote
Every `delay_check_run_status=30 s` (`config/base.py:37`), jobs are grouped by worker and queried in batch via
qtoolkit (`queue.get_jobs_list(jobs=ids_list, user=…)`, `runner.py:1031`). `qstate in [None, DONE, FAILED]`
becomes `RUN_FINISHED` (runner.py:1066), with an optional `delay_download` before fetching, so a missing job is
"finished" and the output file decides. A query error turns into a retried step error with backoff.

### dpdispatcher
Per job: `squeue -o "%.18i %.2t" -j <id>` (`machines/slurm.py:145`). "Invalid job id specified" or a header-only
output means the job is finished if `<job_hash>_job_tag_finished` exists, else `terminated`. Status codes are mapped
by hand. Specific stderr strings ("Socket timed out on send/recv operation", "Unable to contact slurm controller",
"Invalid user for SlurmUser") raise `RetrySignal`, which triggers `@retry(max_retry=3, sleep=60)`. The blocking
sleeps are unsuitable for an interactive CLI.

### catgo
The engine polls per task: `squeue -j <id> -h -o …`, falling back to `sacct` for finished jobs
(`utils/slurm.py:188`, `poller.py:128`). It does a health-check `echo` once per session per cycle "to detect
half-open TCP sockets before we waste 30s on a dead connection" (`poller.py _verify_connection`). The campaign skill
does the same with `squeue -j <id> -h -o %T`, then `sacct -j <id> -n -P -o State,ExitCode`. It also notes: "COMPLETED
only means the batch script exited 0 — it does NOT mean the calculation converged" (`campaign_lib.py:~225`).

### Eleforge
It runs `squeue -h -j id1,id2 -o '%A|%T'` in batch, then `sacct -j <missing> -n -P -X -o JobIDRaw,State`. If both are
silent it falls back to `_resolve_status_via_workdir`, looking for a `canceled` marker, then `result.json`, otherwise
unknown, which stays running ("集群可能没开 accounting": the cluster may not have accounting enabled). Result-fetch
misses are counted up to `_RESULT_FETCH_MISS_LIMIT = 5` before an explicit error. It "never synthesizes an empty
success" (`jobs/manager_reconcile.py:19–27`).

---

## 4. SSH and transport

| Project | Library | Reuse | Flaky-link handling | Staging |
|---|---|---|---|---|
| jobflow-remote | Fabric 3 (paramiko) (`remote/host/remote.py`) | One connection per worker held by the runner, `keepalive=60` | `retry_on_closed_connection=True`: close, reconnect, retry once (l.309–383). `auth_timeout=120` for interactive/OTP | Fabric put/get. Remote dir = `work_dir/<uuid hex sharded 3×2>/` (`utils/data.py:86 uuid_to_path`), containing `jfremote_in.json`, `jfremote_out.json`, `queue.out/err` |
| dpdispatcher | paramiko (`contexts/ssh_context.py`) | One `SSHSession`, `set_keepalive(60)`, `ensure_alive(max_check=10, sleep_time=10)` | `@retry(max_retry=6, sleep=1)` on setup. Supports ProxyCommand/jump host and TOTP/interactive | Tar-and-SFTP by default; **rsync over ssh if available** (l.442–485). Remote root `<remote_root>/<submission_hash>/`, which also holds the recovery JSON |
| aiida | paramiko `SshTransport`, plus `AsyncSshTransport` with two backends: **asyncssh** or **OpenSSH CLI** (`transports/plugins/async_backend.py:225,505`) | `TransportQueue` (`engine/transports.py:37`) batches requests, "opening of transports (a costly operation) can be minimised", rate-limited by `safe_interval` | Every transport task is wrapped in `exponential_backoff_retry` (`engine/utils.py:206`; interval doubles, default initial 20 s, `task_maximum_attempts=5`). After the max the process is **paused, not failed** ("Maximum number of transport task attempts before a Process is Paused") | Remote `<workdir>/<uuid[:2]>/<uuid[2:4]>/<uuid[4:]>/` (`execmanager.py:141`) with `_aiidasubmit.sh` and `_scheduler-stdout.txt` |
| psij | **None.** It shells out to sbatch/squeue locally | — | — | Local `work_directory` with `<native_id>.ec/.out/.nodefile` |
| snakemake | **None.** It runs on the login node | — | — | Shared FS |
| catgo engine | asyncssh pool (`utils/connection_pool.py`) | Reuse, idle cleanup, **application-level keepalive** `echo __catgo_keepalive__`; marked dead after N consecutive failures | Disconnect → `REMOTE_ERROR(transient)`. Recovery pass on reconnect probes the job (`scanner.py:476`) | `cat > file` over exec channel |
| catgo campaign | **Plain `ssh`/`scp` subprocess on an ssh alias:** "ControlMaster / ~/.ssh/config handles auth … No catgo-package coupling" (`campaign_lib.py:1–11`). `BatchMode=yes`, `timeout=120`, `bash -l -c` | User's ControlMaster | Raise and let the agent/user retry | scp per file. Remote mirrors the local tree |
| Eleforge | OpenSSH CLI (`compute/remote/ssh/openssh.py`), plus a paramiko session (`paramiko_session.py`) and a broker | **Multiplexing deliberately disabled for automated commands:** "a persistent ControlMaster wedges over flaky relayed tunnels … a half-dead master makes every submit/reconcile/watcher command hang". ControlMaster is kept only for interactive auth | `BatchMode=yes`, `ConnectTimeout=30`, `ServerAliveInterval=20`, `ServerAliveCountMax=4`; a fresh connection per command | Text push via exec. `remote_workdir_root/<job_id>/` |

Lessons:
- **OpenSSH CLI beats paramiko for a CLI tool.** It inherits `~/.ssh/config` (ProxyJump, keys, agent, Kerberos/GSSAPI,
  site OTP via a user-opened ControlMaster), adds zero dependencies and matches what researchers already use. aiida
  added an OpenSSH backend for this reason, and both catgo-campaign and Eleforge converged on it.
- **ControlMaster is a double-edged sword.** It is essential on MFA clusters (the user authenticates once
  interactively and automated calls ride the socket), but a half-dead master hangs everything (Eleforge). Policy:
  never *start* a master from automation. If the user's config has one, check it with
  `ssh -O check -o ConnectTimeout=5`, wrap every call in a hard subprocess timeout, and on a hang fall back to a
  fresh connection or report "auth needed".
- **One round-trip per cluster per tick.** Ship a small bash script over a single `ssh` that runs the squeue batch,
  the sacct batch for missing ids and the marker-file reads, and prints delimited sections. This is the
  per-command-connection equivalent of aiida's `TransportQueue` batching.
- **Use rsync for staging** (`-a --partial`, resumable), and fall back to `tar | ssh` (dpdispatcher).
- **Lay remote dirs out per attempt** and make them human-readable (catgo: "naming (never hashes)") rather than
  uuid-sharded (aiida/jobflow-remote). Researchers browse these dirs by hand.

---

## 5. Runner architecture

| Project | Model | Multiple-runner protection | Lives on the cluster |
|---|---|---|---|
| aiida | Long-running daemon workers (circus) consuming RabbitMQ tasks. Process checkpoints are stored in the DB (`engine/persistence.py`) | Per-process RMQ task ownership | Only the work dirs |
| jobflow-remote | `jf runner start`: a supervisord-managed daemon with separate transfer/queue/complete processes and `schedule` loops (`jobs/daemon.py`, `runner.py:295`). The state lives in MongoDB | DB-stored `running_runner` doc compared on `hostname`/`user`/`project_name`/`daemon_dir` (`daemon.py:1147`): "A daemon runner process associated to this database may be already running". Per-job Mongo locks with `lock_timeout` | `jf execution run <dir>` executes there and writes `jfremote_out.json` |
| catgo engine | **"WorkflowEngine — stateless periodic scanner. Each scan_cycle() reads DB, advances task states, and returns. No in-memory state between cycles. Crash and restart safely."** (`workflow/engine/scanner.py:1–5`). The scan order is WAITING→READY, run local tasks, recover transient errors, submit READY, poll, collect, handle errors, update the workflow (`_process_workflow`, l.444) | Single server process | Only the work dirs |
| catgo campaign | **No runner.** The agent calls `submit_calc.py` and `poll.py` itself, and the state is `STATUS.md` per calc dir | The STATUS.md guard | Mirror of the local tree |
| dpdispatcher | A blocking `run_submission` loop (`check_interval=30`). With `exit_on_submit=True` it returns after submitting, and **calling it again later recovers from the remote JSON**, which makes it re-entrant | Content hash | The recovery JSON, `_job_id` and finish tags live in the remote root |
| snakemake | Snakemake's main process is the poller | — | — |
| dflow | An Argo pod per step holds the poller | Argo | — |
| Eleforge | Server background loop `watch_remote_jobs_loop`/`tick`, sharded reconcile, claim tokens (`jobs/manager_reconcile.py:132–201`) | DB claim tokens | Marker, `result.json` and `canceled` files |

For flower, the catgo scanner shape (an idempotent `scan_cycle` over persisted state) plus dpdispatcher-style
re-entrancy is exactly the "reconcile on every CLI invocation" model. The jobflow-remote `running_runner`
host/user check is the right diagnostic to show when a lock is held by another machine.

---

## 6. Cancellation, retries, walltime, checkpoint-restart

**Cancellation**
- aiida `kill_calculation` (`execmanager.py:792`): `scancel <id>`. If that fails, it re-queries: "Failed to kill
  because the job might have already been completed". If the job is still listed, the kill really failed.
- Eleforge `cancel`: `scancel <id>`, then `touch <workdir>/canceled`, because "集群无 accounting 时 scancel 后
  squeue/sacct 都查不到该 job" (without accounting, neither squeue nor sacct can find the job after scancel), so the
  marker is the only evidence. snakemake: `scancel <ids> [--clusters=all]` with `timeout=60`
  (`job_cancellation.py`). dpdispatcher: `scancel -Q`.
- psij treats CANCELED as final and skips reading the exit code for it.

**Retries (two distinct counters everywhere mature)**
- *Infra step retries:* jobflow-remote has `max_step_attempts=3`, `delta_retry=(30, 300, 1200)` s, and
  `remote.retry_time_limit` stored on the doc so the next runner pass skips until then (`config/base.py:68–76`,
  `jobcontroller.py:4671`). Errors carry `no_retry`. aiida uses exponential backoff and pauses on exhaustion. catgo
  puts transient errors in REMOTE_ERROR forever without consuming retries until the connection returns.
- *Job resubmission:* dpdispatcher auto-resubmits `terminated` jobs up to `retry_count` (default 3). It
  re-uploads forward files first ("to handle cases where remote workdir was cleaned"), then on exhaustion fails
  with the last error lines (`submission.py:1853–1925`). catgo uses `max_retries=3` with three tiers: Custodian at
  runtime, then regex-based input fixes (`smart_recovery.py`: ZBRENT→IBRION=1, BRMIX→AMIX…), then PAUSED for the
  user. aiida's `BaseRestartWorkChain` (`engine/processes/workchains/restart.py:93`) has `max_iterations`,
  `@process_handler`s keyed on exit codes and `pause_on_max_iterations`. This is the canonical "retry with
  modification" pattern.
- **Retry vs rerun:** jobflow-remote's `jf job retry` redoes the failed infra step in place. `jf job rerun` resets
  the job and its children to READY.

**Walltime and checkpoint-restart**
- No project here does generic walltime checkpointing. It is handled per code: aiida's `ERROR_SCHEDULER_OUT_OF_WALLTIME`
  feeds a restart-workchain handler that copies restart files, and VASP Custodian does the same in catgo.
- dpdispatcher's per-task finish tag (`machine.py:48`: `if [ ! -f {task_tag_finished} ] … touch {task_tag_finished}`)
  is a coarse checkpoint, so a resubmitted grouped job skips tasks that already finished.
- snakemake supports `--requeue`/`--no-requeue` and lets Slurm requeue on NODE_FAIL/PREEMPTED (the job id is kept).
- Standard Slurm convention (not borrowed from these repos): `#SBATCH --signal=B:USR1@300` plus a `trap` in the
  wrapper so the application can write a checkpoint before the kill, optionally with `--requeue`/`--open-mode=append`.

---

## 7. Licenses and the depend-vs-port decision

| Project | License | Python | Core dependencies | SSH | Fit as flower's scheduler/transport layer |
|---|---|---|---|---|---|
| psij-python | **MIT** | ≥3.8 | psutil, pystache, typeguard, packaging (light) | **No.** It runs scheduler CLIs locally | Clean portable `JobSpec`/`JobState`/`attach(native_id)` API, but it would need flower itself to run on the login node. Its poll thread fails all jobs after 2 errors. Possible optional "local Slurm" backend later |
| dpdispatcher | **LGPL-3.0** | ≥3.10 | paramiko, dargs, requests, tqdm, pyyaml | Yes (paramiko, rsync) | Mature and DFT-community-proven (DeePMD/dflow). But the model is "a Submission = bag of tasks, blocking loop", identity comes from a content hash (amending inputs changes the hash), it polls per job, and `@retry` sleeps 60 s. Dynamic import is fine under LGPL, but vendoring triggers LGPL obligations |
| jobflow-remote | **BSD-3** | ≥3.10 | jobflow, pymongo, fabric, supervisor, pydantic, qtoolkit, typer… | Yes (Fabric) | Too heavy and DB-centric. Borrow its state design, step-retry schema and runner-identity check. (Its scheduler layer *qtoolkit*, also Matgenix/BSD, is a lighter script-generation and squeue-parsing library. It was not cloned here; evaluate it separately if a parser dependency is wanted) |
| aiida-core | **MIT** | ≥3.10 | ORM (SQLAlchemy/PostgreSQL or SQLite), plumpy, kiwipy/RabbitMQ… | Yes | Far too heavy to depend on. **Copy snippets with attribution:** squeue field format, single-id duplication trick, sacct-based `parse_output`, exponential backoff |
| snakemake-executor-plugin-slurm | **MIT** | ≥3.11 | snakemake interfaces, pandas, numpy, throttler | No | Not a library. **Copy algorithms:** sacct-by-name query, MinJobAge detection, adaptive interval, missing-from-sacct tracking, fail-status list |
| dflow | LGPL-3.0 | — | Argo/K8s | via pod | Not applicable |
| catgo | **AGPL-3.0** | — | — | asyncssh / OpenSSH | **Designs only.** No code copied |
| Eleforge compute | Own code | 3.12 | Internal (DB stores, metrics) | OpenSSH CLI plus paramiko | Most robust idempotency (submit key, fingerprint, marker, work-dir fallback, reattach), but entangled with the DB job store, edge control plane and metrics. Port the algorithms, not the module |

**Recommendation: port, don't depend.** Write `flower/hpc/` as a small, dependency-free layer:
`ssh.py` (OpenSSH subprocess wrapper with timeouts and a transient-error classifier), `slurm.py` (script render,
submit, batch status query, cancel, parsers) and `staging.py` (rsync/tar).
- Reasons: (a) flower's identity is the plan file plus the event log, and every library above brings its own
  persistence model (Mongo, ORM, hash JSON, in-memory) that would have to be reconciled with ours. (b) The
  hard-won logic is about 300 lines of algorithm, and the user already owns the best version (Eleforge). (c) The
  OpenSSH CLI gives the widest auth compatibility for zero dependencies. (d) Python ≥3.10 can be kept
  without pandas or numpy (the snakemake plugin's dependencies).
- Tradeoff: we own Slurm quirks (version-specific flags, multi-cluster `;cluster` ids, sites without accounting).
  Mitigate this with the aiida/snakemake/dpdispatcher-derived parsers and a fixture suite of real `squeue`/`sacct`
  outputs. Define a `Scheduler` protocol shaped like psij (`submit`, `status_batch`, `cancel`, `attach`) so PBS/LSF
  or a psij-backed "on-login-node" adapter can be added later without changing the node runtime.
- **Don't** use dpdispatcher as the default backend. Users who already have dpdispatcher `machine.json` files could
  get an optional `job` backend `dpdispatcher` that calls `run_submission(exit_on_submit=True)` re-entrantly, which
  is LGPL-compatible as an optional import. That is a nice-to-have, not core.

---

## 8. Recommended design for flower's `job` node and runner

### 8.1 State enum (job sub-state; node-level READY/BLOCKED/etc. stay generic)
```
PREPARED       inputs rendered locally, script generated         [catgo GENERATING; optional review stop ← catgo PENDING_REVIEW]
STAGING        uploading inputs to remote attempt dir            [jobflow-remote UPLOADED, aiida UPLOADING, psij STAGE_IN]
SUBMITTING     intent durably recorded; sbatch may or may not    [aiida SUBMITTING; Eleforge marker-before-sbatch]
               have happened  ← the only ambiguous state
QUEUED         scheduler accepted (PD/CF/RH…)                    [catgo QUEUED, psij QUEUED]
RUNNING        R/CG                                              [psij ACTIVE]
EXITED         left the queue; verdict pending                   [jobflow-remote RUN_FINISHED, aiida DONE, catgo COMPLETED_REMOTE]
RETRIEVING     downloading declared outputs                      [jobflow-remote DOWNLOADED, aiida RETRIEVING]
COMPLETED      exit code 0 + required outputs present  (final)   [psij COMPLETED semantics]
FAILED         nonzero exit / scheduler failure; carries a typed reason
               {TIMEOUT, OOM, NODE_FAIL, PREEMPTED, CANCELLED_EXT, EXIT_NONZERO, MISSING_OUTPUT}   (final for this attempt)
                                                                 [aiida scheduler exit codes; snakemake fail_stati]
CANCELLING     scancel issued, awaiting confirmation             [aiida kill_calculation re-query]
CANCELLED      (final)                                           [psij CANCELED]
REMOTE_ERROR   infra failure (ssh/transfer/slurmctld); retried with backoff; never
               consumes job attempts; never fails the run        [jobflow-remote REMOTE_ERROR, catgo transient, aiida pause]
LOST           no job id and no evidence after grace, or job vanished from squeue+sacct
               with no exit-code file after N misses → needs a decision  [flower; inputs from snakemake/Eleforge]
```
Two fields are kept separately: `state` (above) and `sched` (last raw scheduler state, source, timestamp), following
aiida's two axes. Transitions are monotonic per attempt, with a psij-style `order` to drop stale observations. The
only way back is a **new attempt**.

### 8.2 On-disk records
Local (authoritative):
```
runs/<run_id>/
  plan.yaml                      approved contract (separate topic)
  events.jsonl                   append-only; fsync after each append
  lock                           fcntl.flock target; JSON body {pid, host, user, started_at} for diagnostics
  cache/clusters/<name>.json     last_polled_at, MinJobAge, sacct_available, ssh health   [aiida min poll interval; snakemake MinJobAge probe]
  nodes/<node_id>/a<N>/          one dir per attempt
      job.sh                     rendered script (hash recorded in event)
      inputs/ (or manifest)      what was staged
      outputs/                   retrieved files
      scheduler/                 slurm-<id>.out/.err copies, sacct detail dump   [aiida get_detailed_job_info]
```
Remote (evidence, never authoritative):
```
<remote_root>/<project>/<run_id>/<node_id>/a<N>/            human-readable path   [catgo "never hashes"]
  .flower/submit.json   {run, node, attempt, submit_key, fingerprint, created_at}  written BEFORE sbatch  [Eleforge marker]
  .flower/jobid         written atomically by the same remote command as sbatch     [dpdispatcher <hash>_job_id]
  .flower/owner/        mkdir-lock taken by the job itself; contains SLURM_JOB_ID   [flower; dup-guard]
  .flower/started       touched at job start (timestamp, host)
  .flower/ec            exit code of the payload, written by trap                 [psij <native_id>.ec]
  .flower/done          touched only if ec==0 and declared outputs exist          [dpdispatcher job_tag_finished]
  .flower/cancelled     touched by `flower cancel`                             [Eleforge canceled marker]
  .flower/timeout_imminent  written by USR1 trap                                  [Slurm --signal convention]
```
Event types (all carry `run, node, attempt, ts, actor`):
`job.prepared{script_sha, resources}`, `job.staged{manifest_sha}`,
`job.submit_intent{submit_key, fingerprint, remote_dir}`,
`job.submitted{slurm_id, via: sbatch|reattach|jobid_file}`,
`job.observed{state, sched_raw, source: squeue|sacct|evidence}` (emitted only on change),
`job.exited{ec, sacct_state, elapsed}`, `job.retrieved{files}`, `job.completed`, `job.failed{reason, detail}`,
`job.remote_error{op, error, attempt, retry_at}`, `job.cancel_requested{by}`, `job.cancelled`,
`job.lost{why, evidence}`, `job.orphan_detected{slurm_id, submit_key}`.
A replay folds these into state with no remote calls. `status` therefore works offline.

### 8.3 Idempotent submit protocol
```
submit_key  = "flower-{run8}-{node}-a{N}"   (≤ 64 chars, [A-Za-z0-9_.-])   [Eleforge submit_key; snakemake --name run_uuid; dpdispatcher name sanitizer]
fingerprint = sha256(job.sh + input manifest + attempt)[:16]
```
1. Take the run lock (non-blocking; if it's held, mutating commands exit with "runner on <host> pid <pid>").
   [jobflow-remote running_runner check]
2. If the attempt already has `job.submitted`, return (no-op). [aiida "already WITHSCHEDULER"]
3. Append `job.submit_intent` and fsync. **Nothing remote happens before this record exists.**
   [aiida SUBMITTING, Eleforge marker]
4. Stage inputs with rsync to `a<N>/`, then write `.flower/submit.json`. Failures here become `REMOTE_ERROR`
   (op=stage) with backoff.
5. Do a dedupe lookup in the same SSH call as the submit:
   ```
   cd <dir> && if [ -s .flower/jobid ]; then echo EXISTING $(cat .flower/jobid);
   else J=$(squeue --me -h -o '%i|%j' | awk -F'|' -v k=<submit_key> '$2==k{print $1}' | head -1);
        [ -z "$J" ] && J=$(sacct -X -n -P --name=<submit_key> -S now-7days -o JobIDRaw | head -1);
        if [ -n "$J" ]; then echo "$J" > .flower/jobid; echo EXISTING $J;
        else sbatch --parsable --job-name=<submit_key> --comment=<fingerprint> --chdir=<dir> \
               -o slurm-%j.out -e slurm-%j.err job.sh > .flower/jobid.tmp && mv .flower/jobid.tmp .flower/jobid \
             && echo SUBMITTED $(cut -d';' -f1 .flower/jobid); fi; fi
   ```
   [Eleforge `_find_existing_slurm_job`; dpdispatcher remote job-id file; `--parsable` split on `;` per
   dpdispatcher/snakemake]
6. Parse and validate the id (digits, plus an optional `_array`). Append `job.submitted{via}`.
   [snakemake `validate_or_get_slurm_job_id`]
7. **Duplicate guard inside the job** (belt and braces). The first lines of `job.sh` run
   `mkdir .flower/owner 2>/dev/null || [ "$(cat .flower/owner/id)" = "$SLURM_JOB_ID" ] || exit 97` and then
   `echo $SLURM_JOB_ID > .flower/owner/id`. A second copy of the same attempt aborts in seconds, while a Slurm
   *requeue* (same id) proceeds. [flower; generalizes dpdispatcher's finish-tag skip]

**Recovery for an attempt stuck in SUBMITTING** (run on every tick):
- If remote `.flower/jobid` exists, record `job.submitted(via=jobid_file)`.
- Otherwise, if `squeue`/`sacct --name=<submit_key>` finds exactly one id, record `job.submitted(via=reattach)`.
  If it finds more than one, keep the earliest, cancel the rest and emit `job.orphan_detected` events.
  [Eleforge ambiguity handling]
- Otherwise, with no remote marker *and* an intent older than the grace period (default 10 min ≥ 2×MinJobAge),
  re-run steps 4–6 under the **same** submit_key, which is safe because nothing exists.
- Otherwise (a marker exists but there is no job and no jobid), wait until the grace period expires, then mark
  `LOST(why=submit_unconfirmed)`, which needs a human or agent `retry`. It is never auto-reset to READY.
  [counter-example: catgo]

### 8.4 Reconcile loop (`tick`), invoked by CLI commands
```
tick(run):
  with run_lock(nonblocking):
    state = fold(events.jsonl)
    for cluster, jobs in group_active_jobs(state):                         [aiida JobsList batching]
        if now - cache.last_polled_at < cluster.min_poll_interval: continue [aiida minimum_job_poll_interval]
        out = ssh_one_shot(cluster, probe_script(jobs))                    [one round-trip/cluster]
            # probe_script:
            #  squeue --me -h -t all -o '%i|%T|%r' -j <ids>     (dup single id: aiida trick)
            #  sacct -X -n -P -j <ids not in squeue> -o JobIDRaw,State,ExitCode,Elapsed,End   [snakemake/catgo/Eleforge]
            #  for each id not in either: cat <dir>/.flower/{ec,done,cancelled,started} 2>/dev/null   [Eleforge workdir fallback, psij .ec]
        on ssh/timeout/slurmctld error: job.remote_error(op=poll) w/ backoff (30,300,1200)  [jobflow-remote delta_retry]
                                         — never change job verdicts on poll errors       [contrast psij fail-all]
        for job in jobs: apply(job, observation)  → emit job.observed / job.exited only on change
    advance: SUBMITTING recovery (8.3); EXITED → RETRIEVING (rsync outputs/) → verdict;  [jobflow-remote step chain]
             READY job nodes → submit (respecting per-cluster max_jobs)                [jobflow-remote limited workers]
    update cache/clusters/<name>.json
```
Verdict rules (on EXITED):
- `sacct State=COMPLETED`, `ec == 0` and the declared outputs exist give COMPLETED. [psij ec, dpdispatcher finish
  tag, catgo "COMPLETED ≠ converged"]
- A sacct failure state gives FAILED with the reason mapped from the state.
  [aiida parse_output; snakemake fail_stati]
- If sacct is unavailable, decide from `ec`/`done`/`cancelled` alone. [Eleforge no-accounting path]
- Scientific convergence checks are out of scope for the job node. They belong to a downstream `function` or
  `agent` node. [catgo campaign: "the real scientific error is found only by reading the work_dir outputs at the
  agent-driven collect step"]

Lost-job detection:
- A job missing from squeue, sacct and evidence files stays in its current state on the first misses. sacct has
  write lag, and the snakemake rule "didn't make it into slurmdbd yet" applies within `sacct_lag_grace` (default
  10 min after submit).
- After `lost_after_misses` (default 5) consecutive polls spanning more than `max(MinJobAge, 15 min)`, mark
  `LOST(why=vanished)`. [snakemake `missing_sacct_status` + status_attempts; Eleforge miss counter]
- A job that was RUNNING and is now gone with `started` present but no `ec` means the node died or was hard-killed,
  so mark it `FAILED(reason=NODE_FAIL?)` and check sacct detail. [snakemake NODE_FAIL handling]
- **Orphan sweep** (in `tick --sweep`, run every N ticks): `squeue --me -h -o '%i|%j' | grep '|flower-<run8>-'`. Any id
  not bound to an attempt raises `job.orphan_detected` and suggests `flower cancel --orphans`.
  [Eleforge name search; snakemake `--name run_uuid`]

CLI surface:
- `flower status [--refresh]`: a pure fold by default (fast, offline). `--refresh` runs one `tick` if the lock is
  free, otherwise it prints the cached state plus the lock holder.
- `flower tick [--all-runs] [--sweep]`: one reconcile pass, then exit. This is what cron, systemd timers, Slurm
  `scrontab` or the outer agent harness call. [catgo scanner `scan_cycle`, dpdispatcher `exit_on_submit`
  re-entrancy]
- `flower watch [--interval auto]`: loops `tick` in the foreground with snakemake's adaptive interval (40 s,
  +10 s per idle cycle, cap 180 s, reset on any transition), streaming events. Ctrl-C is harmless because all state
  is in the log. [snakemake check_active_jobs]
- Optional `flower daemon install` writes a systemd `--user` timer or crontab line running `tick --all-runs`
  every 2–5 min. It is never required. [jobflow-remote supervisord as the heavier alternative, deliberately not
  copied]
- Lock semantics: `fcntl.flock` on `runs/<id>/lock`. A stale holder (pid dead on the same host) is reported and
  broken automatically. A different host is reported with its host/user and not broken unless `--break-lock` is
  given. [jobflow-remote `_check_running_runner`, `break_lock`]

### 8.5 Cancel, retry, rerun, walltime
- `flower cancel <node>` appends `job.cancel_requested`, then runs `scancel <id>` and
  `touch .flower/cancelled` in one SSH call, and moves to CANCELLING. The next tick confirms with CANCELLED via
  sacct or marker. If scancel errors and the job is already gone, it reconciles to the true terminal state.
  [aiida kill_calculation; Eleforge marker] Downstream nodes become BLOCKED (node-level, not the job runtime).
- `flower retry <node>` re-executes the stuck *infra step* of the current attempt (REMOTE_ERROR or LOST with
  submit_unconfirmed). `flower rerun <node> [--downstream]` creates attempt N+1 (new dir, new submit_key) and
  invalidates downstream. [jobflow-remote retry vs rerun]
- Automatic job retries come from the node's `retry:` policy keyed on the typed FAILED reason:
  `NODE_FAIL|BOOT_FAIL|PREEMPTED → resubmit, add --exclude=<failed nodes>` [snakemake],
  `TIMEOUT → resubmit with restart:` mapping if the node declares one (e.g. `CONTCAR→POSCAR`, `--signal=B:USR1@300`
  trap writes a checkpoint) [aiida BaseRestartWorkChain handlers; Slurm convention], `OOM/EXIT_NONZERO →` no blind
  retry; emit a proposed amendment for the planning agent or human to approve [catgo tier 3 "escalate"; flower
  amendment flow]. Infra retries (REMOTE_ERROR) are counted separately and never consume this budget.
  [jobflow-remote step_attempts; catgo transient]
- Each auto-retry is an event with its rationale (`job.retry_scheduled{reason, policy_rule}`), so provenance shows
  why attempt N+1 exists.

### 8.6 Transport defaults
- `ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=20 -o ServerAliveCountMax=4 <alias> 'bash -l -c …'`,
  with a hard subprocess timeout of 120 s for commands and none for rsync (`--timeout=60` handles stalls).
  [Eleforge openssh.py; catgo campaign]
- Hosts come from `~/.ssh/config` aliases, never stored credentials. If an alias has a ControlMaster, check it with
  `ssh -O check` (5 s timeout) and fall back to a fresh connection on failure. Automation never starts a master
  itself; `flower cluster login <alias>` opens one interactively for MFA sites.
  [Eleforge lesson; aiida OpenSSH backend]
- The transient-error classifier matches "Connection timed out/refused/reset", "Socket timed out on send/recv",
  "Unable to contact slurm controller", "Invalid user for SlurmUser", ssh exit 255 and subprocess timeouts. These
  become REMOTE_ERROR with backoff; everything else is a permanent step error. [dpdispatcher RetrySignal strings]
- Staging uses `rsync -a --partial` and falls back to `tar czf - | ssh … tar xzf -`. [dpdispatcher]
