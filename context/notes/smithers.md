# Smithers deep-read (source level) — design input for forgeflow

Status: COMPLETE. §1 (data model) was read from source; §§2–7 below are superseded by Appendices A–D at the end of this file, which are source-level deep dives. §8 recommendation stands: borrow designs + small data-shape ports, do not build on Smithers.

Clones: `context/repos/smithers-0.x` (v0.35.0, commit 584f479) and `context/repos/smithers-main` (1.0.0-rc, commit 96aed3b0). All paths below are relative to `smithers-0.x/` unless marked otherwise.

---

## 1. Data model and persistence (0.x)

### Storage engine
- Persistence uses SQLite through `bun:sqlite` and Drizzle, with PGlite/Postgres as alternatives. `packages/db/src/dialect.js` is the only SQLite↔PG seam (`packages/db/src/README.md`).
- Pragmas are set in `packages/db/src/openDurableSqliteDatabase.js:19-23`: `busy_timeout=30000`, `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`. Writes are retried on `SQLITE_BUSY|PROTOCOL|IOERR` (`isRetryableSqliteWriteError.js`, `withSqliteWriteRetryEffect.js`).
  - **forgeflow caveat:** WAL on NFS/Lustre home directories is unsafe. HPC is exactly where forgeflow runs, which supports the file-first JSONL choice.
- Default store files:
  - the run DB is `smithers.db` in the project, which `init` adds to `.gitignore` (`apps/cli/src/initCeremony.js:110`)
  - the event log is `.smithers/executions/<runId>/logs/stream.ndjson` (`docs/llms-events.txt`).
- There are 38 internal `_smithers_*` tables (`packages/db/src/internal-schema/`). Migrations live in `packages/db/migrations/*.sql`, and a versioned ledger is kept in `schema-migrations.js`.

### Core tables (Drizzle definitions in `packages/db/src/internal-schema/`)

| Table | PK | Key columns |
|---|---|---|
| `_smithers_runs` (`smithersRuns.js`) | run_id | parent_run_id, workflow_name/path/**hash**, status, created/started/finished_at_ms, **heartbeat_at_ms, runtime_owner_id**, cancel_requested_at_ms (+ request id/source/signal/client pid), pause_requested_at_ms, hijack_requested_at_ms, hijack_target, **vcs_type/root/revision**, error_json, config_json |
| `_smithers_nodes` (`smithersNodes.js`) | (run_id, node_id, **iteration**) | state, last_attempt, updated_at_ms, output_table, label |
| `_smithers_attempts` (`smithersAttempts.js`) | (run_id, node_id, iteration, **attempt**) | state, started/finished_at_ms, heartbeat_at_ms, heartbeat_data_json, error_json, **jj_pointer** (VCS snapshot), cached, **meta_json** (agent conversation, session id, steer, wait snapshots), **response_text**, jj_cwd, effort |
| `_smithers_events` (`smithersEvents.js`) | (run_id, **seq**) | timestamp_ms, type, payload_json |
| `_smithers_frames` (`smithersFrames.js`) | (run_id, frame_no) | xml_json (rendered plan tree), xml_hash, encoding (`full` or delta), mounted_task_ids_json, task_index_json, note |
| `_smithers_approvals` | (run_id, node_id, iteration) | status, requested/decided_at_ms, note, decided_by, request_json, decision_json, auto_approved |
| `_smithers_signals` | (run_id, seq) | signal_name, correlation_id, payload_json, received_at_ms, received_by |
| `_smithers_steers` | steer_id | run_id, node_id, message, status `queued\|consumed\|expired`, consumed_by_attempt/iteration |
| `_smithers_human_requests` | request_id | kind, status, prompt, schema_json, options_json, response_json, timeout_at_ms |
| `_smithers_cache` | cache_key | workflow_name, node_id, output_table, schema_sig, agent_sig, tools_sig, jj_pointer, payload_json |
| `_smithers_output_provenance` | (run_id, output_table, node_id, iteration) | **seq** UNIQUE per run, which gives the durable completion order (`migrations/0030_output_provenance.sql`) |
| `_smithers_tool_calls` | (run_id, node_id, iteration, attempt, seq) | tool_name, input/output_json, status, **side_effect, idempotent, idempotency_key, has_revert, revert_status** (`reverting\|reverted\|revert-failed\|revert-stale`). This is the side-effect journal (`migrations/0031_side_effect_journal.sql`), plus `_smithers_tool_call_archive` keyed by `archived_by_op` |
| `_smithers_agent_checkpoints` (+ `_contents`) | (run,node,iter,attempt,sequence) | content_hash (content-addressed blob), codec, version, agent_id, purpose |
| `_smithers_agent_processes` (`migrations/0036`) | pid | run_id, node_id, **engine_pid**, started_at_ms, used for orphan reaping |
| `_smithers_time_travel_audit` | autoinc | from/to_frame_no, caller, result, duration_ms |
| others | | sandboxes (heartbeat), node_diffs (per-node VCS diff cache), cron, docs, memory_*, scorers, workspace_checkpoints/states, ralph (loops), alerts, integration_* |

`_smithers_steers.js` has a useful comment: the steers table is only the *pre-consumption inbox*. The consumed text is copied into the attempt's persisted `agentConversation`, so "replay reproduces the injected turn from the attempt metadata, not from this (mutable) table." forgeflow should follow the same rule: anything an agent saw must be in the immutable attempt record.

### User output tables
- Each task's Zod output schema becomes a real SQL table, `CREATE TABLE IF NOT EXISTS` with fixed prefix columns `run_id, node_id, iteration` plus one column per field (`packages/db/src/zodToCreateTableSQL.js`).
  - Field types map to INTEGER, REAL or TEXT; JSON values are stored as TEXT.
  - `assertNoReservedColumns.js` guards the prefix columns.
- The input table has the single key `run_id`.
- Outputs are therefore keyed by `(run_id, node_id, iteration)`, with no content hash. Content hashing is used only by the opt-in step cache.

### What counts as a "completed step"
A single SQLite transaction does all of the following (`packages/engine/src/engine.js:8466-8539`, `"task-completion"`):
1. `claimAttemptCompletion(runId,node,iter,attempt, executionOwnerId, t)`. This is a CAS on the attempt owner. If the claim fails (someone else finished, cancelled or timed out the attempt), nothing is written.
2. `upsertOutputRow(outputTable, {runId,nodeId,iteration}, payload)`.
3. `insertCache(...)`, if step cache is enabled and the result was not itself a cache hit.
4. `updateAttempt(... state:"finished", jjPointer, cached, metaJson, responseText)`.
5. `recordToolCallEffect(phase:"finished", kind:"task", idempotent, hasRevert)`, which journals the task as a side effect.
6. `insertNode(state:"finished", lastAttempt, outputTable)`.

Only after commit is `NodeFinished` emitted with persist. The invariant is stated at `engine.js:9846-9856`: "an output row and its node's 'finished' state commit in one transaction … so a persisted snapshot could never record a finished node whose in-schema output row is absent". Before committing, a late success is refused if the watchdog or abort has already fired (`engine.js:8459-8463`: "Never let a late successful value cross that durable terminal boundary").

→ **forgeflow equivalent:** for each node, append one `node.finished` JSONL record that embeds or points to the output artifact digest, after an fsync of the artifact. The owner/attempt token goes in the record, and replay ignores a `finished` record whose attempt was already terminal.

### Step cache key (opt-in, `engine.js:5747-5818`)
`sha256(JSON.stringify(cacheBase))`, where `cacheBase` is one of:
- default: `{workflowName, nodeId, iteration, outputTableName, schemaSig, outputSchemaSig, agentSig(agent.id), toolsSig(hash of capability registry), checkpointSig, jjPointer, prompt, payload}`
- with `cachePolicy {by, version, key, ttlMs, scope: run|workflow|global}` (`packages/scheduler/src/CachePolicy.ts`): `{cacheScope, scope identity, sigs…, jjPointer, cacheVersion, cacheKey, cacheBy: by(ctx)}`.

`schemaSignature` is the sha256 of the sorted `col:type:notNull:pk` list (`packages/db/src/schema-signature.js`). The VCS pointer (jj) is part of the key, so workspace changes invalidate the cache.

### State enums
- **Run status** (persisted): `running | waiting-approval | waiting-event | waiting-timer | waiting-quota | paused | finished | continued | failed | cancelled` (`packages/db/src/adapter/DB_RUN_ALLOWED_STATUSES.js`, `packages/driver/src/RunStatus.ts`).
- **Run state** (derived view, `packages/db/src/runState/deriveRunState.js`): the statuses above plus `recovering | stale | orphaned | succeeded | succeeded-with-failures | unknown`.
  - A `running` run with a heartbeat older than 30 s (`RUN_STATE_HEARTBEAT_STALE_MS`) is `stale`.
  - It is `orphaned` only if the owner is provably dead. Owner ids look like `pid:<pid>@<host>:<session>` (`packages/db/src/runtime-owner.js`), and only same-host PIDs are probed. An unknown owner shape stays `stale` "because its death cannot be proven" (`runState/README.md`).
  - A `waiting-timer` run is flagged `timer-overdue` only after a 30 s grace period (`RUN_STATE_TIMER_OVERDUE_GRACE_MS.js`).
  - **For forgeflow:** a host-scoped owner id matters on HPC, where login nodes rotate.
- **Task/node state** (`packages/scheduler/src/TaskState.ts`): `pending | waiting-approval | waiting-event | waiting-timer | waiting-quota | waiting-bound | bound-stale | in-progress | finished | failed | stalled | cancelled | skipped`. Attempt rows use the same vocabulary. Non-terminal attempt states are `in-progress` and the `waiting-*` states (`packages/engine/src/cancel-subtree.js:389`).
- **Frames:** each re-render of the JSX tree commits a frame, stored as a keyframe every 50 frames (`FRAME_KEYFRAME_INTERVAL`) plus JSON-path deltas with ops `set|insert|remove` (`packages/db/src/frame-codec/encodeFrameDelta.js:136-170`). `FrameCommitted{frameNo, xmlHash, trigger{reason,nodeId,iteration}}` gives time travel its addressable points.

### Event log / NDJSON
- `EventBus` (`packages/engine/src/events.js`) has three emit flavours:
  - `emitEvent` writes a DB row
  - `emitEventWithPersist` writes a DB row and appends to NDJSON
  - `emitEventQueued` serializes persistence behind `persistTail`.
- `seq` is monotonic per run (`insertEventWithNextSeq`). Each NDJSON line is `JSON.stringify(event)` with `correlation{runId,nodeId,iteration,attempt}` attached (`events.js:218-245`), written with plain `appendFile` and no fsync.
- **Important: in 0.x the event log is NOT the source of truth.** State lives in the mutable `_nodes/_attempts/_outputs` tables, and events/NDJSON are observability. forgeflow inverts this (log = truth), so it cannot copy Smithers' recovery logic wholesale.
- Common fields (`docs/llms-events.txt`): `{type, runId, timestampMs}`. Node-scoped events add `nodeId, iteration`; attempt-scoped events add `attempt`. The full union is in `apps/observability/src/SmithersEvent.ts` (843 lines). Categories:
  - run: `RunStarted, RunStatusChanged{status}, RunAutoResumed{lastHeartbeatAtMs, staleDurationMs}, RunAutoResumeSkipped{reason: pid-alive|missing-workflow|rate-limited}, RunFinished{failedChildren?}, RunFailed, RunCancelled, RunContinuedAsNew, RunHijackRequested, RunHijacked{engine, mode: native-cli|conversation, resume, cwd}, RetryTaskStarted{resetDependents, resetNodes[]}, RetryTaskFinished, RunForked{parentRunId, parentFrameNo, branchLabel}, ReplayStarted{parentRunId, parentFrameNo, restoreVcs}`
  - frame: `FrameCommitted`
  - node: `NodePending, NodeStarted{attempt, childRunId?}, TaskHeartbeat{hasData, dataSizeBytes}, TaskHeartbeatTimeout, NodeFinished, NodeFailed{error}, NodeStalled, NodeCancelled, NodeSkipped, NodeRetrying, NodeWaitingApproval, NodeWaitingTimer{firesAtMs}`
  - approval: `ApprovalRequested/Granted/AutoApproved/Denied`
  - timer: `TimerCreated{timerId, firesAtMs, timerType: duration|absolute}, TimerFired{firedAtMs}, TimerCancelled`
  - output: `NodeOutput{text, stream: stdout|stderr}`
  - agent: `AgentEvent{engine, event: AgentCliEvent}, AgentTraceEvent, AgentTraceSummary, AgentSessionEvent`
  - token: `TokenUsageReported{model, agent, inputTokens, freshInputTokens, cacheReadTokens, cacheWriteTokens, outputTokens, reasoningTokens, costUsd?}`. This is the **final cumulative total per attempt**. Old versions wrote it repeatedly, so readers dedupe by (node, iter, attempt) and keep the last record. The `_smithers_run_usage` table upserts by attempt.
  - workflow (hot reload): `WorkflowReloadDetected/Reloaded{generation, changedFiles}/ReloadFailed/ReloadUnsafe`
  - revert / time-travel: `EffectRevert*, SideEffectBoundaryCrossed{report{blocking, revertible, warnings}}`
  - others: tool-call, sandbox, scorer, memory, supervisor (`SupervisorStarted, SupervisorPollCompleted`)
- **Harness-neutral agent event** (`packages/agents/src/BaseCliAgent/AgentCliEvent.ts`). This is worth copying verbatim as forgeflow's normalized harness stream:
  - `{type:"started", engine, title, resume?}`
  - `{type:"action", engine, phase: started|updated|completed, entryType?: thought|message, action{id, kind: turn|command|tool|file_change|web_search|todo_list|reasoning|warning|note, title, detail}, message?, ok?, level?}`
  - `{type:"completed", engine, ok, answer?, error?, resume?(session id), usage?}`

---

## 2. Agent node execution (0.x) — PENDING
Established so far:
- CLI agents extend `BaseCliAgent` (`packages/agents/src/BaseCliAgent/`), which spawns the vendor binary via `node:child_process`. Adapters are `ClaudeCodeAgent.js`, `CodexAgent.js`, `PiAgent.js` (Pi RPC mode), and others.
- For agents without native structured output, JSON instructions are injected and the output is extracted and validated against Zod, with a retry on failure (`packages/engine/src/json-extraction.js`, `output-validation-diagnostics.js`).
- Engine helpers in `engine.js` include:
  - `raceWithTimeout` at :483
  - abort/heartbeat-timeout classification at :850-893
  - `resumeEligibleAttempts`/`shouldDiscardResumeSession` at :556-609
  - `resumeSessionFromCheckpoint` at :652
  - `estimateReportedCostUsd` at :3964
  - quota failover across agent chains at :4134-4385
  - a "Kimi broken session" special case at :4247
  - `resolveAgentWorkerExitGraceMs` at :1801.
- **To extract:** exact argv/env per harness, stream parsing, kill-tree semantics, and session resume.

## 3. Durability / scheduling — PENDING
Established so far:
- `up` exits with code 3 on `waiting-*` instead of holding a process.
- Timers persist only `firesAtMs`, and `supervise` or the Gateway wakes them.
- Resume checks the workflow hash and VCS revision (`assertResumeDurabilityMetadata`, `engine.js:2992`) and fails with `RESUME_METADATA_MISMATCH` unless `--accept-workflow-change` is passed.
- Supervisor heartbeat and run-takeover code live at `engine.js:3271` (`startRunSupervisor`) and :3366 (`isRunHeartbeatFresh`), plus `run-parking.js`, `runDriverLiveness.js` and `run-failure-recovery.js`.
- `runsDueForQuotaResume` (:3096) gives quota waits durable resume.

## 4. Control verbs — PENDING
Established so far:
- `retry-task --run-id --node-id` resets the node and, by default, its dependents; `--no-deps` resets only the node. Event: `RetryTaskStarted{resetDependents, resetNodes}`.
- steer: a durable inbox that is consumed into the next `generate()` (§1).
- hijack: sets `runs.hijack_requested_at_ms/hijack_target`, and `RunHijacked{mode: native-cli|conversation, resume}`.
- rewind, fork and replay operate at a frame; a plain fork does not re-execute. `_smithers_tool_call_archive` and `revert_status` support compensation when rewinding past side effects.

## 5. Dynamic graph — PENDING
Established so far:
- The plan is re-rendered from JSX plus persisted outputs on every state change, and each render is a frame.
- Node identity is `(nodeId, iteration)` (`buildStateKey` gives `nodeId::iteration`).
- `up --hot` emits `WorkflowReload*` events, and `WorkflowReloadUnsafe` exists for changes that cannot be applied in flight.

## 6. 1.0 Control.plan / plan-store / journal / park-on-job — PENDING
See the survey §1 for PlanCard digest binding, the append-only plan-store (SQL trigger refuses rewrites), the journal, and `smithers-main/docs/design/durable-external-work.md` (Start/Poll/Collect "park on job"; a suspended run holds no lease). That design doc still needs a full summary.

## 7. Outer-agent surface — PENDING
Established so far:
- 0.x CLI catalog: `up, ps, inspect, logs, events, chat, why, cancel, approve/deny, signal, steer, hijack, retry-task, timeline, diff, rewind, fork, replay, timetravel, supervise, graph, create-workflow, init, mcp add`.
- MCP tools: `run_workflow, list_runs, get_run, watch_run, resolve_approval, revert_attempt, rewind_run, time_travel, …`.
- Skill/plugin directories are `skills/`, `claude-plugin/` (bin, hooks, lib, monitors, skills, workflows), `codex-plugin/` and `packages/pi-plugin`. Their contents are not yet summarized.

## 8. License, quality, recommendation (provisional)
- **License:** MIT (root `LICENSE`, and `package.json` `"license":"MIT"`, author William Cory). Porting with attribution is legally fine.
- **Code shape:**
  - 0.x is about 4.5k JS/TS source files in roughly 50 packages. JS carries `.d.ts`/`.ts` type sidecars.
  - The core engine is a single 12,079-line `packages/engine/src/engine.js`.
  - The stack is Effect-based, with Bun, React reconciler, Drizzle and jj.
  - The code is heavily defensive, and comments name concrete races (completion CAS, late-success refusal, heartbeat drain).
  - 1.0 is a ground-up rewrite on Effect 4 rc / Node 26 / Rust FFI, with no state compatibility with 0.x.
- **Recommendation: (c) borrow designs, with a few small (b) ports of data shapes (no code)**. Do not build on Smithers.
  - It is TS/Bun, which is foreign to Python science users and to an HPC login-node environment.
  - It has a single maintainer and is pivoting to a hosted product.
  - 0.x is end-of-line, and 1.0 is unreleased and incompatible.
  - It uses SQLite WAL, which is unsafe on shared HPC filesystems.
  - Its truth is in mutable tables, whereas forgeflow wants log-as-truth.
- **What to borrow:**
  - the state-enum vocabulary
  - (node, iteration, attempt) keying
  - the atomic "claim attempt then commit output + finished" rule
  - host-scoped owner ids with a stale/orphaned distinction
  - `AgentCliEvent` normalization
  - the per-attempt cumulative `TokenUsageReported`
  - the steer inbox → immutable attempt record rule
  - the side-effect journal flags (idempotent / idempotency_key / revert)
  - exit code 3 for "parked"
  - 1.0's digest-bound plan approval and append-only plan growth.


---

> The sections below were produced by four source-level deep-dive passes after the partial digest above; they supersede its placeholder sections 2-7.

## Appendix A — 0.x agent (harness) execution, source-level

### Smithers v0.35.0: how it runs headless CLI agents (Claude Code, Codex, Pi)

Repo root: `/homes/nessa/zhanghao/dev/Eleforge/forgeflow/context/repos/smithers-0.x/packages/`. Paths below are relative to `packages/`. I modified no files.

---

### 0. Architecture in one paragraph
- Each adapter subclasses `BaseCliAgent` and implements two methods:
  - `buildCommand(params) → {command, args, stdin?, env?, outputFile?, outputFormat?, cleanup?, stdoutBannerPatterns?, stdoutErrorPatterns?, benignStderrPatterns?}`
  - `createOutputInterpreter() → {onStdoutLine, onStderrLine, onExit}`, which emits normalized `AgentCliEvent`s: `started`, `action`, `completed{ok, answer, error, resume, usage}`.
- `BaseCliAgent.runGenerateEffect` (`agents/src/BaseCliAgent/BaseCliAgent.js:1011-1457`) runs the following in order:
  1. Spawn the process.
  2. Feed stdout/stderr through the interpreter line by line.
  3. Classify errors.
  4. Pick the answer text.
  5. Extract usage.
  6. Return an AI-SDK-shaped `GenerateTextResult`.
- The engine (`engine/src/engine.js`) owns several jobs:
  - prompt-injecting the output schema
  - JSON extraction
  - Zod validation
  - correction retries
  - session resume
  - failover across agent chains
  - quota parking

### 1. Exact argv and env per harness

### Claude Code (`agents/src/ClaudeCodeAgent.js:526-796`)
- **Base flags**
  - `args = ["--print"]` (l.527).
  - Default output format is stream-json (l.531): `const outputFormat = this.opts.outputFormat ?? "stream-json";`
  - `--verbose` is forced for stream-json (l.532-534, 599): "Recent Claude CLI builds require --verbose when --print is combined with --output-format=stream-json."
- **Yolo** defaults to ON (`this.yolo = opts.yolo ?? true`, `BaseCliAgent.js:999`). When on (l.541-548):
  ```js
  args.push("--allow-dangerously-skip-permissions");
  args.push("--dangerously-skip-permissions");
  if (!this.opts.permissionMode) args.push("--permission-mode", "bypassPermissions");
  ```
- **Option flags passed through:**
  - `--add-dir`, `--agent`, `--agents <json>`, `--allowed-tools`, `--append-system-prompt`, `--betas`
  - `--continue` (also when `options.continueSession`), `--debug`, `--disallowed-tools`, `--fallback-model`
  - `--fork-session`, `--include-partial-messages`, `--input-format`, `--json-schema`, `--max-budget-usd`
  - `--mcp-config`, `--model`, `--no-session-persistence`, `--permission-mode`, `--plugin-dir`, `--setting-sources`, `--strict-mcp-config`, `--tools`
  - (l.535-598)
- **Resume:** `pushFlag(args, "--resume", resumeSession ?? this.opts.resume); pushFlag(args, "--session-id", this.opts.sessionId);` (l.582-584).
- **System prompt:** `--system-prompt <combined>` (l.587-589). This REPLACES Claude Code's default system prompt. The append-only form is used only when `opts.appendSystemPrompt` is set.
- **Effort:** there is no flag for it. Effort goes into `--settings` as `effortLevel` (l.627-633).
- **Single `--settings` flag** (l.652-657): "Claude Code's `--settings <file-or-json>` is single-valued: the CLI arg parser keeps only the LAST occurrence…". Smithers deep-merges every settings source into one value.
  - The merged settings are written to a mode-0600 temp file, never inlined on argv (l.770-775): "A merged settings object routinely contains secrets … never inline it onto argv (world-readable via `ps`)."
  - Cleanup is returned as `cleanup` (l.788).
- **Durability hook** injected through settings (l.646-650):
  ```js
  post.push({ matcher: "Write|Edit|MultiEdit|NotebookEdit|Bash",
    hooks: [{ type: "command", command: "smithers snapshot-hook" }] });
  ```
  Paired with env `SMITHERS_SNAPSHOT_SOCK` (l.780).
- **Prompt** is the LAST positional argv: `if (params.prompt) args.push(params.prompt);` (l.778). It is not sent on stdin.
- **Env**
  - Constructor (l.192-213): "Clear env vars that cause 'Cannot run nested Claude Code instances' errors…". It sets `CLAUDE_CODE_ENTRYPOINT=""` and `CLAUDECODE=""` when those are present in the parent.
  - `ANTHROPIC_API_KEY=""` unless `opts.apiKey` is given. Comment: "cleared so Claude Code uses the subscription instead of API billing"; a warning is logged once (l.200-211).
  - Per-command env (l.779-782): `CLAUDE_CONFIG_DIR` from `opts.configDir` (one per account), and `ANTHROPIC_API_KEY` from `opts.apiKey`.

### Codex (`agents/src/CodexAgent.js:557-669`)
- **Subcommand:** `const args = resumeSession ? ["exec", "resume"] : ["exec"];` (l.559). The resume id is positional near the end (l.641).
- **Config overrides:** `-c key=value` for each normalized config entry (l.588-591). Effort becomes `model_reasoning_effort=<effort>` unless the caller set one explicitly (l.561-587). Comment: "`max` from the shared ladder is NOT a Codex value".
- **Flags:** `--enable`, `--disable`, `--image`, `--model`.
- **Flags dropped on resume** ("`codex exec resume` does not accept --output-schema", l.623): `--oss`, `--local-provider`, `--sandbox`, `--profile`, `--cd`, `--add-dir`, `--output-schema` (l.596-614).
- **Sandbox/approvals** (l.600-608):
  ```js
  if (!resumeSession && this.opts.fullAuto && !this.opts.sandbox) {
    // codex-cli 0.147 removed `--full-auto` entirely ... emit the canonical flag.
    args.push("--sandbox", "workspace-write");
  } else if (yoloEnabled || this.opts.dangerouslyBypassApprovalsAndSandbox) {
    args.push("--dangerously-bypass-approvals-and-sandbox");
  ```
- **`--skip-git-repo-check`** is opt-in (l.610). The fallback-chain factories always set `skipGitRepoCheck: true` (`fallbackAgents.js:61,105`).
- **Output:** `args.push("--json")` always (l.619). `--output-last-message <tmp>/smithers-codex-<uuid>.txt` always (l.638-639); the temp file is deleted in `cleanup` (l.660-667).
- **Prompt on stdin:** `args.push("-")`, `stdin: fullPrompt`, where `fullPrompt = systemPrompt + "\n\n" + prompt` (l.642-651). There is no separate system flag; the system prompt is prefixed into the stdin text.
- **Env:** `CODEX_HOME` from `opts.configDir`, and `OPENAI_API_KEY` from `opts.apiKey` (l.645-647).
- **Banner stripping:** `stdoutBannerPatterns: [/^OpenAI Codex v[^\n]*$/gm]` (l.655-659).

### Pi (`agents/src/PiAgent.js:103-195, 424-515`)
- **Mode selection** (l.103-107): `if (opts.mode==="rpc") "rpc"; if (options?.onEvent) "json"; else opts.mode ?? "text"`. The engine always passes `onEvent`, so under the engine Pi runs as `--mode json`.
- **`--print`** for every non-rpc mode (l.118-124): "Apply --print to every non-RPC mode so json task executions also process one prompt and exit instead of lingering as an interactive session (#284)."
- **Flags:** `--provider`, `--model`, `--api-key`, `--system-prompt` (`this.systemPrompt` only), `--append-system-prompt` (opts.appendSystemPrompt + system messages), `--session <id>`, `--session-dir`, `--thinking`, `--tools a,b`/`--no-tools`, `--extension`, `--skill`, and `@file` args (l.128-190).
- **Sessions:** `--no-session` is added only when no durable session is needed. If `onEvent` or a session id is present, sessions are kept (l.137-142).
- **Prompt** is the last positional argv (l.191-193).
- **RPC mode** (`BaseCliAgent/runRpcCommandEffect.js`) writes JSONL on stdin:
  - `{"type":"get_state"}` then `{"type":"prompt","message":...}` (l.469-475).
  - It answers `extension_ui_request` with `extension_ui_response` (l.297-306).
  - It finishes on `turn_end`, `agent_end` or `prompt_result` (l.355-385).

### Common env (`BaseCliAgent.js:1019-1023`, `taskContextEnv.js`)
- `env = {...process.env (inheritEnv default true), ...opts.env, ...taskContextEnv}`. The adapter's `commandSpec.env` is layered on top (l.1117).
- Variables injected: `SMITHERS_RUN_ID`, `SMITHERS_NODE_ID`, `SMITHERS_ITERATION`, `SMITHERS_ATTEMPT`, and the recursion guard `SMITHERS_INSIDE_RUN=<runId>/<nodeId>`. Comment: "Its presence… is the signal that the process is already executing a node inside a run, so the orchestration skills must not route the node's own prompt back through `smithers oneshot`".
- Logged argv is redacted by `sanitizeCliArgs`. Any flag matching `/(api[-_]?key|token|secret|password)/` gets `[REDACTED]` (`BaseCliAgent/sanitizeCliArgs.js`).

### 2. Prompt assembly
- **`extractPrompt`** (`BaseCliAgent/extractPrompt.js:55-100`):
  - A string `prompt` is used as-is.
  - A `messages[]` array is flattened: system messages become `systemFromMessages`; the others become `"ROLE: text"` joined with `\n\n`.
  - So a conversation replay to a CLI agent is a flattened transcript.
- **Combined system prompt:** `combinedSystem = combineNonEmpty([this.systemPrompt, systemFromMessages])` (`BaseCliAgent.js:1024`).
- **Schema injection by the engine** (`engine/src/engine.js:6906-6939`). It applies when `!agent.supportsNativeStructuredOutput`, which is the case for all CLI agents. It wraps the prompt like this:
  ```
  "IMPORTANT: After completing the task below, you MUST output ONLY a raw JSON object. Do NOT wrap it in markdown..."
  <task prompt>
  "**REQUIRED OUTPUT** — You MUST return ONLY a raw JSON object matching this schema:" <schemaDesc>
  "The first character of your response must be `{` and the last character must be `}`."
  ```
  - `schemaDesc` comes from `db/src/output/describeSchemaShape.js:10`: JSON Schema from Zod, or a `{field: typeDescription}` map as a fallback.
  - In guided-resume mode with steers, the JSON contract is re-appended as the final user turn (l.6941-6948).
- **Codex native `--output-schema` is opt-in only** (`CodexAgent.js:99-107`): "constrains the model to emit only final JSON and makes it refuse tool calls ('tool calls are constrained by a JSON response schema'), which breaks any agentic task… It is therefore OPT-IN".
  - With `nativeStructuredOutput:true`, the Zod schema goes through `zodToOpenAISchema` into a temp file, passed as `--output-schema` (l.625-637).
- **Prompt transport per harness:**

  | Harness | Prompt goes via |
  |---|---|
  | Claude | argv (positional) |
  | Pi | argv (positional) |
  | Codex | stdin, with `-` |
  | Pi rpc | stdin JSONL |

  - stdin is written once and then `end()`ed (`driver/src/child-process.js:510-513`).
  - Smithers does nothing about argv length limits for Claude or Pi. On Linux a single argument is capped at 128 KB (MAX_ARG_STRLEN), so a Python port should prefer stdin or a file for big prompts.

### 3. Structured output capture and validation

### Agent layer: choosing the answer text
`resolveAgentAnswerText`, `BaseCliAgent.js:617-664`, checks sources in priority order:
1. **Output file:** if `outputFile` (Codex `--output-last-message`) parsed as JSON, use it. Comment: "authoritative output channel… immune to the stdout byte cap" (l.1348-1355).
2. **Stream-json:** for stream-json, use the interpreter's `completed.answer`, which for Claude is the terminal `result` event's `result` field.
   - Comment (l.621-628): "`extractTextFromJsonPayload` concatenates every assistant turn for Claude Code NDJSON… duplicates content while splicing in tool-result noise… (#277 … silently corrupted every stream-json step.)"
3. **Truncated or empty:** use the interpreter answer, then the extracted stdout, then the raw text.
4. **Otherwise:** the generic JSONL extractor (`extractTextFromJsonPayload`, l.555-616). It reverse-scans for `turn_end`/`message_end` (assistant), `agent_end`, then `finish`/`done`, and finally joins text chunks.

Then `output = outputFileJson ?? tryParseJson(extractedText)` (l.1365).

### Engine layer: extracting JSON
`engine.js:7746-7845`, a cascade in this order:
1. `result._output` / `result.output`.
2. The whole text as JSON, after stripping a BOM, starting with `{` or `[`.
3. The LAST ```` ```json { ```` fence plus balanced-brace extraction ("Find the LAST code fence — the required output is always at the end").
4. Fences found in `steps[]`, scanned from the end.
5. A balanced object in `steps[]` text.
6. `extractLastBalancedJson(text)`: "search from END so we get the required output JSON, not an earlier JSON object from intermediate tool output".

The brace matcher is string- and escape-aware (`engine/src/json-extraction.js:7-55`).

### Validation
`engine.js:8166-8176`:
- First the Drizzle `validateOutput(table, row)`.
- Then `desc.outputSchema.safeParse(payload)` (Zod).

### Retry when JSON is missing or invalid
These are correction calls, NOT task retries. One shared budget covers both kinds: `maxSchemaRetries`, default 3 (`engine.js:5053-5054`, `5512-5515`).
- **Missing JSON** (l.7846-7960):
  - With a CLI session: resume it (`resolveCorrectionResumeSession`, l.955-978, uses `attemptMeta.agentResume` when the engine matches) with a short prompt: "Your previous response did not include the required JSON output. Reply with ONLY a valid JSON object…"
  - Without a session: a context-free prompt that includes the original task (capped to 6 KB head + 2 KB tail) and the last response (1 KB head + 1 KB tail). Comment: "Without it the model honestly reports the task as missing and emits schema-valid but amnesiac values (#277)."
- **Schema mismatch** (l.8219-8300): the prompt lists the Zod issues (`path: message`) and restates the schema. The call goes, in order of preference, through a checkpoint resume, then `resumeSession` + prompt, then a replay of `messages` (`[user prompt, ...responseMessages, correction]`).
- **Final failure:** `INVALID_OUTPUT` with diagnostics (l.8181-8215). `isRetryableTaskFailure` treats `INVALID_OUTPUT` from an agent task as retryable for the normal task retry (l.3978-3993).

### 4. Streaming and log capture
- **Line framing** (`BaseCliAgent.js:1142-1180`): stdout and stderr each have a string buffer. They are split on `\n`, and the partial tail is kept until the final flush (l.1205-1206).
  - For json/stream-json, the last stdout line is also summarized as a bounded failure preview (l.708-718): `CLI stdout fallback (event=type/subtype, bytes=N, preview=<200 chars>)`.
- **Claude stream-json events recognized** (`ClaudeCodeAgent.js:265-510`):

  | Event | Handling |
  |---|---|
  | `system/init` | captures `session_id`, emits `started{resume}` |
  | `assistant` / `user` content blocks: `text` | `action{note}` |
  | `tool_use` | `action started`, with `toolKindFromName` and file changes from Write/Edit input |
  | `tool_result` | `action completed`, `ok=!is_error`, summarized (500 chars, `<tool_use_error>` extracted, Read outputs collapsed to "Read output (N lines)") |
  | `result` | `completed{ok:!is_error && subtype!=="error", answer:result, resume:session_id, usage}`; `permission_denials[]` become warnings |
  | `rate_limit_event` | see §8 |
  | Non-JSON lines | surfaced only if ≤220 chars and contain error/failed/denied/exception/timeout (`parseHelpers.js:85-102`) |
  | stderr lines | warning actions, truncated to 220 chars |

  `onExit` synthesizes `completed` if no `result` was seen.
  - Write diffs: "File created successfully" means a real empty-old diff; "has been updated" means paths only — "never fabricate an empty-old diff" (l.233-238).
- **Codex `--json` events recognized** (`CodexAgent.js:330-497`):
  - `thread.started` carries `thread_id`, which becomes the resume id.
  - `turn.started`.
  - `item.started` / `item.updated` / `item.completed`, with `item.type` one of: `agent_message` (final answer = last completed text), `reasoning`, `command_execution` (command, status, exit_code), `file_change` (path + kind only), `mcp_tool_call`, `web_search`, `todo_list`, `error`.
  - `turn.completed` (usage), `turn.failed` (`error.message`).
  - A top-level `error` whose message matches `/reconnecting/` is only a warning; any other `error` is a failure.
- **Pi `--mode json` events:**
  - `session{id}`.
  - `message_update`: `assistantMessageEvent.text_delta` is accumulated.
  - `message_end` / `turn_end`: the assistant message plus usage.
  - `agent_end`: the final completed event.
  - `tool_execution_start` / `_update` / `_end`.
  - Values are summarized to 400 chars (`PiAgent.js:255-394`).
- **Outward propagation:** every event goes to `logAgentCliEvent` and to `options.onEvent`. The engine's `handleAgentEvent` (`engine.js:7143-7200`) does the following:
  - persists `AgentEvent` on the event bus
  - records the `resume` id into `attemptMeta.agentResume` and a checkpoint
  - updates the heartbeat
  - tracks blocking tool actions so a tool lease can extend the heartbeat
- **Raw text forwarding:** `onStdout`/`onStderr` pass text through `createAgentStdoutTextEmitter`. It shows assistant text for stream-json and de-duplicates the final `result` echo (`createAgentStdoutTextEmitter.js:53-58`).
- **Buffer limits:**
  - `DEFAULT_MAX_OUTPUT_BYTES = 200_000` per stream (`driver/src/child-process.js:12`, `engine.js:1774`).
  - The agent passes `truncateKeep: "tail"` for stdout (`BaseCliAgent.js:1191-1194`): "CLI harnesses emit their final result event at the END of the stream; if the capture cap trips, the tail is the part that must survive (#277)."
  - stderr always keeps the HEAD ("so failure classification can read the leading error text", `child-process.js:61-63`).
  - Truncation is UTF-8-boundary safe (l.66-89) and emits a "captured stdout truncated" warning event (`BaseCliAgent.js:1208-1225`).
  - The interpreter parses the live stream before the cap applies, so its answer survives truncation.
- **ANSI/OSC:** only OSC title sequences are stripped: `raw.replace(/\x1b\]0;[^\x07]*\x07/g, "")` (`BaseCliAgent.js:669-671`). General ANSI is not stripped.
- **Benign stderr filtered out** (`BaseCliAgent.js:291-311`): codex `state db missing rollout path`, `codex_core::rollout::list`, "failed to record rollout items… channel closed", "Failed to shutdown rollout recorder", "failed to renew cache TTL: Operation not permitted".

### 5. Timeouts, cancellation, orphan cleanup
- **Timeout inputs:** `resolveTimeouts(options.timeout, {totalMs: agent.timeoutMs, idleMs: agent.idleTimeoutMs})` (`resolveTimeouts.js:9-23`). The engine passes only `timeout: {totalMs: desc.timeoutMs}` (`engine.js:7510`); idle comes from agent options.
- **Engine-level liveness:** `heartbeatTimeoutMs`. Stream output refreshes a lease (`engine.js:5386-5393`). A running tool extends the lease to `heartbeatTimeoutMs * 12` (`TASK_TOOL_EXECUTION_LEASE_MULTIPLIER`, l.1812). An expired lease yields "Task X has not heartbeated in Nms" (l.5661-5675).
- **Timers in `spawnCaptureEffect`** (`driver/src/child-process.js:375-400`):
  - The total timer fails with `PROCESS_TIMEOUT`.
  - The idle timer resets on every stdout/stderr chunk and fails with `PROCESS_IDLE_TIMEOUT`.
  - Under Bun the idle timeout is floored to 5 s: "Bun can deliver child stdout/stderr chunks late on macOS" (l.13-17).
  - An `AbortSignal` fails with `PROCESS_ABORTED`.
- **Process groups:** every agent runs under a watchdog wrapper (`BaseCliAgent/parentDeathCommand.js:93-103`): `node|bun parentDeathWatchdog.js <enginePid> <cwd> <cmd> ...args`, spawned with `detached: true`, so it leads a process group (`runCommandEffect.js:44-48`). Comment: "so cleanup can kill the whole group — subagents, MCP servers, tool children… and so an orphan reaper can address the group by pgid after an engine death (#1464 AWF-3, #1332)."
- **Watchdog** (`parentDeathWatchdog.js:117-156`):
  - It refuses to start if the parent is already dead: "Never launch an agent that already has no supervising engine."
  - It polls the parent every 100 ms and SIGKILLs its own group with `process.kill(-process.pid, "SIGKILL")`.
  - It runs in its own package directory, not the agent cwd, because of #1546: the target repo's `bunfig.toml` preload killed the watchdog.
- **Kill escalation differs by path:**

  | Path | Behavior |
  |---|---|
  | `spawnCaptureEffect` timeout/abort (`killChildTree`, `child-process.js:211-275`) | Immediate SIGKILL to `-pgid`. No SIGTERM grace. On Windows, `taskkill /PID /T /F` with a 1 s fallback. |
  | RPC (Pi rpc) (`runRpcCommandEffect.js:248-255`) | SIGTERM to the group, then SIGKILL after 250 ms. |
  | pid-addressed reaping (`killProcessTree`, `child-process.js:153-206`) | SIGTERM the group, else the pid; poll every 50 ms; SIGKILL after a 2 s grace. Refuses to signal its own pid or pgrp. |

  - Late signals are guarded (l.349-351): "A late abort… must not touch the process: for detached children the pid group may already be reused."
- **Liveness signal:** `onProcess({phase:"exited"})` fires on the OS `exit` event, not `close`. Comment: "A grandchild that inherited the stdio pipes (an MCP server…) keeps `close` pending indefinitely… report `exited` from the OS-level `exit` event" (l.327-331, #1582).
- **Engine grace periods:**
  - Engine abort race: after the task signal aborts, it waits up to `AGENT_ABORT_CLEANUP_GRACE_MS = 4_000` for adapter cleanup (`engine.js:1785, 5190-5210`).
  - `DEFAULT_AGENT_WORKER_EXIT_GRACE_MS = 30_000` between a worker's OS exit and failing the attempt (l.1790-1797).
- **EPIPE:** `child.stdin?.on("error", () => {})`. Comment: "A fast-exiting or early-closing child can make writes to stdin emit an async EPIPE/ECONNRESET. Without a listener Node escalates it to an uncaught exception that tears down the whole orchestrator process." (`runRpcCommandEffect.js:144-147`). `spawnCaptureEffect` logs stdin errors (`child-process.js:500-509`).

### 6. Cost and usage
- **Token parsing:** `extractUsageFromOutput(result.stdout)` (`BaseCliAgent.js:853-969`) recognizes these shapes:
  - `message_start.message.usage` (input, cache_read, cache_creation) and `message_delta.usage.output_tokens`.
  - Claude `result`, which is skipped if incremental events were counted, to avoid double-counting (l.887-896). Otherwise it falls to the generic `parsed.usage` branch.
  - Codex `turn.completed.usage` (input/output/cached_input).
  - OpenCode `step_finish.part.tokens`.
  - Generic `usage`.
  - Gemini `stats.models`.
  - Fallback: `usageFromCompletedEvent` (the interpreter's usage) "when truncated stdout lost the per-message usage events" (l.829-848).
- **Caveat (my reading, not a code comment):** the generic branch never adds `cache_creation_input_tokens`, so Claude cache-write tokens are lost when only `result.usage` is counted. Also, plain `claude -p` stream-json puts usage under `assistant.message.usage`, not top-level `message_start`, so in practice the `result` line is what gets counted.
- **Cost is never read from the CLI:** `total_cost_usd` and `cost_usd` from Claude result events do not appear anywhere in the code. The engine estimates cost from a built-in price table with `estimateReportedCostUsd(model, usage)` (`engine.js:3957-3973`): "Unknown models retain complete token accounting but report no cost instead of a misleading $0 estimate."
- **Storage:**
  - Normalization: `normalizeTokenUsage` (input vs. fresh input, cache read/write, reasoning; l.3927-3955).
  - The `TokenUsageReported` event is emitted on success and on failure (l.7579, 7700).
  - Rows are upserted into `_smithers_run_usage(run_id,node_id,iteration,attempt,model,agent,input_tokens,fresh_input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,cost_usd,...)` (`db/src/adapter.js:4930-4960`).
  - Aggregates return NULL cost if any attempt is unpriced (l.4998).
  - OTel metrics: `agentTokensTotal{kind}`, `agentDurationMs`, `agentRetriesTotal{reason}` (`BaseCliAgent.js:454-479`).

### 7. Session resume, checkpoints, fork, hijack
- **Capture:** each interpreter puts the id in `event.resume`:
  - Claude: `system/init.session_id` / `result.session_id`.
  - Codex: `thread.started.thread_id`.
  - Pi: `session.id`.

  The engine's `handleAgentEvent` sets `attemptMeta.agentResume`, writes it to the heartbeat, and enqueues a checkpoint `{codec:"smithers.cli-session", version:1, payload:{engine, resume}}` (`engine.js:7149-7165`, codec at l.645).
- **Resume resolution on a retry or restart** (`engine.js:6490-6550`):
  1. A hijack continuation in native-cli mode.
  2. The heartbeat checkpoint's `agentResume`.
  3. A stored checkpoint ref (`resumeSessionFromCheckpoint` checks codec and engine match, l.652-664).
  4. As a fallback, `continueSession` (Claude `--continue`). Comment: "--continue is cwd-scoped and may attach the most recent session" if the worktree is shared.
- **Resume pointers from failed or cancelled attempts are ignored (#1610).** Comment (l.979-990): "resuming it kills the next attempt instantly with AGENT_SESSION_LOST and burns a retry doing no work… Dropping a resume pointer costs conversation context, never work: the agent's real state lives in its worktree."
- **Session-loss detection** (`classifySessionLoss`, `BaseCliAgent.js:204-284`). On a match the agent raises `AGENT_SESSION_LOST {discardResumeSession:true, freshSessionFailure:!hadResumeSession}`, and the engine drops the id (`shouldDiscardResumeSession`, `engine.js:592-595`). Patterns per CLI:

  | CLI | Pattern |
  |---|---|
  | Claude | `No conversation found with session ID` ("isolated jj worktrees can relocate the cwd its conversation store is keyed by"). Also caught when it arrives as `completed ok:false` with exit 0 (l.1291-1303). |
  | Codex | `no rollout found for thread id <id>`: "when the response stream drops before the rollout is recorded, the thread id was captured but never persisted". |
  | Kimi | `kimi -r <uuid>` |
  | Grok | `Session does not exist` |

  If the session that broke was freshly minted, the message says retrying won't help and the run fails over.
- **Fork** (`engine.js:6556-6605`): a task with `forkSource` either uses the source's checkpoint, if the agent declares a `"fork"` mode capability, or is seeded with the source's final conversation messages (`resolveForkSessionMessages.js:128`). CLI agents declare no checkpointFormats, so for them a fork means a replay of flattened messages. If neither is available: `TASK_FORK_CHECKPOINT_INCOMPATIBLE`, non-retryable.
- **Hijack:** requires `cliEngine`/`hijackEngine`. `HIJACK_RESUME_AFTER_TURN_ENGINES = new Set(["codex","omp"])`. Comment: "Codex reports its thread id on `thread.started`… but flushes the file `--resume` reads only as the turn progresses. Aborting the CLI the moment the id appears hands the user a dangling session (#1502)" (`engine.js:1814-1822`).
- **Checkpoint envelope:** exactly `{codec, version, payload}`, max 16 MiB (`agent-checkpoint.js:4, 101-138`). Capabilities: `agentSupportsCheckpoint(agent, cp, "resume"|"fork")`.

### 8. Pitfalls explicitly handled in code (quoted)
- **Exit-0 limit banners** (`ClaudeCodeAgent.js:227-231`): "Claude/Fable print usage/session-limit banners as ordinary assistant text and still exit 0, so the banner never reaches the CLI error path." Also caught as raw non-JSON lines (l.274-281).
  - Narrow matchers (`isClaudeLimitBanner.js`): `^You've hit your session limit · resets 1am (TZ)`, `^You're out of usage credits. Run /usage-credits…`, `^Claude usage limit reached. Your limit will reset at …`. They are kept narrow so model output that merely *mentions* rate limits is not parked.
- **`rate_limit_event` false positives** (l.304-314): "The CLI emits a rate_limit_event on HEALTHY streams too: with overage disabled at the org… {status:"allowed", overageStatus:"rejected"}… Treating it as fatal killed every claude attempt on overage-disabled orgs. Only status decides."
  - A rejected event produces a synthesized banner with `Retry after N seconds` taken from `resetsAt`.
- **Quota classifier** (`BaseCliAgent.js:28-117`): regex patterns cover "hit your … limit", "usage limit exceeded", "too many requests", `429…try again`, `out_of_credits`, `usage_limit_reached`, and `"rate_limit_event"…"status":"rejected"`.
  - Reset time is parsed from "try again at Jun 18th, 2026 9:54 AM", "resets 1:30am (America/New_York)", or "retry after N seconds".
  - Result: `AGENT_QUOTA_EXCEEDED {failureQuota, quotaResetAtMs}`, meaning "the run will pause until the quota resets".
- **Non-retryable config/auth errors** (l.119-171): "LLM not set", "unknown model", `401…invalid_authentication`, "API Key…expired", "access token…expired". These become `AGENT_CONFIG_INVALID {failureRetryable:false}`.
- **Error-text priority on non-zero exit** (l.1236-1263): structured JSON error (`type:"error"|"turn.failed"`), then the interpreter's distilled error, unless it is the generic "exited with code"), then filtered stderr, then the stdout summary.
  - Comment: "a stream-json stdout tail is usually an init line or token-usage event, not the failure."
- **Codex non-zero exit with only benign stderr is NOT treated as failure:** `if (!(commandSpec.command === "codex" && filteredStderr.length === 0))` (l.1235).
- **Codex flag drift:** `--full-auto` was removed in codex-cli 0.147 (see §1). `--output-schema` and `--sandbox` are rejected on `exec resume`.
- **Exit 0 with error:** `completed.ok===false` is still classified for quota and session loss and then fails (l.1285-1305). Optional per-agent `stdoutErrorPatterns` and `errorOnBannerOnly` exist (l.1306-1336).
- **Settings secrets kept off argv** (Claude, §1). Unreadable settings files make Smithers drop its own keys with a loud warning: "CRASH-RECOVERY DURABILITY SNAPSHOTS ARE DISABLED" (l.750-767).
- **Diagnostics run alongside each call** (`launchDiagnostics`, `BaseCliAgent.js:1181`), and on failure they are attached to `err.details.diagnostics`, but not for `PROCESS_ABORTED`: "Probe results are concurrent observations and can be stale" (l.1419-1423).
  - Claude probe: `claude auth status` (15 s), the `~/.claude` OAuth expiry check, the macOS Keychain, and a rate-limit header probe (`diagnostics/getDiagnosticStrategy.js:70-272`).
  - Codex probe: reads `$CODEX_HOME/auth.json`, honoring `auth_mode` ("a chatgpt login must not be probed as an API key (#1447)", l.317-319).
  - `preflight()` turns failed checks into a non-retryable `AGENT_CONFIG_INVALID` (l.1463-1601).
- **Spawn-error sanitizing:** `spawnargs` is stripped from spawn errors so secrets on argv don't leak into the stored error (`child-process.js:24-36`).

### 9. Fallback chains and retry classification
- **Chain:** `desc.agent` can be an array. The rung for attempt N is `N-1-quotaFailedAttempts` (`engine.js:5929-5958`).
  - Comment: "Counting [quota failures] here anyway would silently demote the task down the chain: a Codex quota wall would push every task onto its Claude fallback and keep it there."
  - Retries advance past the last genuinely failed `chainIndex` (#1480).
  - Rungs that are quota-blocked in the current round, or failed preflight, are skipped. Skips are recorded in `attemptMeta.agentChainSkips` with a reason (l.5960-6030).
- **Quota failover** (`resolveQuotaChainFailover`, l.4271-4380; applied at l.8767-8795): fail over to the next unblocked rung (`quotaFailoverPending`). Only when every rung is blocked does the run park waiting-quota until the EARLIEST reset. The round then clears "instead of ping-ponging between two blocked providers forever".
- **Run-wide circuit breaker** (`disableAgentForRun`, l.4215, 8747-8766): agents are disabled on quota, on auth (`/invalid_authentication|401|api.key.*invalid|expired.*credentials|authentication.*failed/`), or after 2 broken Kimi sessions.
- **Retryability** (`isRetryableTaskFailure`, l.3978-3993):

  | Condition | Retryable? |
  |---|---|
  | `meta.failureRetryable` set | it wins |
  | `AGENT_CONFIG_INVALID` | never ("Retrying is guaranteed to fail again and just multiplies cost") |
  | `INVALID_OUTPUT` from a non-agent task | not retryable |
  | `INVALID_OUTPUT` from an agent task | retryable |
  | everything else | retryable |

  Quota failures don't consume the retry budget (`isQuotaTaskFailure`, l.4001-4018).
- **`fallbackAgents()`** (`agents/src/fallbackAgents.js`):
  - Builds one rung per registered account (`claude-code` uses `configDir` → `CLAUDE_CONFIG_DIR`; `codex` uses `CODEX_HOME` and `skipGitRepoCheck:true`; there are also `anthropic-api`/`openai-api` key rungs), l.46-125.
  - Ordered by cached quota headroom (`orderAccountsByUsage`), ties broken by a seeded shuffle. The order is stable per run (MAX_STABLE_ORDERS 256).
  - Accounts with a known quota block become "no-network rungs" that fail with the reset time and re-check the clock when invoked: "Without that, one rate limit would retire the account for the whole process."
  - The default agent is the last rung. Default providers: `["claude-code","codex"]`.

### Takeaways for a Python port (my synthesis)
1. **Process launch:**
   - Spawn each harness in its own session/pgid (`start_new_session=True`) behind a parent-death watchdog.
   - Kill the group, not the pid. Add a SIGTERM→SIGKILL grace (Smithers only does this on its RPC and reaper paths).
   - Use the OS `exit` event, not pipe EOF, as the liveness signal.
2. **Prompt and output transport:**
   - Prompt via stdin (Codex `-`), or for Claude/Pi via a file or stdin to avoid argv limits.
   - The answer comes from a dedicated channel: Claude's `result.result`, Codex's `--output-last-message` file.
   - Never concatenate stream text.
   - Keep the stdout TAIL when capping output.
3. **Env:** strip `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT`. Decide explicitly whether `ANTHROPIC_API_KEY` is passed or cleared. Export run/node ids plus a recursion guard variable.
4. **Structured output:**
   - Prompt-inject the schema.
   - Extract with the cascade: whole text, then last fence, then last balanced object.
   - Validate with pydantic.
   - Use a correction budget, default 3, separate from task retries, that resumes the CLI session (`--resume` / `exec resume <id>`). Without a session, re-send the original task plus a truncated prior answer.
   - Avoid Codex `--output-schema` for agentic tasks.
5. **Error classification:**
   - quota → park or fail over, with reset-time parsing
   - config/auth → non-retryable
   - session-lost → drop the resume id and retry fresh
   - also check exit-0 banners and `is_error` on result events
6. **Cost:** compute it yourself from tokens, or read Claude's `total_cost_usd`. Smithers ignores the latter.


## Appendix B — 0.x durability, control verbs, dynamic graph

### Smithers v0.35.0: durability, control verbs and the dynamic graph (findings for a Python HPC workflow CLI)

Root: `forgeflow/context/repos/smithers-0.x/`. All paths below are relative to it. I did not modify any files.

**Five findings that matter most for your design:**
1. **Nothing sleeps while it waits.** On any external wait, the engine writes `waiting-*`, clears the owner and heartbeat, and the process exits with code 3. A separate poller (`supervise` or the gateway sweep) reads the deadlines stored in the DB and relaunches `up --resume`.
2. **Crash recovery throws away in-flight work.** On resume, every `in-progress` attempt is marked `cancelled` and its node goes back to `pending`, so the node gets a new attempt. Separately, any journalled tool call still in `intended` is marked `unknown` only by the 15-minute stale sweep.
3. **"Dependents" in retry-task and timetravel are temporal, not structural.** They are computed from attempt order, timestamps and iteration, not from graph edges. fork and replay never expand to dependents at all.
4. **`--hot` is only partly wired in the production engine.** The watcher and reloader code exists, but `engine.js` never calls it (details in C3).
5. **Node identity is `nodeId::iteration`.** Explicit `id`s are required for tasks, and loop iterations get a `@@loop=N` scope suffix. A node that is finished in the DB is skipped by checking whether its output row exists. That row is never re-validated against the current schema.

---

### A. Durability and scheduling

### A1. The run loop (one "frame" per render)
- **Driver state machine.** `packages/driver/src/WorkflowDriver.js:455-539` (`runUntilTerminal`) renders the graph, submits it to the session, and switches on the decision tag:
  ```js
  switch (decision._tag) {
    case "Execute": { const next = await this.executeTasks(decision.tasks); ...
    case "ReRender": decision = await this.renderAndSubmit(decision.context);
    case "Wait": { const next = await this.handleWait(decision.reason); ...
  ```
  - Pause is checked before every step (`:483-493`). It drains in-flight tasks without aborting them, then returns `{status:"paused"}`.
- **Every render re-reads durable state.**
  - `renderAndSubmit` (`WorkflowDriver.js:562-660`) builds a fresh `SmithersCtx` from base outputs plus live outputs.
  - Signals are fully reloaded on every render (`:576-584`): "a fresh full reload every render is both the simplest and the correct behavior".
  - It then calls `renderer.render(workflow.build(ctx))` and `session.submitGraph(graph)`.
- **Scheduler session.** `packages/scheduler/src/makeWorkflowSession.js`:
  - `submitGraph` → `markGraph(graph); return decide();` (`:1414-1422`).
  - `markGraph` (`:657-690`) rebuilds `descriptors` and the plan tree (`buildPlanTree(graph.xml, …)`) on every submit.
  - `decide()` (`~:1116-1412`) calls `scheduleTasks(plan, states, descriptors, ralphState, retryWait, now, …)` (`packages/scheduler/src/scheduleTasks.js:99`). It walks sequence/parallel/ralph/saga/try-catch nodes, and `dependenciesSatisfied` (`:36-56`) requires every `dependsOn` to be in a terminal state.
  - Runnable tasks are converted to states in `decide` (`makeWorkflowSession.js:1180-1240`): `needsApproval` → `waiting-approval`, `meta.__waitForEvent` → `waiting-event`, `meta.__timer` → `waiting-timer` with `Wait{Timer,resumeAtMs}`, otherwise Execute.
  - Each completion re-renders: `decideAfterOutputChange` → `{_tag:"ReRender"}` when `requireRerenderOnOutputChange` is set (`:739-744`). The engine passes `true` (`engine.js:11552-11556`).
  - **Stable-finish fixpoint** (`:1404-1410`): before returning Finished, the session re-renders once more if the set of mounted task ids changed.
    ```js
    if (options.requireStableFinish && state.graph) {
      const signature = mountedSignature(state.graph);
      if (state.lastMountedSignature !== signature) { ... return { _tag: "ReRender", ...
    ```
- **Frame persistence.** `engine.js:10718-10750` (`persistDriverFrame`) stores `frameNo += 1` with canonical `xmlJson`, `xmlHash`, `mountedTaskIdsJson`, a `taskIndexJson` of (nodeId, iteration, kind, agent, maxAttempts), and a frame snapshot (nodes, outputs, ralph, input), written incrementally with periodic full keyframes (`:9800-9815`). The docs describe the frame as the unit of progress (`docs/llms-full.txt:765-770`).

### A2. Heartbeats, owner leases, liveness, stale thresholds
- **Constants** (`engine.js:1772-1786`): `STALE_ATTEMPT_MS = 15*60*1000`, `RUN_HEARTBEAT_MS = 1_000`, `RUN_HEARTBEAT_STALE_MS = 30_000`, `RUN_CANCEL_POLL_MS = 250`. Task-level heartbeats are separate (`TASK_HEARTBEAT_*`, `:1787-1789`; timeout → `TASK_HEARTBEAT_TIMEOUT`).
- **Engine heartbeat and control poller.** `startRunSupervisor` (`engine.js:3270-3360`):
  - A `setInterval` calls `adapter.heartbeatRun(runId, runtimeOwnerId, now)` every 1 s.
  - Its boolean return acts as a fence; losing it raises `HEARTBEAT_FENCE_LOST` (`:5272`).
  - A `cancelWatcher` loop polls the run row every 250 ms for `hijackRequestedAtMs`, `pauseRequestedAtMs` and `cancelRequestedAtMs`, and aborts the matching controller.
- **Owner id format.** `packages/db/src/runtime-owner.js:18-30`: `pid:${pid}@${hostname}:${sessionId}`. `isPidAlive` uses `kill(pid,0)` and treats EPERM as alive (`:91-99`).
- **Liveness classifier.** `packages/db/src/runDriverLiveness.js:102-142`. Thresholds: `RUN_DRIVER_HEARTBEAT_STALE_MS = 30_000` (`:24`), `PID_RECYCLE_TOLERANCE_MS = 2_000` (`:31`).
  - Local owner: live only if the PID is alive and not recycled (`ps -o lstart` later than the last heartbeat + 2 s means recycled).
  - Remote owner: live only if the heartbeat is fresh.
  - Non-`pid:` id (a resume claim): live only if its heartbeat is fresh.
  - Terminal statuses are never live (`:41`). Override flag: `--steal-ownership` (`:34`). For HPC this matters: a driver on another node can only be judged by its heartbeat.
- **Resume preconditions.** `assertResumeActivationPreconditions` (`engine.js:9055-9111`) refuses with `RUN_OWNER_ALIVE` unless the caller holds the recorded claim or passed `stealOwnership`. A second check refuses with `RUN_STILL_RUNNING` when the heartbeat is fresh and neither `--force` nor steal is set.
- **Atomic claim (compare-and-swap).** `packages/db/src/adapter.js:2036-2117` (`claimRunForResume`):
  ```sql
  UPDATE _smithers_runs SET runtime_owner_id = ?, heartbeat_at_ms = ?
  WHERE run_id = ? AND status = ? AND COALESCE(runtime_owner_id,'') = COALESCE(?,'')
    AND COALESCE(heartbeat_at_ms,-1) = COALESCE(?,-1)
    AND (? = 0 OR heartbeat_at_ms IS NULL OR heartbeat_at_ms < ?)
  ```
  - `releaseRunResumeClaim` (`:2122-2131`) restores the previous owner and heartbeat.
  - `activateRunForResume` (`engine.js:9113-9224`) claims, then `updateClaimedRun` sets `status:"running"`, the new owner and fresh durability metadata.
- **Supervisor** (`apps/cli/src/supervisor.js`):
  - Defaults (`:21-24`): interval 10 s, stale 30 s, maxConcurrent 3, maxResumeAttempts 3. The `supervise` CLI command (`apps/cli/src/index.js:9874`; options `:3129-3136`) requires `--run <ids>` or `--all`.
  - `pollEffect` (`:1078-1230`) scans five candidate classes, in priority order, sharing `maxConcurrent` slots:
    1. Stale `running` runs: `listStaleRunningRuns` = `status='running' AND (heartbeat IS NULL OR heartbeat < now-30s)` (`adapter.js:1968-1984`).
    2. `waiting-timer` runs with a due `firesAtMs` (`:1112-1137`, `runHasDueTimerEffect` `:305-340`).
    3. `waiting-event` runs with an approval decided while detached (`:1141-1156`).
    4. `waiting-approval` runs with a decided gate (`:1160-1183`).
    5. `waiting-quota` runs past their reset time (`:1185-1206`).
  - It only resumes a stale run whose owner is verifiably gone (`:575-605`):
    ```js
    const orphaned = !hasOwner || remoteOwner || (ownerPid !== null && !ownerPidAlive) || priorResumeAttempts > 0;
    ```
  - Claim owner `supervisor:<id>#a<n>` (`:32,49-67`) counts consecutive resumes that died. After the limit, `giveUpOnFailedResumesEffect` (`:466-545`) marks the run failed and raises the `supervisor_auto_resume_gave_up` alert.
  - The resume itself is a detached `smithers up <wf> --resume --run-id <id>` carrying the claim (`:647-656`, `resumeRunDetached`).

### A3. Timers, waitForEvent, signals and approvals with no process alive
- **Timers store an absolute fire time in the attempt metadata.** `effect/deferred-state-bridge.js:480-510` (`buildTimerSnapshot`): `firesAtMs: createdAtMs + delayMs`, or parsed from `until`. `buildTimerAttemptMeta` (`:542-555`) writes `{kind:"timer", timer:{timerId,timerType,duration,until,createdAtMs,firesAtMs,firedAtMs}}`.
  - First visit (`:599-690`) inserts attempt #1 in state `waiting-timer` (or `finished` if already due) and emits `TimerCreated` and `NodeWaitingTimer`.
  - Duration anchors carry across continue-as-new via `initialTimerStarts`.
- **The engine parks instead of sleeping.** `reconcileTimerWait` (`engine.js:10221-10327`) ends with:
  ```js
  const waitMs = Math.max(0, effectiveResumeAtMs - nowMs());
  if (waitMs <= 0) return submitLastGraph();
  return markRunWaiting("waiting-timer", "timer");
  ```
  This applies even to short timers when nothing else is in flight. When siblings are in flight, the driver keeps the timer as a deadline instead (`WorkflowDriver.js:844-867`).
- **`markRunWaiting`** (`engine.js:10026-10069`) persists the parked status and releases the lease:
  ```js
  const patch = { status, heartbeatAtMs: null, runtimeOwnerId: null, cancelRequestedAtMs: null,
                  pauseRequestedAtMs: null, hijackRequestedAtMs: null, hijackTarget: null };
  ```
  `handleDriverWait` (`:10383-10420`) maps wait reasons to statuses: Approval → `waiting-approval`, Event / ExternalTrigger / HotReload / OrphanRecovery / Bound → `waiting-event`, Timer → `waiting-timer`, Quota → `waiting-quota` (with `resetAtMs` in `errorJson`). RetryBackoff sleeps in process.
- **Who polls.**
  - `supervise` reads `metaJson.timer.firesAtMs` (`supervisor.js:205-213`).
  - The gateway runs `processDueTimers` (`packages/server/src/gateway.js:6789-6946`). It scans `waiting-timer`, `waiting-approval` and `waiting-event` runs and the earliest `firesAtMs`, then calls `resumeRunIfNeeded`; it does the same for `waiting-quota` using `resetAtMs`.
- **Signals.** `packages/engine/src/signals.js:45-115` (`signalRun`):
  - Inserts a `_smithers_signals` row with a per-run sequence number (`insertSignalWithNextSeq`) and fans it out to live descendant subflow runs.
  - `bridgeSignalResolve` (`effect/durable-deferred-bridge.js:271-288`) finds nodes in `waiting-event` whose attempt snapshot matches `signalName` and `correlationId`, and marks them resolved.
  - **The CLI `signal` command does not auto-resume** (`index.js:11529-11591`). It only prints an `up … --resume` hint. The gateway's signal endpoints do call `resumeRunIfNeeded` (`gateway.js:5488-5500`, `10970-10975`).
- **Approvals.**
  - `approve` (`index.js:11450-11526`) records the decision, then `maybeResumeDecidedDetachedRun` (`:625`) relaunches detached when the owner is verifiably absent (`--no-resume` opts out).
  - On resume the engine seeds `initialApprovals` from `listDecidedApprovals` (`engine.js:11544-11551`).
- **`up` exit codes.** `formatStatusExitCode` (`index.js:580-592`): finished=0; `waiting-approval`/`waiting-event`/`waiting-timer`/`paused`=3; cancelled=2; anything else=1. `waiting-quota` is not in the list, so it falls through to 1.

### A4. Crash recovery on resume
- **Every start** runs `cancelStaleAttempts` (`engine.js:4483-4513`). For in-progress attempts older than 15 min it, in one transaction:
  - sets the attempt to `cancelled`;
  - calls `markToolCallsUnknownForAttempt` (journal rows `intended` → `unknown`, `adapter.js:4334-4347`);
  - resets the node to `pending`.
- **On `--resume`** (`engine.js:11413-11440`), **all** remaining in-progress attempts are set to `cancelled` with `finishedAtMs`, and their nodes are re-inserted as `pending` with `lastAttempt` kept. The node is re-run as a new attempt. This path does not mark tool calls unknown.
- **Agent resume pointers.**
  - Cancelled and failed attempts' `agentResume` / `agentConversation` pointers are ignored (`packages/db/src/attempt-resume-pointers.js:24,55-57,72-90`), except for `hijackHandoff`.
  - Rationale (`:13-15`): "Discarding a resume pointer costs conversation context, never work: the agent's real state lives in its worktree".
- **Finished tasks are skipped by the presence of an output row.** `executeDriverTask` (`engine.js:10423-10448`) calls `readTaskOutput(task)` (`:9945-9967`). If a row exists for (runId, nodeId, iteration), the node is marked `finished` without executing. That row is not re-validated against the current schema.
- **In-memory orphans.** `recoverOrphanedTasks` (`makeWorkflowSession.js:1578-1592`) flips in-memory `in-progress` states back to `pending`.
- **Other recovery.**
  - Interrupted rewinds are repaired at startup (`recoverRewindAuditsAtStartup`, `index.js:4425-4430`).
  - A frame snapshot never records a finished node without its output row; this is enforced by `findFinishedNodeMissingCachedOutput` (`engine.js:~9850-9880`).

### A5. Idempotency and the effect journal
- **Tool-call journal** `_smithers_tool_calls`. `createToolJournalContext.js:42-80`: on `phase:"started"` it inserts a row with `status:"intended"`, a random `callToken`, and provenance flags `{sideEffect, idempotent, acceptsIdempotencyKey, hasRevert, idempotencyKey}`. `completeToolJournalCall.js` finalizes the row.
- **Default idempotency key.** `packages/tool-context/src/toolContext.js:50-61` derives a deterministic per-node key that is stable across attempts:
  ```js
  return `smithers:${ctx.runId}:${ctx.nodeId}:${ctx.iteration ?? 0}`;
  ```
  `defineTool.js:60-100`: `idempotent` defaults to `!sideEffect`, and the key is passed only if `execute` takes two arguments. `revert` requires `sideEffect:true`.
- **Effect boundary for time travel.** `packages/time-travel/src/assessEffectBoundary.js`:
  - `effectStatus` (`:14-31`): `intended`, `started`, `unknown` and `failed` all count as `"unknown"`.
  - Classification (`:200-214`): side effects with a revert handler are revertible; without one they are blocking; previously forced (archived) effects become warnings.
  - `guardEffectBoundary.js` requires `--force` to cross blocking effects, and the run is marked `needs_attention` (`jumpToFrame.js:574-600`).
- **Workflow versioning.** `usePatched(patchId)` (`effect/versioning.js:40-64,119-122`) is a Temporal-style `patched()`. A decision is recorded once per run (`decision = options.isNewRun`) and replays thereafter.

### A6. Resume validation
- **What is hashed.** `getRunDurabilityMetadata` (`engine.js:2906-2930`):
  - `workflowHash` = `readWorkflowGraphHash`, a sha256 over the sorted `path:sha256(source)` entries of the transitive import graph (`workflow-hash.js:112-150`);
  - `entryWorkflowHash` = sha of the entry file only;
  - `vcsType`, `vcsRoot`, `vcsRevision` (git `rev-parse HEAD`, or the jj `commit_id`).
- **`assertResumeDurabilityMetadata`** (`engine.js:2992-3086`):
  - Mismatch labels: "workflow path changed", "workflow module graph changed", "workflow entry file changed", "…unavailable", "VCS root changed".
  - `vcsRevision` is recorded but **not compared**; only the VCS root is checked.
  - `--accept-workflow-change` waives only the four hash labels (`:3047-3056`):
    ```js
    const acceptedWorkflowMismatches = options.acceptWorkflowChange === true
      ? mismatches.filter((m) => workflowHashMismatchLabels.includes(m)) : [];
    ```
  - Otherwise it throws `RESUME_METADATA_MISMATCH` with a hint to use `--accept-workflow-change` (which re-blesses the metadata in place: "you own replay determinism") or `fork`.
  - A workflow-name mismatch is a separate check during the first render (`engine.js:11585-11610`).
- **`up` flags** (`index.js:2935-2980`): `--resume`, `--force` ("Resume even if still marked running"), `--steal-ownership`, `--accept-workflow-change`, plus internal `--resume-claim-owner`/`--resume-claim-heartbeat`. `RESUMABLE_RUN_STATUSES` (`engine.js:3434-3444`) includes finished, failed and cancelled.

---

### B. Control verbs: exact semantics

### retry-task
- **CLI** (`index.js:12129-12283`):
  ```js
  deps: z.boolean().default(true).describe("Also reset dependents. Use --no-deps to reset only this node."),
  detach: ..."After the reset, resume the run in the background (like `up -d`) and exit"
  force: ..."Allow retry even if run is still running"
  stealOwnership, acceptWorkflowChange
  ```
- **Flow.**
  1. `preflightRetryResume` checks resume metadata.
  2. A claim `supervisor:retry-task:<pid>:<uuid>` is created.
  3. `retryTask` performs the DB reset and stamps the claim.
  4. The CLI either resumes in process (`runWorkflow({resume:true, resumeClaim})`) or detaches with `resumeRunDetached`.
  5. If the engine never attaches, `rollbackFailedRetryResume` undoes the reset.
- **Core** (`packages/time-travel/src/retry-task.js:267-487`):
  - Refuses a live driver unless steal is set (`:316-332`).
  - Refuses an active status without `--force` (`:333-343`).
  - `validateWorkflowIdentity` unless `acceptWorkflowChange` (`:344-354`).
- **How dependents are computed** (`resolveResetNodes`, `:134-160`). They are **temporal, not graph edges**: a node is reset if it has a higher iteration, a later position in the attempt list, or (fallback) a later `updatedAtMs`:
  ```js
  if (nodeIteration > targetIteration) return true;
  if (targetOrder !== undefined && nodeOrder !== undefined) return nodeOrder > targetOrder;
  return (node.updatedAtMs ?? 0) > targetUpdatedAtMs;
  ```
  - `--no-deps` resets only `[targetNode]`.
  - Child subflow runs owned by reset nodes are reset too (`resolveChildResetPlans`, `:172-250`), including continue-as-new successors.
- **Reset transaction** (`:398-462`):
  - Attempts in finished, failed, in-progress or any waiting-* state become `cancelled`, with a reset marker in their meta.
  - The output row is deleted (`deleteOutputRowEffect`) and the node is re-inserted as `pending`.
  - The run is set to `status:"running"` with the claim as owner. Frames and snapshots are **not** truncated.

### cancel
- **CLI** (`index.js:11664-11780`): options `{cwd}` only; exits with code 2.
- **Implementation:** `cancelRunSubtree` (`packages/engine/src/cancel-subtree.js:345-440`).
  - It cascades over `listRunDescendants`, pruning fork subtrees, for up to 5 discovery passes.
  - **Live run** (status running, fresh heartbeat, owner not known dead): it only sets the durable `cancel_requested_at_ms` (`requestRunCancel`). The owner's 250 ms watcher aborts in-flight tasks (`engine.js:3327-3339`), waits up to `RUN_ABORT_SETTLE_TIMEOUT_MS = 5_000` for them to settle, then finalizes.
  - **Stale, waiting or paused run:** `finalizeCancelledOwnedRun` flips the run to `cancelled` in a fenced write, cancels active attempts and pending approvals, human requests and timers (`cancelPendingExternalWaits`, `engine.js:4515+`), and expires queued steers.
  - It then kills any surviving owner process tree (`terminateRunOwner` → `killProcessTree`, 2 s grace, `:66-68`) and every registered agent PID for the subtree (1 s grace, `:87-133`, from the agent process registry).

### steer
- **CLI options** (`index.js:3483-3510`): `message`, `--node`, `--takeover` ("Hijack the live agent session instead of queuing a steer; run-wide…"), `--yes`, `--timeout-ms`.
- **Storage.** `enqueueSteer` (`packages/engine/src/steers.js:46-93`) inserts a `_smithers_steers` row `{steerId, runId, nodeId, message, author, status:"queued"}` together with a `SteerQueued` event in one transaction. It is idempotent on `steerId` and requires an active target: `STEER_ACTIVE_RUN_STATUSES = running|waiting-approval|waiting-event|waiting-timer` (`adapter.js:112`).
- **Delivery** happens only at the next agent `generate()` for that node: first start, retry attempt, or loop iteration (`engine.js:6835-6900`). Steers are appended as user turns (conversation mode) or added to the prompt, and marked `consumed` before `generate()`. This is at-most-once: "a crash in the narrow window … may drop the steer".
- **Expiry.** Queued steers expire when the run reaches a terminal state (`steers.js:146-166`), except when the cancel was a hijack (`engine.js:4949-4952`).

### hijack
- **CLI** (`index.js:3477-3482,10681-10715`): `--target <engine|nodeId>`, `--timeout-ms 30000`, `--launch`. The flow is `runHijackFlow` (`index.js:8203-8400`).
- **If the run is live:**
  1. `requestRunHijack` writes `hijack_requested_at_ms` and the target, plus a `RunHijackRequested` event.
  2. The CLI polls `waitForHijackCandidate`.
  3. The engine's watcher sees the request (`engine.js:3289-3310`). `maybeCompleteHijack` (`:7056-7130`) waits until the agent is at a safe point (no active CLI actions; for codex/omp, the turn has completed).
  4. The engine writes `attemptMeta.hijackHandoff = {engine, mode:"native-cli"|"conversation", resume, messages, cwd, …}`, emits `RunHijacked`, and **aborts the whole run**. Other in-flight siblings are aborted and re-run later.
  5. The run is finalized as `cancelled` with `errorJson.code="RUN_HIJACKED"` (`engine.js:11866-11877`). This is treated as a resumable handoff: steers are preserved and resume pointers kept.
- **Then** the CLI launches the native session (e.g. `claude --resume <id>`). When that session exits with code 0 and the run was live, it auto-relaunches `up --resume` detached (`index.js:~8364-8372`); otherwise it prints the resume command.

### rewind (frame)
- **CLI** (`index.js:12778-12815`): `rewind <runId> <frameNo> [--yes] [--json] [--force] [--no-revert]`. It calls `jumpToFrame` (`packages/time-travel/src/jumpToFrame.js:806+`).
- **Steps** (`JumpStepName.ts`): snapshot-pre-jump, pause-event-loop, revert-effects, revert-sandboxes (jj), truncate-frames, truncate-attempts, truncate-outputs, invalidate-diffs, rebuild-reconciler, resume-event-loop.
- **Inside one transaction** (`:1120-1260`):
  - `deleteFramesAfter`, `deleteSnapshotsAfter` and `deleteVcsTagsAfter`.
  - `archiveDiscardedEffects`; attempts outside the snapshot are deleted.
  - When an exact snapshot exists, outputs, nodes and attempts are restored from it (and signals with seq above the horizon are deleted). Otherwise a legacy heuristic deletes outputs and resets nodes to pending.
  - Finally `updateRun({status:"running", heartbeatAtMs:null, runtimeOwnerId:null,…})`, so a supervisor then sees it as stale and resumes it.
- **Guards:** live run (`isRunLikelyLive`) unless `--force`, a rewind lock, a rate limit, and a durable audit row.

### fork
- **CLI** (`index.js:12820-12930`):
  ```js
  frame: z.number().int().describe("Frame number to fork from"),
  resetNode: ..."Node ID to reset to pending; comma-separate ids ... (dependents are not reset for you)",
  input, label, run: ..."Immediately start the forked run", force
  ```
- **What is copied** (`time-travel/src/fork/forkRunEffect.js:512-760`):
  - The source snapshot at `frameNo` becomes child snapshot frame 0 (nodes, outputs, ralph, input with overrides, `vcsPointer`).
  - The child run row: new UUID, `parentRunId`, `status: parent.status==="running" ? "failed" : parent.status`, owner null.
  - The workflow hash can come from the current file (the fork CLI passes the hashes of the edited file).
  - Output provenance rows are copied, signals are copied up to `__smithersSignalProvenanceHorizon`, agent checkpoints are inherited, and a `_smithers_branches` row is written.
- **Reset nodes:** set to `{state:"pending", lastAttempt:null}` (`:581-608`). Only the named nodes are reset, matched by base id across all iterations (`fork/_helpers.js:28+`; README: "never their downstream dependents … a fork may target an edited workflow whose edges differ").

### replay
- **CLI** (`index.js:12530-12620`): `--frame`, `--node`, `--input`, `--label`, `--restore-vcs` ("Restore jj filesystem state to the source frame's revision"), `--force`.
- **Implementation** (`replayFromCheckpointEffect.js:30-75`): `forkRun` with `autoRun:true`, then optionally `rerunAtRevision`. That loads the frame's VCS tag and runs `revertToJjPointer(tag.vcsPointer, cwd)` (`vcs-version/rerunAtRevisionEffect.js`). This is jj only.

### timetravel
- **CLI** (`index.js:12288-12390`): `--node-id`, `--iteration`, `--attempt` (default latest), `--vcs/--no-vcs`, `--deps/--no-deps`, `--resume`, `--force`, `--steal-ownership`, `--revert/--no-revert`.
- **Implementation** (`timetravel.js:238+`):
  - Optional `revertToJjPointer(targetAttempt.jjPointer)` for the filesystem.
  - Dependents are temporal (`:73-100`): same iteration-or-later, or attempts started at or after the target's `startedAtMs`.
  - Deletes frames, snapshots and VCS tags created after the target attempt started (`:428-440`).
  - Archives effects, cancels attempts, deletes outputs, sets nodes to pending, and sets the run to `running` with no owner. With `--resume`, it resumes.

### revert (attempt)
- `revertOptions` (`index.js:3517-3524`) → `revertToAttempt` (`time-travel/src/revert.js:17-110`).
- This is a **filesystem-only** jj revert to `attemptRow.jjPointer`, with the effect-boundary guard, rewind lock and audit row. It does not reset DB state.

### pause
- Sets `pause_requested_at_ms`. The engine stops scheduling, drains in-flight tasks without aborting them, and the run is parked as `paused` (`WorkflowDriver.js:483-493`, `engine.js:3313-3326`).

---

### C. The dynamic graph

### C1. Re-derivation each frame
- `workflow.build(ctx)` is re-rendered from scratch through the React reconciler on every ReRender. The resulting graph replaces the session's descriptors and plan (`markGraph`). `ctx.outputs` comes from the DB base plus live completions, and signals are reloaded each render.
- Tasks with unresolved dependencies are tracked in `_deferredDeps`. If any survive to Finished, the run fails with `DEPENDENCY_DEADLOCK` (`WorkflowDriver.js:513-522, 649-651`).
- New nodes get `pending` rows in `persistDriverGraphTaskStates` (`engine.js:10886-10960`). Existing node rows are never overwritten by a render (`if (previous != null) continue;`), and `skipIf` nodes get `skipped`.

### C2. Node identity
- **Explicit ids are required for tasks.** `requireTaskId`: "id is required and must be a string" (`packages/graph/src/extract.js:466-471`). Structural nodes fall back to a path id `prefix:0.2.1` (`graph/src/utils/tree-ids.js:6-21`). Duplicates throw `DUPLICATE_ID` (`extract.js:508`).
- **Loop scoping.** Loops (`<Loop>`/`<Ralph>`) add a scope suffix `@@<ralphId>=<iteration>` (`extract.js:245-248`), and the `iteration` field comes from the persisted ralph state (`getRalphIteration`; `adapter.listRalph`, `engine.js:11508`). Nested loops are rejected (`:535-542`).
- **State key** = `${nodeId}::${iteration}` (`scheduler/src/buildStateKey.js`). This is the primary key for nodes, attempts and output rows.
- **Graph types** (`graph/src/types.ts:269-280`):
  ```ts
  type WorkflowGraph = { xml; tasks: TaskDescriptor[]; mountedTaskIds: string[] };
  type GraphSnapshot = { runId; frameNo; xml; tasks: TaskDescriptor[] };
  ```
  Persisted per frame as `_smithers_frames.xmlJson` / `mountedTaskIdsJson` / `taskIndexJson`.

### C3. Hot reload (`up --hot`)
- **The pieces exist** in `packages/engine/src/hot/`:
  - `WatchTree` (fs.watch plus adaptive polling);
  - per-generation overlays copied or hardlinked into `.smithers/hmr/gen-N`;
  - `HotWorkflowController.reloadEffect` (`:158-210`), which does a dynamic `import()` of the overlay entry and classifies the result as reloaded, failed or unsafe;
  - the session's `hotReloaded(graph)` → `markGraph(graph,{pruneUnmounted:true})` (`makeWorkflowSession.js:1524-1532`).
- **Schema guard.** With `SMITHERS_HOT=1`, `createSmithers` compares a DDL signature of all output schemas and throws `SCHEMA_CHANGE_HOT` ("Schema change detected; restart required") when it changes (`packages/smithers/src/create.js:446-455`).
- **Finding: the production path is only partly wired in v0.35.**
  - `up --hot` only sets `process.env.SMITHERS_HOT="1"` (`index.js:4345-4347`), prints "[hot] Hot reload enabled" (`:4418`), and logs `hotReload:true` (`engine.js:11199`).
  - In `engine.js`, `driver` and `apps/cli`, I found no instantiation of `HotWorkflowController`/`WatchTree` and no call to `session.hotReloaded`. The only callers are in `packages/testing` (`coverWorkflow.js:1267`).
  - The docs claim edits "apply on the next render frame … finished tasks stay persisted" (`docs/llms-full.txt:2224-2240, 8761, 8845-8852`). Treat hot reload as a design reference, not proven behaviour.
- **Intended semantics** (docs plus `pruneUnmounted`): prompt and body edits apply to newly scheduled tasks while in-flight tasks finish on their old code. Restart when output schemas or task id shapes change. Keep ids "stable and data-derived".

### C4. When the definition changes: unmounted ids, schema changes, orphaned outputs
- **Unmounted nodes.** `pruneUnmounted` (`makeWorkflowSession.js:662-679`) drops in-memory state for keys no longer mounted; `in-progress` becomes `cancelled`. DB node rows and output rows are **not deleted**, so they remain as orphans and reappear as finished if the id re-mounts.
- **Late results.** A completion or failure for a task that left the graph is recorded but not fatal (`:1424-1447`): "record it (so it is available if the task re-mounts)".
- **Schema evolution.** Output tables are only ever extended: `syncZodTableSchema` does `ALTER TABLE ADD COLUMN` for new fields (`create.js:534-537`) and never drops columns. An old output row still marks a node finished on resume (`readTaskOutput` does no re-validation), so downstream code may see old-shape data.
- **Resume after any source edit** is blocked by `RESUME_METADATA_MISMATCH` (A6) unless `--accept-workflow-change` is passed. In that case, ids that still exist keep their outputs; new ids start `pending`; removed ids are left as orphans.
- **Alternative:** `fork --frame N --reset-node X` against the edited file branches the run and re-blesses its hashes.


## Appendix C — 1.0 control/plan/journal/time-travel + durable-external-work (park on job)

### Smithers 1.0.0-rc: control, plan, journal, time-travel and durable external work

All paths are relative to `forgeflow/context/repos/smithers-main/`. `P=packages/smithers`, `F=packages/smithers/flows`. Everything here was read directly. Two status points first:

- **Most of `docs/design/durable-external-work.md` is already implemented in this checkout.** I found `F/flow/src/ExternalJob.ts`, `F/sandbox/src/Sandbox/job.ts`, `Consensus.reconfirm`, the `lease-reconfirmed` decision, and the `keyed` attempt meta used in `canRetryReleased`. Section 7 compares the code with the doc's API sketch.
- **Growing a plan mid-run does not need re-approval.** Approval binds to the generation-0 `baseDigest`. Appends move only `digest` and are journaled; nothing re-asks. Details in section 2.

---

### 1. Control service (`@smthrs/control`)

### 1.1 Verbs: `P/control/src/Control.ts:210-295`
```ts
plan:    (PlanInput)        => Effect<PlanCard, FlowNotFound|InvalidInput|PersistenceError|Unavailable|...>
run:     (RunInput)         => Effect<Receipt, RunNotFound|PlanNotFound|PlanDenied|PlanDigestMismatch|EnvelopeMismatch|ClaimLost|CodeDrift|LaunchFailed|...>
approve/deny: (ApprovalInput)=> Effect<Receipt, PlanDigestMismatch|EnvelopeMismatch|AlreadyResolved|PlanNotFound|RunNotFound|CodeDrift|Unauthorized|...>
steer:   (SteerInput)       => Effect<Receipt, RunNotFound|InvalidInput|...|NotificationError>
signal:  (SignalInput)      => Effect<Receipt, RunNotFound|NoMatchingWait|...>
cancel:  (RunMutationInput) => Effect<Receipt, RunNotFound|ClaimLost|...>
resume:  (RunMutationInput) => Effect<Receipt, RunNotFound|ClaimLost|CodeDrift|...>
list:    (ListInput)        => Effect<ListResponse, ControlError>
watch:   (WatchInput)       => Stream<ControlEvent, ControlError>   // "Checkpoint event.cursor ... resume with afterCursor"
```

**Inputs**
- `PlanInput` (`Control.ts:57-63`): `{flowId, input: unknown, idempotencyKey?, budget?}`. The comment on `budget` reads: "Budget fields that replace the flow's declared ones on this plan's envelope".
- `RunMutationInput` (`Control.ts:169-200`): `{runId, idempotencyKey, reason?, principal?, allowCodeDrift?, reader?}`.
  - `reason` is "recorded on the journal entry ... projected back onto RunSummary.cancellation".
  - `allowCodeDrift`: "Resume only: run the flow's current code even when its execution digest differs".
- `RunInputSchema` (`ControlSchema.ts:828-841`) is a union:
  - `Plan{planId, digest, envelope, idempotencyKey}`
  - `Resume{runId, idempotencyKey, allowCodeDrift?}`
- `SignalPayload` (`ControlSchema.ts:792-795`): `{name, payload: Json}`. `SignalInputSchema` is `{runId, signal, idempotencyKey}` (`:871-875`).
- `WatchFilter` (`ControlSchema.ts:953-960`): `{runId?, afterSequence?, afterCursor?, follow?}`.
- `ControlEvent` (`:976-984`): `{cursor?, sequence, kind, runId?, occurredAt, payload}`.

**Receipts** (`ControlSchema.ts:1346-1361`):
- `Accepted{receiptId, runId?, handedTo?}`
- `AlreadyApplied{receiptId, runId?}`
- `Parked{receiptId, planId, status:"waiting-approval"}`
- `Conflict{message}`
- `Terminal{runId, status}`

The README (`P/control/README.md:82-87`) defines them: "`Accepted` means this call did the work, `AlreadyApplied` means an earlier call under the same idempotency key did, `Conflict` means the key names a different intent, `Parked` means the plan is waiting for an approval".

### 1.2 `PlanCard`: exact fields (`ControlSchema.ts:362-389`)
```ts
PlanCard = { planId, flowId, digest, inputSummary,
  warnings?: DiscoveryWarning[],            // "outside the approval identity"
  envelope: Envelope, deployClass: boolean,
  executionDigest?: string,                 // "Executable source/metadata identity"
  plan?: PersistedPlan.Plan,
  nodes: PlanNode[],                        // PersistedPlan.PlanNode fields + status: "cached"|"run"
  graph?: PlanGraph,                        // edges{from,to,reason}, nodes{id,declaredAt}, sourceRevision(40-hex)
  approval: ApprovalPayload }               // {target, scope, idempotencyKey}
```
- **`PlanNodeStatus`** (`:189`) is `["cached","run"]`. The comment says: "outcomes fall out of step keys for free — a key either hits the step cache or it does not".
- **`PlanEdgeReason`** (`:251`) is `value | continuation | failure | conflict | lane-merge`.
- **`graph` is deliberately outside the digest** (`:376-386`): "the edges and the declaration sites describe the plan a reader draws, and nothing here changes what will run."
- **Why the node graph is inside the digest** (`:349-358`): "'approve this flow with this input' and 'approve this graph of keyed work' are different promises: a change that re-keys a node changes what will run, and an approval taken against the old graph must not authorize the new one."

### 1.3 Envelope (`ControlSchema.ts:93-107`)
`Envelope = {capabilities: string[], hostImports?: string[], flows: string[], budget: {tokens?, milliseconds?, usd?, onExceeded?, deadline?: int>0}, host?: string}`.

- The comment calls it "The capabilities, flows, budget, and placement approved for a plan". `deadline` is wall-clock milliseconds "counted from the run's first start, shared by every round".
- An envelope is the authority being granted. It is resubmitted with every approval and run, and compared by canonical bytes: `sameEnvelope = canonical(left) === canonical(right)` (`control/src/internal/planning.ts:128`).
- An approval installs a `BulkGrant{tokenId, envelope, scope, installedAt}` (`ControlRuntime.ts:317-322`). The comment says: "The envelope is deliberately not split into individual capabilities".
- `GrantScope` is `once | run | remembered` (`ControlSchema.ts:121`).

### 1.4 Digest computation (`control/src/internal/planning.ts`)
- The header (`:4-7`) explains why this lives in one place: "The plan digest is what an approval is bound to, so the memory runtime and the durable runtime must compute it the same way".
- Canonicalization is `canonicalize` from `@smthrs/canonical` (RFC 8785 serializer, `F/canonical/src/Serializer.ts:2`). The hash is SHA-256: `digest = decode(Sha256)(canonical(value))` (`:120`).
- Inputs to the digest (`:219-229`):
```ts
digest({ flowId, input: decodedInput, envelope, deployClass,
         executionDigest?,                 // code identity of the flow
         persistedPlan: plan?.digest ?? null })  // covers keys, edges, effects, conflicts, priorities, generations
```
- The comment at `:225-227`: "Hashing only node keys loses executable graph changes whose content keys legitimately stay stable."
- The approval target is `{_tag:"Plan", planId, digest, envelope}`. The default scope is `"run"` and the default key is `approve:${planId}` (`:230-254`).
- `inputSummary = canonical(decodedInput)` (`:240`).
- Plan idempotency fingerprint is `canonical({flowId, input, budget?})` (`:146-151`): "a key replayed with another budget is another plan".
- Mutation fingerprint (`ControlLive.ts:167-174`): `control-mutation:v2:${sha256(canonical({operation, actor:{id,kind}, intent}))}`. The durable key is `${op}:actor:${sha256(canonical({id,kind}))}:${key}` (`planning.ts:107-112`). In other words, the idempotency key is namespaced per principal.

### 1.5 How approval binds, and how the first run parks
1. `Control.plan` (`ControlLive.ts:1676-1725`) calls `runtime.plan`, which stores the plan with `decision: "pending"` (`ControlRuntime.ts:1022`). It then journals `control.plan.created {planId, flowId, digest}` on the pseudo-run `plan:<planId>`, inside `journal.transact`.
2. `Control.run(Plan{planId,digest,envelope})` (`ControlLive.ts:1727-1792`) calls `runtime.launch` (`ControlRuntime.ts:1180-1250`), which does these checks in order:
   - `plan.card.digest !== requestedDigest` fails `PlanDigestMismatch{planId, expected, actual}`.
   - Envelope canonical mismatch fails `EnvelopeMismatch`.
   - `decision === "pending"` returns `Parked{status:"waiting-approval"}`.
   - Decision not approved fails `PlanDenied`.
   - Otherwise it creates the run with `planDigest`, `executionDigest`, `engineVersion` and `deadlineAt = now + budget.deadline`.
3. A `Parked` receipt is **not recorded** under the idempotency key; the run-key claim is released instead (`ControlLive.ts:551-554`). Retrying the same `run` after approval therefore launches the run.
4. `approve`/`deny` go through `decide` (`ControlLive.ts:660-770`), in this order:
   - Authorize first: "Authorization precedes target reads and idempotency replay".
   - `lookupApproval` re-checks the digest and envelope against the stored token (`ControlRuntime.ts:1086-1101`). It raises `PlanDigestMismatch`, `EnvelopeMismatch`, or `AlreadyResolved` if the token is not `Pending`.
   - `resolveApproval` runs exactly once, then `installBulkGrant` runs (approval only), then the journal gets `control.approval.{approved|denied}`.
   - A plan approval returns `Accepted` and does **not** auto-launch.
   - A node (in-run) approval records a durable resume delegation (`requestResume`) for the host that parked the run. The comment: "The restart is recorded, not performed, and this plane does NOT claim the row".
   - An unauthorized refusal still writes the audit row `control.approval.refused`.
5. Node targets are `{_tag:"Node", runId, requestId, digest, envelope}` (`ControlSchema.ts:139-152`). Registration is idempotent through `registerApproval` (`ControlRuntime.ts:504-520`): "the executor calls it on every parked attempt — and returns the token with its current tagged decision so a resumed attempt can read the decision instead of parking again".

### 1.6 Errors (`ControlError.ts`)
| Error | Where | Fields / meaning |
|---|---|---|
| `PlanDigestMismatch` | `:98` | `code:"plan_digest_mismatch"`, `planId`, `expected`, `actual` |
| `EnvelopeMismatch` | `:111` | — |
| `ClaimLost` | `:127` | `{runId, reason?, parkedBy?: RunHost}`: "it, not the caller, drives the run, so Control.resume hands the resume to it" |
| `CodeDrift` | `:142-150` | `{recorded, current, recordedEngine, currentEngine}`. Raised on resume or node-approve when the flow's execution digest or engine version changed; overridden by `allowCodeDrift` (`planning.ts:41-57`) |
| `NoMatchingWait` | `:285` | `{runId, waitName}` |
| Others | — | `PlanDenied` (`:59`), `AlreadyResolved` (`:179`) |

All codes are listed in `README.md:111-126`.

### 1.7 signal, steer, cancel, resume
- **signal** (`ControlLive.ts:1872-1930`):
  - `Accepted` means durable admission into `control_signal_commands` (state `pending|delivered|rejected|terminal`, `migrations/0003_signal_commands.ts:17-24`), followed by `executor.deliverSignal`.
  - `no-match` is persisted as `rejected` and raises `NoMatchingWait`. The README (`:89-94`): "that rejected disposition survives retries ... queued commands are never announced as delivered."
- **cancel**: the request goes onto the engine row's `cancel_requested_at_ms` through the `ControlExecutor`, and the owning driver settles it (`README.md:138-142`).
- **resume** (`ControlLive.ts:~770-800` comment): if a live host parked the run, the resume is handed to that host (`handedTo`). The durable delegation carries a `consent_seq` (`migrations/0007_resume_consent.ts`), which "the host records as the per-release retry permission before it re-drives the run (#2982)".

**Control SQL** (`migrations/0001_control_tables.ts:13-80`):
- `control_plans(plan_id PK, card_json, decoded_input_json, decision)`
- `control_plan_keys(idempotency_key PK, fingerprint, plan_id)`
- `control_tokens(target_tag∈{Plan,Node}, run_id, target_id, token_id, target_json, resolved, decision_principal_json)`
- `control_grants(..., envelope_json, scope, installed_at_ms)`
- `control_mutations(mutation_key PK, fingerprint, receipt_json)`
- `control_run_resumes(run_id PK, requested_seq, requested_at_ms[, consent_seq])`
- `control_run_messages(seq, run_id, kind, payload_json)`

---

### 2. Plan and plan store (`@smthrs/plan`, `@smthrs/plan-store`)

### 2.1 Keyed action graph (`F/plan/src/Plan.ts`)
- `PlanNode` (`:145-157`): `{id, kind, key: StoredKey, material: KeyMaterial, effects: NodeEffects, dependsOn: string[], conflicts: ConflictAnnotation[], strategy, runtime, priority:int, generation:int}`.
- `Plan` (`:182-189`): `{planId, flow, generation, baseDigest, digest, nodes}`. The doc comment (`:174-176`): "`baseDigest` is the digest at generation 0: what a human approved and what a `RUNNING` run pins. `digest` advances with every appended elaboration."
- `NodeEffects` (`:41-51`): `{reads, writes, removes?, boundaryMode}`, all workspace-relative path patterns.
- `ConflictAnnotation` (`:111-116`): `{with, paths, strategy: serialize|lane|fail, runtime: delay-rebase|stop-merge}`.
- `KeyMaterial` (`F/plan/src/KeyMaterial.ts`): `{version:"flows/key-material/v2", kind: sealed|compensable|irreversible, nondeterministic?, body, inputs: InputRef[], layers, capabilities, effects?, placement?}`.
  - `InputRef` is a tagged union: `Literal{value}` | `Ref{from, path}` | `Pending{from}`. "the tag is hashed".
  - Node keys are `key1_` + SHA-256 of the canonical serialization (`StepKey.ts:41-43`).
  - Edges come from material refs, plus ordering edges that are "deliberately NOT part of the key" (`Plan.ts:128-136`).
- README rule (`F/plan/README.md:142-145`): "**Invalidation is re-keying.** A node's key is a function of what it consumes, so an edited declaration re-keys that node and its dependent cone ... no reverse-dependency index".

**Plan digest** (`Plan.ts:352-386`): `digestOf = StepKey.content(approvalPayload)`, where
```ts
approvalPayload = { body:{kind:"plan", planId, flow},
  inputs:{ nodes: nodes.map(n => ({id, kind, key, dependsOn, conflicts, effects, strategy, runtime, priority})) },
  layers:[], capabilities:{} }
```
Its comment: "The single approval projection. Its node list is the complete reviewable and behavior-bearing contract".

### 2.2 Growth: `append`
- `Plan.append(plan, drafts)` (`Plan.ts:717-748`): "The plan GROWS; it is never invalidated. Nodes already in it keep their id, key, edges, and generation byte for byte ... Re-ordering after a reconciliation happens by re-keying *future* steps, never by rewriting history." It sets `generation+1` and recomputes `digest` over all nodes. **`baseDigest` does not move.**
- `Plan.verify` (`:762-836`) rebuilds every key and both digests and refuses with `invalid_plan` on any mismatch.
- `prefixDigest` (`:865`) is the digest before the last append.

**Re-approval on growth: none.** `F/plan/docs/concepts/plan-value.md:99-102`: "A control plane binds an approval to `baseDigest`, so a plan that grew during the run still validates against the decision that admitted it."
- `PlanScheduler.append` (`F/engine-store/src/PlanScheduler.ts:573-597`) writes the store row and journals `flows.engine.subgraph-appended {planId, digest, baseDigest, generation, nodeIds, graph}` in one `journal.transact`.
- Scheduler records always use the fenced durable channel (`:503-515`): "a plan digest binds an approval, so it may never ride the lossy queue".
- Unexpected human-approval gates inside a run use Node-target approvals (§1.5) or `HumanTask` (§6), not plan re-approval.

### 2.3 Plan diff (`F/plan/src/PlanDiff.ts`)
```ts
interface Rekeyed { id; from: string; to: string; changed: string[] }   // :28-40
interface PlanDiff { added: string[]; removed: string[]; rekeyed: Rekeyed[]; unchanged: string[] } // :49-54
```
- Verdict by key, attribution by field (`:9-12`): "The **verdict** is the key ... The **attribution** — `changed: ["input[1]"]` — is a report for a human ... deliberately not part of any digest."
- `changedFields` (`:118-155`) compares `version, kind, body, nondeterministic, effects, placement, input[i], layers, capabilities`. An input is also blamed when the upstream it references was re-keyed (`:148`).

### 2.4 Plan-store SQL (`F/plan-store/src/internal/migrations/`)
- `0001_initial.ts:33-62` creates three tables:
  - `flows_plans(plan_id PK, flow, base_digest, digest, generation>=0, created_at_ms)`
  - `flows_plan_nodes(plan_id, node_id, generation, ordinal, kind∈{step,agent,merge}, key_digest, node_json JSON, PK(plan_id,node_id))`
  - `flows_plan_edges(plan_id, from_node, to_node, PK(...))`
- Triggers in `0001_initial.ts:66-100`:
  - `flows_plan_nodes_append_only` (BEFORE UPDATE) and `_no_delete`
  - `flows_plan_edges_append_only` and `_no_delete`
  - `flows_plans_forward_only` (BEFORE UPDATE `WHEN NEW.generation <= OLD.generation OR NEW.base_digest <> OLD.base_digest`, reject "a plan only grows")
- `0002_append_only_hardening.ts:38-58` adds `flows_plans_no_delete`, extends forward-only to `flow` and `created_at_ms`, and adds a unique `(plan_id, ordinal)` index.
- `0003_forward_only_identity.ts:34-44` also pins `plan_id`.

**Store API** (`PlanStore.ts:75-97`):
- `record(plan, createdAtMs) → Recorded | ExistingSame | Conflict{digest}` (first writer wins).
- `append(plan)`.
- `get(planId)`, which re-verifies on read.

**Append is a compare-and-swap** (`PlanStore.ts:284-290`):
```sql
UPDATE flows_plans SET digest=:new, generation=:g
WHERE plan_id=:id AND generation=:g-1 AND flow=:flow AND base_digest=:base AND digest=:prefixDigest
```
If zero rows change, the append fails with `constraint` and the inserted node rows roll back with the transaction (`:291-325`).

---

### 3. Journal (`@smthrs/journal`)

### 3.1 Tables (`F/journal/src/migrations/`)
- **Events** (`0001_initial.ts:20-37`): `flows_journal_events(run_id, seq, event_id UNIQUE, source_id, source_seq, emitted_at_ms, event_type, payload_json JSON, meta_json JSON, PRIMARY KEY(run_id, seq), UNIQUE(run_id, source_id, source_seq))`. Indexes are on `event_type` and `(run_id, event_type, seq)` (`0005`).
- **Checkpoints** (`0002_checkpoints.ts:32-45`): `flows_journal_checkpoints(run_id, seq, state_json, created_at_ms, compacted_at_ms?, PK(run_id,seq))`.
- **Dedup** (`0004_dedup.ts:19-41`): `flows_journal_dedup(run_id, source_id, source_seq, event_id, seq, content_hash len=64)`, plus a BEFORE INSERT trigger with `RAISE(IGNORE)`. This keeps producer identity unique even after compaction deleted the event rows.
- **Leases** (`0006_consensus.ts:27-46`): `flows_consensus_leases(run_id PK, owner_host_id, owner_pid, owner_nonce, granted_at_ms, heartbeat_at_ms, claim_host_id, claim_pid, claim_nonce, claimed_at_ms)`.
- There is no append-only trigger on events. Immutability is by API: no update verb, and `compact` deletes only below a checkpoint.

### 3.2 Row model (`F/journal/src/JournalEvent.ts`)
- `Input` (`~:201-211`): `{runId, sourceId, sourceSeq?, dedupe?: "content"|"identity", eventType (1..1024), payload, meta?}`.
- `Entry` (`~:220-243`): `{runId, seq, eventId, sourceId, sourceSeq, emittedAtMs, eventType, payload, meta}`. The comment: "`seq` is allocated synchronously at journal admission and is the only sequence used for replay".
- `makeEventId` (`:261-262`): `flows:event:${runId.length}:${runId}${sourceId.length}:${sourceId}${sourceSeq}`, which is deterministic so that "retrying the same source event must produce the same durable id".
- README guarantees (`README.md:95-123`):
  - "Two channels, one order" (durable vs lossy, one per-run `seq`).
  - Receipts are `Accepted | Duplicate | Dropped`.
  - "A retry is not a second event. Producer identity is (runId, sourceId, sourceSeq)".
  - Credentials are redacted before encoding.
  - `emitDurable(input, owner)` fails `fence_lost` if the lease moved.
  - `transact` commits state and entries together.
  - checkpoint+compact. "gaps are valid".

### 3.3 Service (`Journal.ts:474-609`)
- Writes: `emitLossy`, `emitDurable(input, owner)`, `emitDurableUnfenced`, `transact`, `whenCommitted`.
- Reads: `stream({runId, afterSequence})`, `entries` (paged), `changes` (lossy live PubSub), `project(projection, opts)`.
- Lifecycle: `flush`, `checkpoint(opts, owner)` (fenced; last writer wins per seq), `latestCheckpoint`, `compact(opts, owner)`, `generation(runId)`.
- `Projection` (`Projection.ts:19-23`): `{name, initial: S, reduce: (S, Entry) => Effect<S>}`.

### 3.4 What replay rebuilds, and what it does not
This is the key nuance. **The journal is history, not the executable state.**
- `README.md:174-181`: "Run and attempt state live in @smthrs/run-store, sealed step results in @smthrs/step-cache, and the durable deferred and clock tables in @smthrs/engine-store. Those stores hold the executable state, which is not derived from journal entries; `transact` is what keeps the two halves consistent".
- Authority table in `F/journal/docs/concepts/state-event-authority.md:12-19`: execution, attempts, deferreds and clocks belong to "Engine-owned stores"; UI summaries are "Read projections".

The engine record families (`state-event-authority.md:76-83`; `F/engine-store/src/EventTypes.ts`):
- `flows.engine.plan-recorded`, `subgraph-appended`, `node-scheduled`, `node-settled`, `node-invalidated`, `node-reconciled`
- `flows.engine.v2.attempt-lifecycle`, `flows.engine.v2.state-event`
- Settlement words are `built | clean | failed | skipped | deferred`. "`clean` means a recorded result served the node and no executor ran" (`:102-104`).

Resume semantics (`apps/site/src/content/docs/docs/concepts/durable-execution.mdx:35-41`): "the engine starts from the flow definition and looks up recorded outcomes as it reaches action boundaries. A completed action returns its stored outcome. A recorded failure returns its stored failure. Unfinished work can execute again." Code outside actions re-runs: the doc's own example is that a local counter increments again on resume.

### 3.5 Result reuse: action keys
- **Attempt table** (`F/run-store/src/migrations/0001_initial.ts:91-108`): `flows_attempts(run_id, step_key_digest, attempt, state, started_at_ms, finished_at_ms, heartbeat_at_ms, checkpoint_json, error_json, outcome_json, meta_json, PK(run_id, step_key_digest, attempt))`.
- **`actionKey`** (`F/engine/src/FlowEngine/ActionKey.ts:160-258`) has two shapes:
  - A sealed action with an `idempotencyKey` gets a `DerivedKey{kind: "run"|"cache", capabilityCeilings?, form: "declared"|"caller", input: {action, idempotencyKey, declaration: digest(success/error schemas)} | callerObject, implementationVersion?, runId|environment, nondeterministic?, boundary?: digest(readSet digests, writeSet, boundaryMode)}`. Without an environment it is run-local; with one it is a cross-run cache key.
  - Anything else (compensable, irreversible, or unkeyed) gets `StepIdentity.invocationKey({runId, parentScope, ordinal, tier})`. The ordinal comes from a counter scoped per declaration identity, "so ... a replay could [not] hand chargeCard the ordinal sendEmail recorded".
- **Guard** (`F/engine-store/src/internal/ActionPersistence.ts:957-964`): `tier === "irreversible" && attempt > 1 && no idempotencyKey` fails `IrreversibleRetryRequiresIdempotencyKey`. Attempt meta records `{tier, keyed?: true, nondeterministic?}` (`:970-974`).

---

### 4. Time travel (`@smthrs/time-travel`)

- `Frame = {lineageId: NonEmptyString, seq: int>=0}` (`F/time-travel/src/Frame.ts:34-37`). `Position = {runId, frame}`. Forks create descendant lineages, so the lineage is part of the address.
- Service (`TimeTravel.ts:212-230`): `replay(position, projection, {pageSize, maxHistoryEntries, engineEvents}?)`, `inspect(position, projection)`, `fork(position, ForkOptions?)`, `rewind(position, RewindOptions?)`.
- `ForkOptions` (`:145-166`): `workspaceRoot`, `pageSize`, `maxHistoryEntries`, `retainWorkspace`, plus:
  - `override?: StepOverride`: "Replace one step's recorded result on the child ... runs everything after the frame again".
  - `rebind?(childRunId)`: replaces the root payload. "A step whose key a new payload changes runs again; every step whose key it leaves alone replays."
- `RewindOptions` (`:178-198`): `detachedChildren: "block"|"cancel"` (default block), `wholeRepo?` (restores the jj operation).

**Semantics** (`apps/site/.../concepts/time-travel.mdx:49-71`):
- **Replay** "folds the committed journal prefix up to the frame through a pure projection ... The fold has no dispatcher, so a replay can never re-execute".
- **Fork** "mints and reserves a child run id, provisions a jj workspace pinned at the frame's recorded pointer, copies the journal prefix and the attempts that prefix can explain, and records the lineage edge". It refuses with `live_parent`. A limitation (`:47`): "copied attempt rows retain their parent digests, so actions keyed by the run ID execute again in the child."
- **Rewind** does the following in order:
  1. Claims the run and heartbeats.
  2. Writes an audit row.
  3. Assesses every effect boundary in the suffix.
  4. Compensates through registered `CompensationHandlers`.
  5. Restores the jj workspace.
  6. Archives and truncates the suffix atomically.
  7. Increments the journal generation (`journal/README.md:186-191`).
  8. Removes deferred completions and clock deadlines whose records lie in the archived suffix (`time-travel/README.md:102-105`).

  It refuses with `irreversible` when a crossed, unsealed effect has no handler.
- **Effect evidence** (`EffectBoundary.ts:22-110`): `EffectTier = sealed|compensable|irreversible`; `EffectStatus = intended|succeeded|unknown`. The record carries `{id, kind, tier, runId, lineageId, input?, cacheKey?, idempotencyKey?, compensation?, residue?, attempt?, nonce?, status, seq, output?, durableBoundary, providerStream}` under event type `flows.time-travel.effect-boundary`.
- **Recovery** is not a call: building the layer finishes or rolls back interrupted rewinds.
- Errors: `busy, live_parent, live_child, not_found, invalid, already_crossed, rate_limited, compensation_failed, irreversible, fence_lost, limit_exceeded, unknown`.
- CLI: `smthrs runs inspect|replay <id> --at <seq>`.

---

### 5. `docs/design/durable-external-work.md` (full summary)

**Header** (`:1-9`): "Durable external work: attach, don't restart".
- Status: "proposal, 2026-10-01; §3.5 approved by Will on 2026-10-02 UTC. Line references are `main` at 954a8125."
- Scope: "liveness of long external work (10-60 min coding agents in microVMs or Cloud workspaces) under host stalls, network loss, restarts and code changes, without duplicate external workers and without `smthrs runs resume`."
- Non-goals: "a second graph model; changing `@smthrs/flow` as the one flow model; who restarts a dead host process".
- Fault cases: #3367 (`packages/smithers/test/faults`).

### §1 Why each fault stopped the run

**F1. A host stall over 19 s parks the run until `runs resume`** (`:13-41`):
1. `heartbeatWriteTolerance` is stale cutoff 30 s minus skew 10 s minus interval 1 s, which is 19 s (`Consensus.ts:45-101`).
2. "The lapse is decided by the clock, not by evidence": the deadline fiber calls `expire` after 19 s without a confirmed pulse and never asks whether anyone claimed the run. On one host nobody can: `sameHostPidProbe` refuses to steal from a live pid.
3. The loop races the flow body. Every execution has its own loop, so ~24 work children, the rounds child and the root all lapse at once.
4. "Interruption destroys the external work, not only the fiber". `remoteFix` runs `Sandbox.run` inside `Effect.scoped`. Closing the scope kills the guest, removes the microVM, or DELETEs the Cloud workspace. Any later RemoteFix restarts the agent.
5. The run records `interrupt-released` (cause `lease-lapsed`) and parks with reason `released`.
6. The sweep wakes released rows, but `ControlAffinity` refuses a released child whose control ancestor is this live process, unless `canRetryReleased` finds an explicit-resume grant or a dead same-host owner. The owner is alive, so the run becomes `needs-resume`.
7. Burndown releases every open claim as `requeued: round interrupted`.

Two root causes (`:43-50`):
- "(a) A lapse is treated as a loss, even though a compare-and-swap could prove that exclusivity held."
- "(b) The external process lives in the engine fiber's scope, so re-execution means restarting it."
- "Given (b), #2982's fail-stop is correct. This design removes (a) and (b); it does not weaken #2982."

**F2. A DNS outage becomes terminal action failures** (`:52-66`):
- Dispatch retries only when an action declares `retryPolicy`, and no issue-sweep action does.
- `Fault.respond` already says `infra -> retry, then park`, but nothing on the action path consults it. `ProviderError`, `HostFailed` and `GhFailed` are unregistered, so they classify as `bug`.
- Burndown settles typed failures as `failed`, which is final for the lineage. "Run-6 lost 118 items to one 75-minute outage."
- Outage detection exists in one flow only and gives up after ~20 min.

**F3. A code change under a live run breaks it** (`:68-79`):
- `body_unavailable` (#3320): `Registry.loadBody` refuses changed live bytes. The verified module is retained only in memory. After a restart the host refuses the module (a `LaunchFailed` defect), or resume raises `CodeDrift` until someone passes `--allow-code-drift`.
- `ExecutionIdentityConflict` (run-8): joining a child admitted under a wider capability ceiling is a defect, and a member defect fails the whole round.

**F4. Fixed 30 s flow-load timeout (#3359)** (`:81-87`): `Executable.catalog` races entries against `loadTimeoutMs ?? 30_000` and records a timeout as `body_unavailable`. Nothing retries it.

### §2 Core mechanism: the two designs (`:89-126`)

Both designs need three things:
- "the job has a key outside the engine fiber";
- "creation is create-or-get at the side effect itself, which is the 'external fencing token at the side effect' that `Consensus.ts:85-90` says is needed";
- "any engine incarnation can observe the job".

**A. Attach inside one action** (like Temporal's activity heartbeat):
- The action writes its handle with `Action.checkpoint(handle)`; AttemptStore already has `checkpoint` and `heartbeat`.
- A re-executed attempt is adopted under the same number, reads the last checkpoint and re-attaches.
- "The fiber keeps waiting on the job and holds the run lease the whole time."

**B. Park on the job** (like Temporal's async completion):
- "`Start` creates or gets the job by key; its handle is journaled."
- "`Poll` runs a read-only `Status` probe, parked on a durable timer between probes."
- "`Collect` captures the work and tears down; it is idempotent."
- "A suspended run holds no lease (`RunDriver.ts:2290`), so stalls cannot lapse it, restarts resume on the timer, and any host can probe."

Comparison table (`:112-120`), verbatim:

| | A: attach in action | B: park on job |
|---|---|---|
| Host stall | Lapse, release, re-drive, re-attach (churn) | No effect while parked |
| Host restart | Adopted attempt re-attaches | Timer wakes; the probe re-attaches |
| Network loss | Stream breaks, so the action needs its own polling loop | Probe fails as `infra` and is retried; the job is unaffected |
| Cancel vs release | Interrupt finalizers must tell them apart (they cannot today) | Explicit `Cancel` step plus a reaper |
| New engine API | `checkpoint` read/write, attach flag, guard change | None (a pattern), plus a `keyed` attempt-meta bit |
| Authoring | One action body | Four functions |
| Latency, journal | Immediate, one attempt | Up to the probe interval, ~40 probes/hour |

**Recommendation: B.** "It removes lease coupling instead of recovering from it, reuses `Action`/`Poll`/`Flow.to`, and its safety does not rely on finalizers. A's only advantage, latency, does not matter for jobs that run 10-60 minutes. §3.1 is still required, because the Burndown round is an action that waits in-fiber on `Work.execute` in the parent for hours."

### §3 Changes

**3.1 Lease reconfirm by the same live owner** (`:130-153`):
- Add `Consensus.reconfirm(runId, owner, nowMs): Renewed | Lost`. It is a compare-and-swap: "when `owner = me AND claim IS NULL`, set `heartbeat_at_ms = nowMs`".
- In `heartbeatLoop`, `expire` first calls `reconfirm`, bounded by one `heartbeatInterval`:
  - `Renewed`: reset the pulse time, call `onReconfirm(ms)`, and continue.
  - `Lost`, an error, or a timeout: today's `onLapse`, then interrupt.
- The driver journals `run-decision lease-reconfirmed {unconfirmedMs}`. `runs show` reports it as a warning, not `needs-resume`.
- Why `claim IS NULL`: "`steal` writes the claim before the thief activates ... In that window an owner-only heartbeat would still succeed."
- Safety: "exclusivity is a property of the lease row, not of the clock."
  - If reconfirm commits, no other owner was activated, and none can be now, because steal needs a stale heartbeat.
  - If a steal commits first, reconfirm fails and the owner interrupts as today.
  - "The 19 s tolerance still bounds non-durable overlap when a peer on another host really did steal."
- "No generation bump. The fence is owner identity (`guard`) ... The journal decision is the audit record."

**3.2 `@smthrs/flow/ExternalJob`** (`:155-190`). API sketch, verbatim:
```ts
export const RemoteFix = ExternalJob.make("issue-sweep/remote-fix", {
  payload: RemoteFixPayload, handle: JobHandle, success: Remoted, error: AgentFailed,
  probe: { every: "15 seconds", max: "2 minutes" },  // exponential; parked between probes
  timeout: "2 hours",                                 // from the journaled start, across restarts
  restarts: 1                                         // new generation only after the last one is proven over
})
RemoteFix.toLayer({
  start:   (payload, key) => Effect<Handle, AgentFailed | Unreachable>,   // create-or-get by key
  status:  (handle, key) => Effect<Running | Exited | Lost, Unreachable>, // read-only
  collect: (handle, key, exited) => Effect<Remoted, AgentFailed | Unreachable | ExternalJob.Again>,
  cancel:  (handle, key) => Effect<void>                                  // idempotent
})
```
- **Key**: "The engine derives `key = <job execution id>#g<generation>` from durable identity, never from the payload. A replay rejoins its job, and two sweeps never share one."
- **Start**: "An action with `idempotencyKey: key` and `tier: "irreversible"`. The engine already refuses to repeat unkeyed irreversible actions ... The provider guarantees at most one process per key."
- **Status and Collect** are keyed actions; `infra` failures retry (§3.4) "without touching the job".
- **Outcomes**:
  - `Exited`: run Collect.
  - `Lost` ("no exit record and no live process or machine"), or Collect returning `Again`: if g < `restarts`, `Cancel(key_g)` then `Flow.to` g+1 after a backoff; otherwise fail `ExternalJobLost` (class `infra`).
  - Timeout: `Cancel`, then `ExternalJobTimedOut` (class `dependency`).
- **Cancellation**: `Cancel` is registered with `Flow.withRollback` for the in-process case. "The durable backstop is the reaper and the Cloud client lease (§3.3), because rollbacks do not survive a restart."

**3.3 `Sandbox.job` and retained machines** (`:192-220`). `Sandbox.job(provider, {command, files, capture})` implements the four functions over any `Session`:
- **start**: acquire the retained machine for the key, then `mkdir /var/lib/smthrs-jobs/<slug>`. "The mkdir is atomic; `EEXIST` means the job already started, so return its handle." Otherwise write the files and run `setsid sh -c '<cmd>; echo $? >exit.tmp && mv exit.tmp exit' </dev/null >out 2>err & echo $! >pid`. "The launcher returns at once, so the spawn scope never holds the job."
- **status**: an `exit` file means Exited; a live process group (`kill -0 -<pgid>`) means Running; "Anything else, including a missing machine, means Lost."
- **collect**: read out, err and exit, run `Work.capture`, destroy the machine.
- **cancel**: `kill -TERM -<pgid>`, wait 2 s, `-KILL`, destroy the machine.
- The job dir sits outside the workdir (never captured) and outside `pidDirectory`, which every acquire wipes.
- Retained machines:
  - Microsandbox already has `persistence:"sticky"` plus `detached`, which survives host death.
  - CloudSandbox gains `sticky` (skip the DELETE finalizer). "Each probe renews the backend `client_lease_seconds` lease from #2457."
  - `Provider` gains an optional `destroy(session)`.
- Reaping: machines carry the label `smithers.execution=<id>`. `MicrosandboxSandbox.reap`'s `retain(labels)` keeps a machine while its execution is non-terminal. Cloud workspaces of dead executions are cleaned up when their client lease lapses.

**3.4 Fault classification and bounded retry** (`:222-240`):
- Register `ProviderError` (`unavailable`/`timeout` are `infra`; `spawn_error`/`not_found` are `bug`).
- Add a new kernel `Unreachable` (lifted from `isNetworkOutage`, plus EAI_AGAIN, ENOTFOUND, HTTP 429/5xx) as `infra`, with a `classifyExit(stderr)` helper.
- Dispatch: with no declared policy, retry under `RetryPolicy.transient` ("5 s doubling to 5 min, expiring after 2 h") only if both hold:
  - the class is `infra`;
  - the action is "repeat-safe (has an `idempotencyKey`, or declares `effects.writes: []`)".

  "Otherwise there is no retry, and the failure keeps its class."
- `Fault.retryTransient(effect)` covers plain effects.
- Burndown: an `infra` failure becomes `requeued`, up to `maxRequeues` (default 3, counted in rows), then `failed`.

**3.5 #2982 guard: consent only for unkeyed in-flight effects (APPROVED)** (`:242-253`):
- "`AttemptMeta` records `keyed: true` when the action has an idempotency key."
- "`canRetryReleased` ... also returns true when the released execution has no `running` attempt without `keyed`."
- "**Approved by Will on 2026-10-02 UTC: "Approve §3.5".** This narrows #2982's "explicit resume" to releases whose unfinished effect could run twice. Legacy metadata without `keyed: true` remains unkeyed. Owner death never substitutes for explicit consent to retry an unfinished unkeyed effect. The consent policy is checked again inside the native activation transaction, so a new release or rewind generation after eligibility cannot spend old consent. Implementation: #3409."

**3.6 Per-child isolation in Burndown** (`:255-263`):
- A member defect fails only that item (with `Fault.of(defect)` in the detail); siblings keep running.
- A typed `Burndown.Stop` is the only way to stop a round; `Effect.die(WorkspaceFailed)` becomes `Burndown.Stop`.
- Optional: `ExecutionIdentityConflict` becomes a typed failure. A fresh id is allowed only when the old execution is terminal: "a fresh id beside a live child would start a second worker."

**3.7 Flow load** (`:265-271`):
- The started or resumed flow loads through the direct path with no deadline, logging progress every 30 s.
- Catalog timeouts get the new code `load_timeout`, retried on first use. `SMITHERS_FLOW_LOAD_TIMEOUT_MS` sets the deadline, and the error names whichever limit fired.

**3.8 Pinning by execution digest across restarts** (`:273-284`):
- At first admission, store the verified module closure in `ArtifactStore` with the manifest `{executionDigest, entry, modules: path -> digest, lockfileDigest}`.
- On digest drift with a manifest present, load the pinned bytes instead of refusing.
- `--allow-code-drift` still adopts new code on purpose; a missing manifest or changed lockfile still raises `CodeDrift`.
- Workspace packages are not pinned ("Run hosts from a pinned checkout"). Manifests of non-terminal runs are gc roots.

### §4 #2982 guarantees (`:286-295`)

| Guarantee | Status | How |
|---|---|---|
| No unrequested second external worker | Kept | "Reconfirm never releases a run nobody else took. `Start` is create-or-get by key at the side effect (atomic mkdir in the guest, stable Cloud name)... Generation g+1 starts only after g is proven exited or lost." |
| Consent binds the release and the journal generation | Kept | Grant schema unchanged |
| Background wake or approval is not consent | Kept | Unchanged |
| Cancellation and dead-owner recovery | Kept | `ExternalJob` adds `Cancel` and the reaper |
| Unknown cross-machine liveness needs explicit resume | **Narrowed (approved 2026-10-02)** | Still required when any in-flight attempt is unkeyed |
| ClaimLost, heartbeat guards, fenced writes | Kept | Reconfirm is a compare-and-swap in the same Consensus |

### §5 issue-sweep migration (`:297-322`)
1. After 3.1/3.4/3.6/3.7:
   - `WorkspaceFailed` becomes `Burndown.Stop`.
   - Supply Burndown's `cancelled` member.
   - Declare `effects:{writes:[]}` on the read actions.
   - Replace `isNetworkOutage` with `Unreachable` plus `Fault.retryTransient`.
2. After 3.2/3.3/3.5, RemoteFix becomes an `ExternalJob` with the same `Remoted` success:
   - **start**: reserve the account and record it in the handle. The reservation lives in host memory, so re-attaching must re-mark it. Acquire the sticky machine under the job key, write login and brief, run `Sandbox.job`.
   - **collect**: capture `Work`, copy the refreshed login back, cool the account, destroy the machine. "A network signature or quota exit returns `Again`."
   - **vm.ts**: `maxVms` counts live job machines by label, not scope permits.
   - **Session key**: changes to the job key.
3. Cut over on a new attempt, running from the pinned checkout.
4. Local placement (`Fix`) stays in-fiber and does not survive restarts.

### §6 Order and tests (`:324-371`)
Each step lands alone and flips one #3367 fault case:
1. Reconfirm: TestClock cases (untouched lease reconfirms; a pending claim, foreign owner or write timeout interrupts); a reconfirm-vs-steal race; a 25 s SIGSTOP giving exactly one spawn and `lease-reconfirmed`.
2. Isolation and Stop.
3. Faults: "a 10-minute resolver blackhole produces zero `failed` rows".
4. Load.
5. Pinning.
6. Sandbox.job: "Spike first: does a `setsid` child outlive the end of a Microsandbox exec?" Concurrent starts produce one process; the job survives its scope and a host SIGKILL.
7. ExternalJob and guard: "Restart mid-poll: start runs exactly once"; Lost and Again each move to g2; timeout and cancel both call `Cancel`; a released execution with only keyed attempts is re-driven without a grant.
8. Live canary: 24 items, a 40 s STOP, a host SIGKILL and a 10-min DNS blackhole. "Expect Codex sessions == items and zero `runs resume`."

### §7 Risks (`:373-379`)
- Cloud's idle timeout may suspend a workspace that is running a job.
- "The probes add journal rows (24 jobs x ~40/hour). Measure with gc and compaction."
- "A `keyed` flag is only as honest as the key behind it ... hand-written keyed actions that are not actually idempotent will now be re-driven after a release."

### Open questions in the doc
The doc has no formal open-questions section. Its open items are:
- the `setsid` spike;
- the Cloud idle-timeout fix (set idle ≥ job timeout, or make lease renewal count as activity);
- probe journal volume.

---

### 6. Other durable-wait primitives

**The rule**: a suspended run holds no lease (design doc §2B). Only `running` rows carry an owner and heartbeat, enforced by a CHECK on `flows_runs` (`F/run-store/src/migrations/0001_initial.ts:20-85`).
- That table also holds `waiting_reason`, `waiting_wake_at_ms`, `waiting_token`, `cancel_requested_at_ms`, `state_json`, with status in `pending|running|suspended|completed|failed|cancelled`.
- Partial index: `(waiting_reason, waiting_wake_at_ms) WHERE waiting_reason IS NOT NULL`.

**Engine-store tables** (`F/engine-store/src/migrations/0001_initial.ts:22-47`):
- `flows_deferred_completions(flow_name, execution_id, deferred_name, exit_json, metadata_json, completed_at_ms, PK(flow,exec,name))`
- `flows_clock_deadlines(flow_name, execution_id, clock_name, deferred_name, due_at_ms, completed_at_ms?, PK(flow,exec,clock))` with a pending index on `(completed_at_ms, due_at_ms)`.

**Semantics** (`F/engine-store/docs/concepts/durable-waits.md`):
- A deferred is "completed once, first writer wins". A clock carries an absolute `dueAtMs`, and scheduling is "first writer wins and fenced against the active run owner".
- Outcome unions (`:26-32`): `completeDeferred → Completed|Existing`, `scheduleClock → Scheduled|Existing`, `park → Parked|NotFound`, `wake → Woken|NotWaiting|NotFound`.
- Waiting taxonomy (`:44-52`): `timer` (with the earliest deadline as `wakeAt`), `event`, `released`, `quarantine`.
- Three sweeps on the heartbeat cadence (`:63-75`): cancel-requested parked runs; `released` runs; stale `running` rows (`staleRunningRuns`).
- Registration re-arms `pendingClocks` and recovers unconsumed `completedDeferreds`. A deferred result is marked consumed before it is served.
- A clock that fails to fire retries with backoff (100 ms doubling to 30 s, forever).
- `WakeBus` is in-process and edge-triggered, explicitly not durable: "A wake with no waiters is dropped, and the polling fallback covers the run."

**Flow-level primitives** (`F/flow/src/`). All are plain keyed actions, i.e. visible plan nodes:
- `Sleep.ts:1-22`: "ship it over the existing DurableClock, so a wait is a visible, keyed plan node". The park is annotated `timer`, and the clock identity is inherited from the dispatch's `CurrentInvocationKey`.
- `WaitFor.ts:1-18`: an external signal over `DurableDeferred`, parked as `event` with a token.
- `HumanTask.ts:1-34`: kinds `ask|confirm|select|json`, validation, and re-asking on a new wait point `WaitFor/<name>#<attempt>`. Rejections are recorded as a sealed step `HumanTask/<name>#<attempt>/rejected`. The park is `approval`; the deadline is a `DurableClock` raced against all attempts.
- `Poll.ts:1-27`: "one attempt per round, a durable wait between attempts" via `Sleep` + `Flow.to`. `maxAttempts` doubles as `maxRounds`.

**Lease constants** (`F/journal/src/Consensus.ts:~44-98`):
- `heartbeatInterval` 1 s, `heartbeatStaleAfter` 30 s, `heartbeatSkewAllowance` 10 s, `heartbeatWriteTolerance` 19 s.
- The constants' comment: "a caller that cannot tolerate any overlap needs an external fencing token at the side effect itself, not a larger timeout."
- `reconfirm` is now in the interface (`:434-438`): "Renews only while this owner holds the lease and no takeover claim is pending."

---

### 7. Implementation status vs. the design doc

**`F/flow/src/ExternalJob.ts` (431 lines) exists.**
- Status union `Running | Exited{exitCode} | Lost` (`:28-32`). Errors `Again`, `ExternalJobLost`, `ExternalJobTimedOut`, registered as `infra` and `dependency` (`:73-74`).
- `Operations{start, status, collect, cancel}` (`:91-96`).
- Key is `keyOf = ${origin}#g${generation}` (`:150`); `origin` is the executionId stamped by `Initialize`, which also journals `startedAt`, so the timeout is anchored across restarts.
- The actions:

  | Action | Line | Tier / key |
  |---|---|---|
  | `Start` | `:187` | `tier:"irreversible", idempotencyKey: keyOf` |
  | `Probe` | `:194` | `tier:"sealed"`, `effects.writes:[]`, key `<key>/probe/<n>` |
  | `Collect` | `:209` | `irreversible`, `<key>/collect` |
  | `Cancel` | `:216` | `irreversible`, `<key>/cancel` |
  | `Ready` | `:227` | refuses a relaunch past the deadline |

- The observe loop is `Probe`, then `Sleep.action.call({until: wakeAt})`, then `observe.to(next)` (`:303`).
- Backoff is `min(max, every*2^(probe-1))`, capped at the deadline (`:373`).
- `recover` runs Cancel, then Sleep, then `launch.to(g+1)` if `generation <= restarts`, else Fail (`:272-279`). The doc comment at `:100-106` says "`restarts` counts replacement generations; zero permits only g1".
- `Flow.withRollback` cancels on a non-interrupt failure (`:179`).

**Also landed:**
- `F/sandbox/src/Sandbox/job.ts`: atomic `mkdir`, `setsid`, `exit.tmp → exit`, `kill -0 -pgid`, a TERM, sleep 2, KILL cancel, and `JobHandle{id, remoteId, workdir, directory}`.
- `P/src/internal/ReleasedChildResume.ts:173-221`: the keyed check, re-validated inside a transaction against the latest release `eventId`/`generation`.
- `lease-reconfirmed` at `F/engine-store/src/internal/RunDriver.ts:2105`.

## Appendix D — CLI / MCP / skill surface, builder, code quality

### Smithers research report: 0.x (v0.35.0) vs main (1.0.0-rc.1)

Path roots used below:
- `0x/` = `/homes/nessa/zhanghao/dev/Eleforge/forgeflow/context/repos/smithers-0.x`
- `main/` = `/homes/nessa/zhanghao/dev/Eleforge/forgeflow/context/repos/smithers-main`

Clone heads: 0.x is at `584f479` (2026-08-17). main is at `96aed3b0` (2026-10-02).

### 0. Licensing (verified)

- **0.x:**
  - `0x/LICENSE:1-3` is MIT, "Copyright (c) 2025 William Cory".
  - There is no THIRD_PARTY_NOTICES file.
  - The bundled `packages/jj-*` binaries are **Apache-2.0** (`0x/packages/jj-linux-x64/package.json:5`).
- **main:**
  - `main/LICENSE` is MIT, "Copyright (c) 2026 William Cory and the Smithers Flows contributors".
  - `main/THIRD_PARTY_NOTICES.md` (86 lines) lists:
    - `@smthrs/engine` "is a fork of Effect's unstable durable-flow runtime" (MIT, line 8+).
    - jj-lib 0.44 (Apache-2.0), statically linked into `flows_jj.wasm` from the fork `smithersai/jj` (lines 53-78).
    - Git 2.50.1 (GPL-2.0), shipped in Docker/macOS distributions only (lines 80-86).
  - **Caveat:** `main/crates/smithers-ffi/Cargo.toml:6` reads `license = "AGPL-3.0-or-later"`. This is the native C ABI for repo/jj ops, and it is required by the README install step (`cargo build ... -p smithers-ffi --bin smithers-jj-export`). `crates/flows-jj` is MIT.
  - So **main is not purely MIT**. Avoid copying from `crates/smithers-ffi`.

---

### 1. 0.x CLI (`0x/apps/cli/src/index.js`, 14,503 lines, single file)

**Framework.** The CLI is built on `incur` (`import { Cli, SyncSkills, z } from "incur"`, `index.js` imports; `apps/cli/package.json:67` `"incur": "^0.4.10"`).
- Root: `Cli.create({ name:"smithers", description: CLI_DESCRIPTION, version, mcp:{command:"bunx smthrs --mcp"} })` at `index.js:8729-8737`.
- Nine sub-CLIs are mounted at `index.js:13486-13494`: workflow, claude, cron, agents, memory, openapi, token, worktree, herdr.
- incur also provides the built-ins `completions`, `mcp add`, `skills add|list`, and `--llms`, `--schema`, `--mcp` (`index.js:13608` `KNOWN_COMMANDS = [...cliCommands.keys(), "completions","mcp","skills"]`).
- The docs catalog `0x/docs/cli/overview.mdx:265+` declares `commands[114]` in TOON.

### Command catalog (`index.js` line → purpose)

**Top level**

| Line | Command | Purpose |
|---|---|---|
| 8741 | `init [prompt]` | Install the `.smithers/` pack. With a prompt, also launch create-workflow. Flags: `--agent`, `--tutorial`, `--force`, `--agents-only`, `--install`, `--skill` (install curated skill + append to CLAUDE.md/AGENTS.md), `--global`, `--yes`. |
| 8792 | `oneshot` | Run one well-scoped goal with a strong agent in the background, with optional review and a live UI. |
| 9272 / 9288 / 9301 / 9312 | `add` / `remove` / `eject` / `share` | Workflow packs. `packs list\|update` at 9337/9343. |
| 9370 | `make-workflow [task]` | Dispatch to the create-workflow builder (see §4). |
| 9423 | `starters` | Plain-English starter workflows with copy-paste commands. |
| 9435 | `hermes` | Alias for `mcp add --agent hermes`. |
| 9460 | `up [workflow]` | Start an execution; `-d` detach. Key options at `index.js:2935-3000`: `--detach`, `--run-id`, `--resume`, `--force`, `--steal-ownership`, `--input <json\|->`, `--annotations`, `--max-concurrency`, `--root`, `--log/--log-dir`, `--allow-network`, `--max-output-bytes`, `--tool-timeout-ms`, `--hot`, `--serve --port 7331 --host`, `--supervise*`, `--backend`, `--interactive`, `--herdr`, `--started-by-*`. |
| 9532 | `migrate` | bun:sqlite to PGlite/Postgres. |
| 9594 | `bug` | File a bug report; `--run` attaches status, error, and recent events. |
| 9610 | `review` | Code review plus HTML walkthrough. |
| 9629 | `listeners` | GitHub webhook declarations. |
| 9659 | `gateway [status\|stop]` | Multi-run RPC/WS control plane (singleton per workspace). |
| 9692 / 9852 | `eval` / `optimize` | JSONL eval suite; GEPA prompt optimization. |
| 9874 / 10083 / 10109 | `supervise` / `supervisor` / `top` | Auto-resume stale runs; live outline TUI. |
| 9976 | `ps` | List active, paused, and recent runs. |
| 10057 / 10070 / 10136 | `logs` / `tail` / `events` | Event log follow; node output; lifecycle history (`--raw`). |
| 10338 / 10585 / 10681 / 10719 | `chat` / `chat-create` / `hijack` / `steer` | Agent transcript; one-task chat run; hand off a live agent session; durable steer message. |
| 10732 / 10804 / 10922 / 10973 / 11013 | `inspect` / `node` / `why` / `status` / `what` | Run state + tokens; node detail; blocker diagnosis; health verdict; LLM narration. |
| 11073 / 11214 / 11341 | `human` / `ask-human` / `alerts` | Human inbox/answer/cancel; blocking request from inside a run; alerts. |
| 11450 / 11594 / 11529 | `approve` / `deny` / `signal` | Gates (`--node`, `--by`, `--note`, `--watch`); durable signal (`<run> <name> --data --correlation`). |
| 11664 / 11787 / 11851 | `cancel` / `pause` / `down` | Halt; graceful park; cancel all. |
| 11927 | `graph` | Render the graph without executing (pre-flight). |
| 12019-12129 | `snapshots`, `restore`, `snapshot-hook` (internal), `revert`, `retry-task` | Checkpoints and retry. |
| 12288, 12530, 12627-12935 | `timetravel`, `replay`, `tree`, `diff`, `output`, `rewind`, `fork`, `timeline` | Time travel / devtools. |
| 12394 / 12453 / 12491 | `observability` / `ask` / `scores` | Observability stack; doc Q&A; scorer results. |
| 12976 / 13038 / 13096 | `gui` / `ui` / `monitor` | Browser UIs; autostart the gateway. |
| 13143 / 13156 | `docs` / `docs-full` | Print llms.txt / llms-full.txt. |
| 13173 / 13215 / 13325 / 13393 / 13471 | `upgrade` / `update` / `usage` / `gc` / `claude-shell` | Maintenance. |

**Groups**
- `workflow` (6417): `run` (6421, alias of `up` by id), `list`, `path`, `create` (scaffold), `inspect`, `skills` (generate agent skill docs per workflow), `doctor`.
- `claude` (6917), the Claude Code mirror protocol:
  - `tick` (one frame; `--wait`, `--after-seq`)
  - `node-wait` (block until a node is terminal; `timedOut:true` means re-invoke)
  - `monitor` (NDJSON actionable transitions)
  - `subscribe` / `unsubscribe`
- `cron` (start/add/list/rm), `agents` (capabilities/doctor/add/list/reauth/remove/test), `memory` (list/get/set/rm), `openapi` (list/generate), `token` (issue/exec/revoke), `worktree` (list/prune), `herdr` (status/attach/open/clean).

### Output envelope for agents

Global options (`overview.mdx:14`): `--format toon|json|yaml|md|jsonl`, `--filter-output <key.path>`, `--full-output`, `--token-count/--token-limit/--token-offset`, `--schema`, `--llms`, `--llms-full`, `--mcp`. `--json` is shorthand for `--format json` (`index.js:8651-8653`).

`overview.mdx:67-75` quoted:
> "When output is piped or captured (any non-TTY consumer, such as an AI agent), each command instead emits a single TOON envelope: the command's `data`, plus a "Next steps" `cta` block that suggests follow-up commands."

```toon
commands[2]{command,description}:
  bunx smthrs logs run-abc123,Tail active run
  bunx smthrs inspect run-abc123,Inspect most recent run
```

> "The runnable shell command is the first field, up to the comma ... Never copy-paste a whole row into a shell ... Pass `--format json` to get the same `cta` as a nested object with explicit `command` and `description` keys."

**Producing it:**
- Handlers call `c.ok(data, { cta: { description, commands:[{command,description}] } })` (e.g. `index.js:1470`) or `c.error({code,message,exitCode})` via `makeFail` (`index.js:8636-8641`). There are 157 `c.ok(` sites.
- CTA content is centralised in `0x/apps/cli/src/agentNextSteps.js:96-171` (`buildAgentNextSteps`). The agent variant returns a script: `"Suggest to the user:\n1. ... build a custom UI ...\n2. Visualize ... graph/tree ...\n3. Ask the user clarifying questions ... route it by size: oneshot vs workflow run create-workflow"`. A shorter human variant is `buildHumanNextSteps` (`:31-57`).
- Type: `WorkflowCta.ts` = `{command, description}`.
- Agent detection: `util/envDetect.js:50` `isAgentHarness = env.CLAUDECODE || env.CLAUDE_CODE_ENTRYPOINT`.
- Detached launches also return a `monitoring` block, with options for the user to choose from (`overview.mdx:199-208`).

**Exit codes (inconsistent in 0.x):**
- Docs (`overview.mdx:55-64`): `0` success, `1` execution failure, `2` run cancelled, `3` `up` ended in waiting-approval/event/timer, `4` invalid args, `130` SIGINT, `143` SIGTERM.
- `eval` exits `5` for INCONCLUSIVE (SKILL.md repair-loop section).
- But `util/CliExitCode.ts:1-10` and `util/exitCodes.js` define `0 ok, 1 user error, 2 server error, 3 declined, 130 sigint` "for the devtools live-run commands".
- Two vocabularies coexist.

---

### 2. 0.x MCP server (`0x/apps/cli/src/mcp/`)

**Entry and surfaces.**
- `mcp-mode.js` detects `--mcp`. `--surface semantic|raw|both` defaults to `semantic` (`argv-utils.js:42-43`). It also takes `--allowed-tools a,b` and `--read-only` (keeps only `readOnlyHint:true` tools; `semantic-server.js`).
- The `raw` surface mirrors every incur command as a tool.

**Result envelope** (`semantic-tools.js:970-1001`):
```js
{ ok: true, data }                                      // structuredContent + JSON text block
{ ok: false, error: { code, message, details, docsUrl } } // isError: true
```

**The 21 semantic tools** (`SEMANTIC_TOOL_NAMES`, `semantic-tools.js:42-64`; definitions at 1124-2041):

| Tool | RO | Params / semantics |
|---|---|---|
| `list_workflows` | ✓ | `includeSystem`. Local `.smithers` shadows the global pack. |
| `run_workflow` | ✗ | `workflowId, input{}, prompt, runId, resume, force, stealOwnership, waitForTerminal(false), waitForStartMs(1000), maxConcurrency, rootDir, logDir, allowNetwork, maxOutputBytes, toolTimeoutMs, hot, startedBy{harness,sessionId,prompt}` (`:289-330`). Background by default; returns a `monitoring` block. |
| `list_runs` | ✓ | `limit(20, ≤200), status` |
| `get_run` | ✓ | `runId`. Steps, approvals, timers, lineage, config. |
| `watch_run` | ✓ | `runId, intervalMs(1000), timeoutMs(30000)`; max 1h. Polls until terminal. |
| `explain_run` | ✓ | `runId`. Why waiting/stale/blocked (diagnosis model). |
| `list_pending_approvals` | ✓ | `runId?, workflowName?, nodeId?` |
| `resolve_approval` | ✗ | `action: approve\|deny, runId?, workflowName?, nodeId?, iteration?, note?, decidedBy?, decision?`. Returns an ambiguity error rather than guessing (`:411-425`). |
| `ask_human` | ✗ | `prompt, context?, choices[]?, runId?, nodeId?, iteration?, timeoutSeconds?, pollSeconds?`. Blocks; `approved` or `blocked` ("on 'blocked' you must not proceed"). |
| `get_node_detail` | ✓ | `runId, nodeId, iteration?`. Attempts, tool calls, tokens, scorers, output. |
| `revert_attempt` | ✗ | Destructive workspace + frame revert. |
| `fork_run` / `replay_run` | ✗ | Branch from a checkpoint; replay optionally restores VCS. |
| `rewind_run` / `restore_checkpoint` / `time_travel` | ✗ | Destructive; `confirm=true` required (time_travel also needs `force` if running). |
| `list_snapshots`, `get_timeline`, `list_artifacts`, `get_chat_transcript` | ✓ | Read-only inspection. |
| `get_run_events` | ✓ | `runId, afterSeq?, limit(200, ≤10000), nodeId?, types[]?, sinceTimestampMs?` |

**Other MCP surfaces.**
- `packages/pi-plugin/src/extension.ts` spawns `smithers --mcp` and re-registers each tool as `smithers_*`. It also registers the slash commands `/smithers`, `/smithers-docs|watch|runs|approve|cancel|run` (`:588-724`).
- `apps/cli/src/openclaw-plugin/` and `hermes-plugin/` (Python) are native wrappers.

---

### 3. Skills / plugins / hooks (0.x)

### Files

| File | Lines | Notes |
|---|---|---|
| `0x/skills/smithers/SKILL.md` | 1,200 | Canonical curated skill. |
| `0x/skills/smithers/llms-full.txt` | 24,306 | Full docs, shipped next to the skill. |
| `0x/claude-plugin/skills/smithers/SKILL.md` | 492 | Claude variant, "(from Claude Code)". |
| `0x/codex-plugin/skills/smithers/SKILL.md` | 392 | Codex variant. |

Plus several side skills: context-engineer, eval-writer, prompt-author, schema-author, risk-reviewer, report-maker, smithers-fallback-agents, train-kimi-smithers.

### Frontmatter

`SKILL.md:1-24` has `name: smithers` and a long `description:` that embeds the hard rules ("YOU (the agent) run Smithers ... HARD RULE 0: if `SMITHERS_INSIDE_RUN` is set ... never invoke the Smithers CLI ... You are otherwise an ORCHESTRATOR").

The Claude variant's description adds:
- HARD RULE 1: right-size the route.
- HARD RULE 2: no Task/Agent/`/loop`; the native Workflow tool is only for the `smithers-run.mjs` mirror.
- HARD RULE 3: "ALWAYS use https://smithers.sh/llms-full.txt as the API reference (fetch it first)" (`claude-plugin/skills/smithers/SKILL.md:1-5`).

### Section map (`skills/smithers/SKILL.md`)

Rule 0 (34) · Route first (56) · Launch attribution (99) · You drive it (109) · Do it, don't describe it (129) · Gateway is the control plane (159) · Orchestrator-only (185) · Plan mode with muscle (226) · How to guide the user (239) · Reports are HTML (294) · Reusable procedures → workflows (321) · 60s to aha (377) · Mental model (448) · Context engineering (495) · `.smithers/` folder (615) · Operating runs (700) · Ask a human, never guess (738) · Smithers vs answering (781) · Repair-loop discipline (799) · oneshot (824) · Examples (929) · Authoring (1049) · Workflow + tests indivisible (1080) · Custom UIs (1112) · Full reference (1164).

### Key instructions (quoted)

- **Inside a run (:34-52):** "If `SMITHERS_INSIDE_RUN` is set ... you ARE a worker agent ... Never launch or steer a run from inside a node ... The one exception is escalating upward: `smithers ask-human`."
- **Routing (:56-97):** Four tiers: ambiguous → "reply ONLY with clarifying questions"; trivial → do it directly; "Clear single-goal ask, at ANY size → `smithers oneshot`"; "Genuinely multi-goal shape → build and run a full workflow". Line 88: "Size does not pick the route; shape does."
- **Agent runs the commands (:122):** "**you run every Smithers command yourself. Never instruct the human to run a Smithers command** ... relay the question in plain language, collect their decision ..., and run the resolving command (`approve`, `deny`, `human answer`, `signal`) yourself."
- **No narrating (:129-157):** "Do it - don't describe it ... Describing the work is not the work."
- **No direct DB access (:169):** "Never import `openSmithersStore` ... query `_smithers_*` tables ... parse the Gateway runtime state file."
- **No own subagents (:191):** "**do NOT spawn your own subagents (the Task tool ...) to do the work. Run a Smithers workflow instead.** ... Subagents are for monitoring, never for the background work."
- **CTA handling (:244):** "**Act on the CLI's next steps.** Nearly every `smithers` command ends with a "Next steps" (cta) block ... Never silently drop it: run the obvious continuation yourself, and relay the other options to the user." It also says "Ask before you build, then guide step by step", "Proactively offer to visualize", and "never pass `--interactive` to a command you execute".
- **Repair loops (:804+):** A "Same-signature budget" of 3 consecutive same-failure rounds, then change strategy or ask-human. Also "Green ratchet", "Never widen a red gate", "Classify red before repairing" (env fault → `eval` exit 5 INCONCLUSIVE).
- **Tests (:1080):** "A workflow and its tests are one indivisible change." It requires a `renderWorkflow` test registered in `.smithers/package.json` with at least 4 assertions (node ids, order, outputSchema, branches/loops).
- **Docs (:1164+):** Progressive docs disclosure via `smithers docs` / `docs-full` / `ask`; "When in doubt, clone the repo."

### claude-plugin (`0x/claude-plugin/`)

- `.claude-plugin/plugin.json`: v0.2.5, with `experimental.monitors`.
- `.mcp.json` runs `node ${CLAUDE_PLUGIN_ROOT}/bin/smithers.mjs --mcp`. `bin/` and `lib/resolve-smithers-cli.mjs` resolve a local checkout CLI first.
- `hooks/hooks.json`:
  - `SessionStart` → `session-start.mjs` (132 lines). It detects `.smithers/`, lists workflows, and flags ones missing a UI via `additionalContext`.
  - `PreToolUse` matcher `Task|Agent|Workflow` → `prefer-smithers.mjs`. It is advisory only and emits `hookSpecificOutput` with "Smithers reminder: you are about to use the native ${toolName} tool ... prefer `smithers oneshot` ... prefer a durable Smithers workflow ... If this call is a short one-off lookup, ignore this" (`prefer-smithers.mjs:35-45`).
- `monitors/monitors.json`: `smithers-runs` runs `smithers claude monitor`. It streams NDJSON actionable transitions for runs this session subscribed to, and asks Claude to "relay every line to the user".
- `workflows/smithers-run.mjs` (405 lines): a native Claude Code Workflow script (`meta.name 'smithers-run'`).
  - It is a generic `/workflows` mirror: args `{runId,cwd}` or `{workflow,input,...}`.
  - It loops on `smithers claude tick --wait` / `claude node-wait` (contract v1, `TICK_TIMEOUT_MS=420000` "< the 10-minute Bash cap").

### codex-plugin

- `.codex-plugin/plugin.json` declares skills + mcpServers (`bunx smthrs --mcp`) and a SessionStart hook.
- `scripts/configure-codex-routing.mjs` rewrites Codex `features.multi_agent_v2.*_hint_text` to steer the spawn tool toward Smithers MCP (`HINT_TEXT` at `:11`).

### What `init` and `mcp add` install

`init` (`init-command.js`, `init/installAgentIntegration.js`):
- It scaffolds `.smithers/` from `apps/cli/templates/init-pack` (18 files: `agents/*.ts.tmpl` per harness, `smithers.config.ts`, `preload.ts`, `gateway.ts`, `tsconfig`, `bunfig`, `skills/`, `tickets/`, `workflows/`).
- It adds the seeded pack (`seeded-workflow-pack.generated.js`, which includes create-workflow) and runs `bun install` in the pack.
- Agent integration per agent:
  - Claude: the marketplace plugin `smithers@smithersai` (`installAgentIntegration.js` `CLAUDE_PLUGIN_SPEC`), with the curated skill as fallback.
  - Hermes/OpenClaw: native plugin + MCP config.
  - Others: the curated skill (SKILL.md + llms-full.txt) copied to `~/.claude/skills`, `~/.codex/skills`, `~/.pi/agent/skills`, `~/.config/opencode/skills`, `~/.config/agents/skills` (kimi/amp), `~/.gemini/skills` (`installCuratedSkill.js:49-92,181-184`).
- It appends a marked block "## Smithers workflows" to existing `CLAUDE.md`/`AGENTS.md` (`noteWorkflowPreferenceInAgentDocs.js:26-44`): "Use your best judgment ... Prefer a smithers workflow for multi-step plans ... offer to turn the session into a reusable smithers workflow".

`mcp add` / `skills add`:
- These are incur built-ins. `mcp add --agent claude-code|cursor [-c cmd] [--no-global]` registers `bunx smthrs --mcp`. `skills add` syncs incur-generated per-command skills plus the curated skill.
- `agent-wiring/` covers what incur can't reach: Hermes YAML, OpenClaw JSON, and Pi skills dir (`index.js:14366-14396`, `agent-wiring/README.md`).

---

### 4. The workflow builder: `make-workflow` → `create-workflow`

- **It is itself a Smithers workflow.** `make-workflow` (`index.js:9370-9417`) just does `resolveWorkflow("create-workflow")` then `executeUpCommand(...)` with `prompt = task`. If the pack is missing, the error says "run `smithers init` first" (exit 4).
- **Workflow source:** `0x/.smithers/workflows/create-workflow.tsx` (609 lines), header "Build a new Smithers workflow from a plain-English ask — clarify, provision docs & skills, design, scaffold, verify, and document."
- **UI:** `.smithers/ui/create-workflow.tsx` (1,485 lines).
- **Prompts** (MDX components with props), in `.smithers/prompts/`: `create-workflow-{clarify 110, provision 117, design 112, scaffold 229, fix 80, document 66}.mdx`.

**Graph** (`create-workflow.tsx:354-570`):
1. `Task clarify` (agents.planning) → `clarifiedSpecSchema`.
   - Tier-0 routing: `route.tier ∈ direct|oneshot|workflow`; non-workflow short-circuits to `routed-simple`.
   - "You MUST surface explicit clarifying questions" covering goal, inputs/outputs, and so on.
   - The questions are recorded with an **assumption**, not asked interactively (`clarify.mdx:1-50`).
2. `Task provision` (agents.implement, heartbeat 600s). It pulls `llms-*.txt`, picks the closest `examples/` template, selects components, runs `smithers skills add` for worker skills, and chooses agent pools.
3. `Task design` (planning). The graph design and an optional UI design.
4. `Branch → Approval "approve-design"` (`onDeny="continue"`, default-on). The summary includes "Q1: … (assumed: …)". **This is where the human answers the clarifications.**
5. `Task scaffold` (implement, 900s). It writes the workflow `.tsx`, prompts `.mdx`, UI `.tsx`, monitor workflow, and test `.tsx`, and registers the test in `.smithers/package.json`.
6. `Loop verify:loop` (max 3, `return-last`).
   - Optional `Task fix` with `FixPrompt(errors)`.
   - Then a compute `Task verify` that shells out:
     - `bunx smthrs graph <wf>`
     - `bun test --preload ... <test>`, which fails if the test is missing or unregistered (`packTestScriptIncludes`, `:33-41`)
     - `bun build --no-bundle <ui>`
7. `Loop skill:loop` (max 3). `Task document` (cheapFast agent) writes `.smithers/skills/<name>.md` with YAML frontmatter `name/description/workflow`, and the body covers how to run, detach, watch, and graph (`document.mdx`). Then `Task skill-verification` (real YAML parse, `validSkillDocument` `:49+`).
8. `Task output`: `{workflow, workflowFile, status: built|verify-failed|denied|designed|routed-simple|incomplete, summary, filesWritten, verified, skillPath, uiFile, nextSteps[]}` (`:238-262`).

**Sibling seeded workflows:** `route-task.tsx`, `make-workflow-tutorial.tsx`, `context-engineer.tsx`, `smithering.tsx`, and `extract-skill-propose` (turn a session into a workflow/skill).

---

### 5. main (1.0.0-rc.1): equivalents

This is a ground-up rewrite. The product has been repositioned to "Smithers maintains your codebase ... Flows beside your code" (`main/README.md`).
- It now includes a Bazel-like **build/target system** (`packages/smithers/build/build-cli/src/Cli.ts`, 1,787 lines: build/test/lint/affected/query/owners/graph/ci...).
- It includes a Go cloud backend (`packages/backend`, 273k LOC) and a hosted app.
- JSX/React workflows are gone. Flows are `Flow.make`/`Action.make` on Effect v4 (`flows/<name>/flow.ts`, or `flow.mdx` from `init`; state lives in `.flows/`).
- 0.x verbs are hard-refused (`src/Unsupported.ts` `removedVerbs`: replay, rewind, fork, timetravel, hijack, pause, gateway, ui, gui, monitor, supervise, chat, ask, agents, usage, hermes, alerts, ...).

### CLI

- Package `@smthrs/cli`, bins `smithers`/`smthrs` → `packages/smithers/bin/smithers.mjs`. Still built on `incur@0.5.1` (`src/Cli.ts:13`).
- The canonical verb table is `src/Verb.ts:79-110`: plan, run (alias resume), up, approve, deny, cancel, signal, steer, ls, ps, status (aliases inspect/why), logs (alias events), output, down, serve (alias gateway), init, suggest, doctor, migrate, gc, memory, claude, mcp, update, bug, completions.
- Five verbs start runs (`startsRuns`).
- Command tree (`src/Cli.ts:127-370`):
  - `flow` (`cli/ControlCommands.ts:171`): `list`, `show`, `plan` ("Compile a flow plan and its approval payload without executing it"), `start` ("Plan, approve, and start one flow; optionally detach"), `execute` ("Execute a previously approved plan payload").
  - `runs` (`:319`): `wait`, `list`, `count`, `show`, `devtools`, `logs`, `output`, `cancel`, `cancel-all`, `resume`, `continue`, `stop`, `signal`, `steer`, plus history `inspect`/`replay`/`verify` (`HistoryCommands.ts:39-79`).
  - `approvals` (`:802`): `list`, `approve`, `deny` ("the exact serialized payload or @file"), `grant`.
  - `generate` (flow/package/ci), `memory` (+notes/threads/messages), `mcp add`, `credentials`, `token mint`, `triggers` (list/show/register/fire/serve), `integrations`, `environment` (add/list/view/remove/exec/shell/forward), `eval` (list/run/baseline/compare; compare exits 1 on regression, 5 on inconclusive), `steps` (step cache).
  - Also `init`, `doctor`, `serve`, `gc`, `suggest`, `open`, `tui`, `migrate`, `update`, `bug`.

**Envelope.**
- `src/Output.ts`: `Format = "human" | "json"`; JSON is compact with sorted keys. It refuses executable or unbounded values (depth 128, 10k members, 4 MiB).
- Failures are one stderr line (`ClaimLost: claim_lost runId=run-42`) with nothing on stdout (`docs/concepts/output-and-exit-codes.md`).
- Next steps: `cli/Presentation.ts:144-190`. `runs()` follow-ups are capped at 3: `runs resume`, `runs show`, `runs logs --format jsonl`, `approvals list`. In structured mode they become `context.ok(value, { cta: { commands } })`; in human mode they print `Next:\nsmthrs ...`.
- `--audience auto|human|agent` is a global.
- **Exit codes (clean, single vocabulary):** `0` ok, `1` failed/run failed/conflict, `2` usage error, `3` parked waiting-approval, `130` cancelled/SIGINT, `143` SIGTERM. The receipt kind decides: `Parked→3`, `Terminal cancelled→130`.

**Approval model.** `plan` produces an approval payload/token, a human approves it, and then `execute` runs it.

### MCP

- `smthrs --mcp` is a mode, not a verb. It uses incur's discovery-style MCP: `search_tools`, `get_tool_details`, `call_read_tool`, `call_write_tool` over canonical names (`flow_list`, `flow_plan`, `flow_execute`, `runs_show`, `approvals_list`, ...).
- **`approvals_approve`, `approvals_deny`, `flow_start` are excluded** so the agent can't self-approve (`docs/guides/wire-the-mcp-server.md`).
- MCP limits: 10k events, 1 MiB per history result, 4 MiB per frame.
- `smthrs mcp add [--agent claude|codex]`:
  - Claude: writes `~/.claude.json` atomically under a lock.
  - Codex: runs `codex mcp add` and verifies with `codex mcp list --json`.
  - It records the exact executable path; 0.x's `bunx smthrs` drifted to the last published build (`src/Cli.ts:82-110`, `src/Agents.ts:101-102`).
- `packages/smithers/mcp` (`@smthrs/mcp`) is an MCP **client** that projects external servers' tools into flows (`--mcp-config`).

### Skills / plugins

- One packaged skill: `main/packages/smithers/skills/smithers/SKILL.md` (169 lines, much leaner). It is installed via `smthrs skills add` (incur sync, `include: ["skills/*"]`, `Cli.ts:117-120`) to e.g. `~/.claude/skills/smithers/SKILL.md` (`test/SkillsInstall.test.ts`).
- Sections: Rule 0, Route first, Mental model (Flow/Action/Node/Plan/Step key), 60s aha, flow directory, Authoring checklist, Durability, Operating a run, Recovery gates.
- Key quotes:
  - "Admission is not completion. Check the run's actual result before reporting success."
  - "Report the result, the run ID, and the next action in a few words; never call an accepted launch completed."
  - "`smthrs flow start` refuses to auto-approve `["*"]`" capabilities.
- There is **no claude-plugin/codex-plugin/hooks/monitors dir** in main. The `claude` verb (mirror protocol, `src/commands/Claude.ts`, `src/ClaudeMirror.ts`) remains.
- `main/.agents/skills/*` are maintainer-only skills (jj ABI, agent runtime, registry...).
- **No create-workflow equivalent.** Replacements are `smthrs generate flow` (`cli/Generate.ts:407`), `smthrs init <name>` (scaffolds `flows/<name>/flow.mdx`, `src/Init.ts`), and `smthrs suggest` (scans the repo with a checklist, streams suggestions, and implements a picked one in a kernel-guarded FS with no proc:spawn and no commits; `src/Suggest.ts:1-30`).

---

### 6. Code quality and maturity

| Metric | 0.x | main |
|---|---|---|
| Test files (`*.test.*`/`*.spec.*`) | 1,866 (packages 1,172; apps/cli 273; .smithers 84; examples 104) | 3,729 TS/JS **+ 1,802 Go `_test.go`** |
| Test LOC | ~431k | ~1.03M TS + ~411k Go |
| Source LOC (excl. tests, node_modules, docs, dist, .d.ts, generated) | ~582k total: .js 253k, .tsx 184k, .ts 123k, .mjs 19k. Core = `packages/` 253k + `apps/cli` 72k; `.smithers` pack 131k; demo apps/sites the rest. | ~1.56M total: .ts 729k, **.go 685k**, .tsx 48k, .rs 26k, .py 14k. `packages/smithers` 509k, `packages/backend` 273k, `apps` 175k. |
| Test framework | `bun test` (shard runner `apps/cli/scripts/run-test-shards.mjs`, `--max-concurrency=1`, 60-120s timeouts) | `vitest` (+`@effect/vitest`, coverage-v8), `go test`, cargo, `node --test` |
| Typing | **JS + JSDoc** with `.ts` type-only sidecars (e.g. `Semantic*.ts`, `CliExitCode.ts`), `allowJs`, `strict`, `tsc --noEmit`; 224 `.d.ts`; `scripts/check-dts.mjs` | Pure TS (16 `.d.ts`); eslint + `eslint.jsdoc.js` (`--max-warnings=0`) + `eslint.invariants.js` |
| Lint invariants | `oxlint` + format; root `pnpm test` chains 17 `scripts/check-*.mjs` (single-effect-version, npm-dedupe, dependency-boundaries, installed-footprint, ui-architecture, **no-direct-db-access**, local-smithers, docs, llms, dts, ...) (`0x/package.json:113`) | `eslint.invariants.js` rules "uninstalled safety", "swallowed cause", "ambient authority" via `no-restricted-syntax`; `check-toolchain-pins`, circular checks, docs drift |
| CI | `.github/workflows/`: ci.yml (431 lines: runtime-conformance, typecheck matrix, test matrix, coverage, test-postgres; Node 22, Bun 1.3.13), faults, faults-nightly, pr-review, release-next, sota-research | ci.yml (1,143 lines: test, repository, scripts, docs, apps-e2e, rust, rust-ffi, **wasm-repro** byte-drift, e2e-faults, browser, packages, go-backend; Go 1.26.8, `.node-version` 26.5.0) + canary, drift, distribution, native-windows, release, reliability, mirror-sync, apps-deploy |
| Runtime | `engines: node >=22, bun >=1.3.0` (`0x/package.json`); Bun is effectively required (bun:sqlite, `$` shell, `bunx smthrs`) | `@smthrs/cli engines: node >=26.4.0` (root also says bun >=1.4.0); Linux is the only required platform; Rust 1.98.0 toolchain (`rust-toolchain.toml`) for `smithers-ffi` (AGPL) and `flows-jj` → wasm32-wasip1; Go for the backend |
| Key deps | `incur ^0.4.10`, `effect 4.0.0-beta.105`, `react ^19.2.7` (JSX reconciler), `drizzle-orm ^0.45.2`, `zod ^4.4.3`, `ai ^7.0.10` (Vercel AI SDK), `@modelcontextprotocol/sdk ^1.29`, `@toon-format/toon 2.3.0`, `@mdx-js/*`, `esbuild`, `@xterm/*`, `@clack/prompts`; optional PGlite/pg; `electrobun` at root | `@smthrs/cli` has 51 deps, ~38 internal `@smthrs/*` rc.1; external: `effect 4.0.0-rc.115` (exact pin, peer everywhere), `incur 0.5.1`, `react 19.2.8`, `@opentui/core/react` (TUI), `@anthropic-ai/claude-agent-sdk 0.3.283`, `@clack/prompts`, `typescript 5.9.3` (runtime dep), `tar`, `yaml`; peers `@effect/sql-sqlite-node`/`sql-pg` |
| Distribution | npm `smthrs`, `bunx smthrs` | **Not on npm**: "The 1.0 release candidate is not on npm. Install it from the source checkout" plus a cargo build (`main/README.md`); RC, not an LTS commitment (`RELEASE_SUPPORT.md:3-4`) |

### Maturity signals

**0.x**
- `index.js` is a 14.5k-line monolith.
- The two exit-code vocabularies conflict (see §1).
- The claude-plugin skill says "the CLI exits non-zero, code 3, on suspend", which matches the docs but not `CliExitCode.ts`.
- `todo.md` (191 lines) lists open items:
  - a repo federation split into 10 repos (unexecuted)
  - "Quota-park kills the engine"
  - #1348 snapshot input pre-validation breaking fork/restore
  - #1349 "control-plane DB unbounded growth (was 100GB...)"
  - a DB-swap handle trap
  - 104 allowlisted UI-architecture violations
- Only about 7 `TODO|FIXME` comments in packages + cli src.
- Heavy dependence on frontier-model seats (sol/kimi/fable/opus naming baked into the skill).
- An effect *beta* pin.

**main**
- Release candidate only, source-install only, Node 26.4+ and Linux only.
- Effect rc pin "Keep exact application pins" (`RELEASE_SUPPORT.md:42`). Resume across the rc.112→rc.115 Effect upgrade is not claimed (`:125-130`).
- 263 TODO/FIXME in non-test TS/Go/Rust.
- Very broad scope: cloud backend, billing ("commerce", "credits"), hosted app.
- The AGPL crate sits in the install path.

### Takeaways for a Python workflow CLI

From 0.x:
- The incur pattern: one schema-declared command tree, auto-projected to `--format toon|json`, MCP, generated skills, and `--llms`.
- A data + `cta{commands[{command,description}]}` envelope on non-TTY output.
- An `{ok,data|error{code,message,details,docsUrl}}` MCP envelope.
- A semantic MCP tool set, with `readOnlyHint` and an `--allowed-tools`/`--read-only` scope.
- SKILL.md rules: Rule 0 via an env var, route-by-shape, "act on next steps", "never tell the human to run commands", repair-loop budget.
- The builder-as-workflow, with clarify → provision → design → approval → scaffold → verify-loop → document-skill.

From main:
- A single clean exit-code vocabulary in which `3` means parked.
- plan → approval payload → execute, with approval verbs **withheld from MCP**.
- "Admission is not completion".
- `mcp add` that writes the absolute executable path.
- Deterministic, bounded JSON output.

