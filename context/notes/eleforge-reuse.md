# Eleforge → forgeflow: reuse inventory

Date: 2026-10-02. Read-only survey of `/homes/nessa/zhanghao/dev/Eleforge` at commit `e420e00c`.
Paths below are relative to the Eleforge root. Abbreviations: **WF** = `apps/backend/compute/workflows/`, **LF** = `packages/labflow/python/src/labflow/`, **LFTS** = `apps/LabFlow/src/` (the TypeScript reference runtime).

**Recommendation scale:**
- **LIFT**: copy nearly as-is.
- **ADAPT**: reuse the code or design, with the changes stated.
- **REFERENCE**: learn from it, then rewrite.
- **LEAVE**: do not reuse.

---

## 0. TL;DR

1. **LabFlow already has forgeflow's storage model: `plan.json` plus `events.jsonl` plus a pure fold.**
   - **ADAPT** `LF/events.py` (259 lines), `LF/repository.py` (180), `LF/plan.py` (260), `LF/template_cas.py` (110), and `fixtures/golden/*`. The fold, the atomic plan write and the fsync'd append are exactly the right shape.
   - **Change five things:**
     - Make the log the only source of truth. Eleforge never got past "SQL first, files are a mirror" (S3c is blocked).
     - Add a cross-process file lock; the current lock is a `threading.Lock`.
     - Add a per-event `seq`.
     - Add first-class attempt, amendment and decision events.
     - Drop the `nanoforge_runtime` dependency.
2. **The node state machine is reusable**: 9 states, pure transition tables at `WF/workflow_state_machine.py:5-34`. **Rerun semantics are not.** Eleforge has single-node rerun only: no downstream invalidation, `skipped` descendants are never revived, and the attempt table is dead. Interaction state (configure/confirm/checkpoint) lives in in-process dicts, not the log. forgeflow must design rerun+downstream, attempts and gates fresh, as events.
3. **There is no mid-run DAG amendment anywhere.** Subgraph expansion happens only at submit time (`WF/subgraph_expander.py`). Its ID scheme (`parent.inner`) and barrier-edge algorithm are worth taking. The sweep's "write the manifest before fan-out, backfill on recovery" pattern (`WF/workflow_manager_sweep.py:170-287`) is the best existing template for durable dynamic growth.
4. **Slurm/SSH**
   - **LIFT** the stdlib OpenSSH command layer (`apps/backend/compute/remote/{shell,command,errors}.py` and `remote/ssh/openssh.py`). It encodes hard-won ControlMaster lessons.
   - **ADAPT** the sbatch generator and the idempotent submit: `--job-name=<key>` plus `--comment=<fingerprint>`, a marker file, and find-existing-before-submit.
   - **ADAPT** the harvest: workdir `result.json` / `canceled` markers, "defer, never fake success", and the LOST watchdog.
   - **Fix the Slurm state map**, which has real bugs (§4.3).
5. **Agent harness**
   - Eleforge never captures harness output. Its brains are MCP clients that poll a queue.
   - **LIFT** the `claude -p` argv discipline (array argv, `--strict-mcp-config`, allow/deny tool lists, `--append-system-prompt`, an isolated cwd).
   - **LIFT** the pi MCP bridge (`tools/pi-brain/eleforge-mcp-bridge.ts`).
   - **REFERENCE** the pi SDK daemon for: `stopReason=error` detection, timeout races, token clamps, and candidate fallback.
   - forgeflow's `agent`-node result capture (stream-json, session-id, result file) is **new work**. The best reference is the vendored Claude Code source under `context/claude-code/src/bridge/sessionRunner.ts:287-303`.
6. **LEAVE:**
   - the SQL stores (`job_state_store.py` 2941 lines, `workflow_state_store.py` 1684);
   - the dispatch queue and status outbox tables;
   - the Redis leases;
   - `platform_tools.py` (7202 lines, coupled to the agent stores);
   - `packages/eleforge-agent`;
   - the domain template builders.

   Keep only their *patterns*.

---

## 1. Asset inventory (one table)

| # | Asset (path) | What it does | Maturity / tests | Eleforge coupling | Rec. |
|---|---|---|---|---|---|
| 1 | `LF/events.py` | `WorkflowEvent` dataclass (camelCase JSON) and the pure fold `apply_workflow_event` / `project_events` into `WorkflowProjection` | Golden fixtures (4 streams); backend tests `test_labflow_events.py` (22), `test_labflow_golden_fixtures.py` (7), `test_projection_parity.py` (20) | none (stdlib) | **ADAPT** (§2) |
| 2 | `LF/repository.py` | `WorkflowRuntimeRepository` ABC plus `FilesystemWorkflowRepository`: `{base}/{urlenc(run_id)}/plan.json` (tmp+`os.replace`) and `events.jsonl` (append + fsync per batch) | Used by the backend mirror; CLI tests (15) | none | **ADAPT**: add `fcntl.flock` (L67-69 admits it is not multi-process safe); do not silently skip corrupt lines (L164-167) — fail loudly or quarantine; tolerate only a torn last line |
| 3 | `LF/plan.py` + `LF/plan_schema.py` | `plan.json` envelope `{$schema, kind, apiVersion, planVersion, metadata, spec}`; deterministic dump (sorted keys, 2-space indent, LF); `PlanLoadError{code,message,path,suggestion}` built for LLM repair; JSON-Schema generation with a `--check` drift guard | `test_labflow_plan.py` (18); schema drift test | `nanoforge_runtime.workflows.workflow_models` import (plan.py:36, plan_schema.py:305) | **ADAPT**: keep the envelope, the determinism and the error shape; define forgeflow's own spec model |
| 4 | `LF/template_cas.py` | sha256 content-addressed store (`sha256:<hex>`, sharded by a 2-character prefix dir, canonical `sort_keys` + compact separators) | small | `LABFLOW_EVENTS_DIR` env | **LIFT** the canonical hash for plan versions, amendment ids and cache keys; pass the path explicitly |
| 5 | `LF/models.py` (573) | Pydantic protocol: `WorkflowTemplate`, `WorkflowNodeDefinition`, `WorkflowEdgeDefinition`, `WorkflowInputSource`, `WorkflowRetryPolicy`, `WorkflowExecutionPolicy`, `WorkflowResourceRequest`, output envelopes | drift tests | Also holds Eleforge route/SQL shapes (`WorkflowRunDetailResponse`, `*Snapshot`) — `labflow_refactor.md:108-110` | **ADAPT**: take the port/binding/resource/policy shapes and drop the route/SQL models |
| 6 | `LF/cli.py` (241) | `labflow <run> [--events --type=…]`, `--list`, `--stats`; reads the log offline | `test_labflow_cli.py` (15) | env var only | **REFERENCE** for the shape of `forgeflow status/log` |
| 7 | `LF/detail_builder.py` (283) | Projection → a UI detail DTO | parity tests | Eleforge DTO | **LEAVE** (pattern: a read model is derived only from plan + events) |
| 8 | `packages/labflow/fixtures/golden/*.events.jsonl` + `.expected.json` | Event streams for happy path, failed run, retry-succeeds and canceled-midway, with their expected projections | Python ↔ TypeScript parity harness (`tests/test_python_ts_parity.py`) | none | **ADAPT**: the golden-fixture replay-test pattern is a must-have |
| 9 | `apps/backend/labflow/event_sharding.py` | Splits events into per-shard files by node index (`shards/events-shard-NNNN.jsonl`); workflow-level events stay in `events.jsonl` | scaffolding only, never hooked up | env | **REFERENCE** (only if fan-out exceeds about 10^4 nodes) |
| 10 | LFTS: `nodeRuns.ts`, `runState.ts`, `scheduler.ts`, `predicates.ts`, `join.ts`, `fanout.ts`, `attempts.ts`, `validator.ts`, `runtime.ts` (1327), `repositories.ts` | TypeScript reference runtime, file-backed and engine-neutral: ready computation, predicate DSL, join policies, fanout item keys, approval suspend/resolve, heartbeat, cancel | 16 `.mjs` test runners; **not used in production** | none | **REFERENCE**: cleanest semantic spec of ready/join/approval (§3.4) |
| 11 | `WF/workflow_state_machine.py` (46) | Run and node transition tables with `ensure_*` validators | no direct unit test | only the Literal types | **LIFT** the tables, with the modifications in §3 |
| 12 | `WF/workflow_manager_tasks.py`, `_rerun.py`, `_sweep.py`, `workflow_manager.py`, `workflow_state_progression.py`, `progression_service.py` | SQL-backed DAG control plane: dispatch, lease, failure propagation, finalize, rerun, sweep, pause/resume, cancel, interactions | `test_workflow_manager.py` (28+), `test_workflow_node_rerun.py` (11), `test_workflow_sweep.py` (17) | heavy: job manager, SQL store, functional registry, canvas sync, agent stores, `auth.ownership_store` | **REFERENCE** (semantics and lessons only) |
| 13 | `WF/subgraph_expander.py` | Submit-time subgraph inlining: `parent.inner` ids, input-pin rewiring, barrier edges, output re-pointing, `_MAX_DEPTH=4` | `test_subgraph_expander.py` (6) and others | one import of `functional.workflow_templates.registry` (easy to inject) | **ADAPT** as the amendment and subflow splice primitive |
| 14 | `compute/remote/shell.py`, `command.py`, `errors.py`, `remote/ssh/openssh.py` | `RemoteCommandSession` protocol, `CommandResult`, an OpenSSH session with a bounded timeout, auth detection, and `put_text`/`get_text` over stdin/cat | `test_openssh_control_master_policy.py` (3) and others; the timeout/killpg path is **untested** | stdlib plus one metrics counter; `remote/ssh/__init__.py` eagerly imports paramiko | **LIFT** (import `openssh` directly) |
| 15 | `compute/runners/slurm/runner.py` + `launch_script.py` | sbatch generation (pure `emit_sbatch_directives`, L140-167), idempotent submit, batched squeue/sacct poll, workdir fallback, scancel plus a marker, result harvest | `test_slurm_runner.py` (15) | medium: `schemas`, `deployment_profile`, stream token, DB template store, log watcher | **ADAPT** (about 200 extractable lines) and fix the state map |
| 16 | `compute/runners/ssh_machine/runner.py` | No-scheduler remote run (`nohup setsid`, pid, `exit_code`, `canceled` marker), LOST watchdog, artifact tar-over-base64 harvest | `test_ssh_machine_runner.py` (5) | medium | **ADAPT** for the `shell`-on-remote / `job` without Slurm cases |
| 17 | `compute/jobs/manager_reconcile.py` | Watcher tick: batched status per (runner, connection), parallel per connection, reattach for `submit_unknown`, deferred harvest, LOST confirmation | `test_job_manager_remote_status_refresh.py` (14) | JobManager / store singletons | **REFERENCE**: rewrite as `forgeflow tick` over files |
| 18 | `compute/jobs/state_machine.py`, `compute/models.py:9-26` | Job states plus a `SubmitState` axis (`accepted/submitting/submitted/submit_unknown`) | `test_job_state_store.py` (23) | none (tables) | **ADAPT**: the separate submit axis is essential for crash-safe submits |
| 19 | `compute/jobs/state_dispatch.py`, `state_outbox.py`, `coordination/*` | SQL dispatch queue (claim TTL, deadletter), status outbox with consumer cursors, Redis leader/claim leases | well tested | DB / Redis | **LEAVE** (the event log plus a file lock replaces them; keep the "cursor + lease + idempotent consumer" idea) |
| 20 | `packages/runtime-core` (`nanoforge_runtime`) | Science worker: `job_worker --payload-path --result-path` (JSON in, JSON out); `FunctionResult{status: success\|error\|queued\|checkpoint}`; contextvar progress/cancel hooks; `PackedWorkflowEventSink` protocol | about 20 science-handler tests | standalone (pydantic) | **REFERENCE** for the `function`-node worker contract and the `__NF_PROGRESS__<json>` progress lines; **LEAVE** the science code |
| 21 | `tools/claude-brain/claude-brain-loop.sh` (101), `fetch-playbook.mjs` | Headless `claude -p` loop with an MCP config and a pinned playbook | manual | MCP URL only | **LIFT** the argv flags (§6.1) |
| 22 | `tools/pi-brain/eleforge-mcp-bridge.ts` (199) | pi extension: MCP `tools/list` → `pi.registerTool`, playbook pin via `before_agent_start`, close on `session_shutdown` | manual | none | **LIFT** |
| 23 | `tools/pi-brain/pi-brain-service.mjs` (624), `pi-brain-loop.sh` (123), `import-subscription.mjs` (144) | Resident pi SDK daemon (fresh session per wake, timeout race, error detection, model fallback, token clamp, steering); shell loop with duration-keyed backoff; CLI-login → pi `auth.json` import | `test-tiering.mjs` (about 22 asserts) | medium (Eleforge tool names, runtime URL) | **ADAPT** the pi runner; **REFERENCE** the rest |
| 24 | `apps/backend/mcp_server/tool_groups.py` (182) | Core tools plus named groups plus an `expand_toolset` meta-tool, with an invariant test (each tool in exactly one group) | `test_tool_groups.py` (7) | none | **LIFT** the pattern for forgeflow's optional MCP |
| 25 | `apps/backend/mcp_server/agent_playbook.py` (785) | Versioned (`PLAYBOOK_VERSION="32"`) playbook served by a tool and pinned in the system prompt | `test_agent_external_brain.py` (116) | domain content | **REFERENCE** (it becomes forgeflow's skill file) |
| 26 | `mcp_server/platform_tools.py` gates (L2487-2921) | Server-side permission gate, check-then-increment budget, advisory lease, dead-letter after 8 redeliveries, long-poll | tested | high | **REFERENCE**: reimplement file-first |
| 27 | `apps/backend/functional/workflow_templates/` (58 files, 60 templates) + `doc/workspace/2026-06-28-dag-template-audit.md` | Real DFT DAG shapes and a 53-template adversarial audit | the audit itself | domain | **REFERENCE** (§7) |
| 28 | `packages/eleforge-agent` | Compute-node enrollment daemon (`/edge/*`) | no tests | Eleforge control plane | **LEAVE** |

---

## 2. LabFlow event schema (concrete)

### 2.1 Event record (`LF/events.py:71-117`)

```python
@dataclass
class WorkflowEvent:
    event_id: str; run_id: str; event_type: WorkflowEventType; occurred_at_iso: str
    node_run_id: str|None; attempt_id: str|None
    actor_type: Literal["human","agent","system"]|None; actor_id: str|None
    payload: dict = {}
# JSON keys: eventId, runId, eventType, occurredAtIso, nodeRunId?, attemptId?, actorType?, actorId?, payload?
```

### 2.2 Event types (`LF/events.py:12-47`; TypeScript twin `LFTS/types.ts:415+`)

| Group | Types |
|---|---|
| workflow lifecycle | `workflow.accepted`, `workflow.planned`, `workflow.started`, `workflow.suspended`, `workflow.completed` (`payload.state` ∈ succeeded/failed/canceled) |
| workflow outputs | `workflow.outputs.committed` (`payload.outputs`, `payload.envelope`), `workflow.outputs.failed` |
| node lifecycle | `node.ready`, `node.leased`, `node.started`, `node.heartbeat`, `node.suspended`, `node.failed` |
| node outputs | `outputs.committed` (`payload.outputs`, `payload.envelope`) |
| interaction | `node.checkpoint`, `node.awaiting_config`, `node.awaiting_confirm` |
| retry, approval, cancel | `retry.scheduled` (`nextAttemptAtIso`), `approval.requested`, `approval.resolved` (`approved`), `cancellation.{requested,acknowledged,completed,detached,failed}` |
| external execution | `external.submitted`, `external.state_observed` |

### 2.3 Gaps in the vocabulary

- **No `node.succeeded`, `node.canceled` or `node.skipped` literal.** The backend emits them anyway through `event_type=f'node.{to_state}'` and `f'workflow.{to_state}'` in `WF/workflow_state_progression.py`. Skipped descendants get **no event at all**.
- forgeflow needs one closed, explicit vocabulary that includes success, skip and invalidate.

### 2.4 Projection fold rules (`LF/events.py:175-251`)

- **Node state:** any event whose `payload.nodeRun` is a dict *replaces* `node_runs[nodeRunId]`. Events are **state-carrying snapshots, not deltas**.
  - Lesson S3a.3 (`doc/workspace/2026-04-24-s3-projection-authoritative.md`): the projection could not see node state until every transition event carried the full nodeRun.
- **Attempts:** `payload.attempt` together with `event.attempt_id` sets `attempts[attemptId]`.
- **Outputs:** `payload.outputs` is merged into a flat `outputs`. Envelopes are kept only when their outputs list is non-empty (S3a.9).
- **Run state:**
  - `accepted`, `planning`, `running` and `suspended` come from a fixed map (L62-68).
  - `workflow.completed` is coerced from `payload.state`, defaulting to `succeeded`.
  - `message` is updated only when the key is present.
- **forgeflow should keep** the pure, deterministic, no-I/O fold and snapshot-carrying payloads.
- **forgeflow should add:**
  - a monotonic `seq` per run (Eleforge hit same-microsecond ordering ambiguity: commit `2bd03af1`);
  - `schemaVersion`;
  - a hash or reference for agent outputs, so replay reuses recorded results;
  - `causationId` / `amendmentId` for provenance.

### 2.5 Golden fixture example (`fixtures/golden/retry-succeeds.events.jsonl`)

`node.started(at-1)` → `node.failed{errorClass:"transient"}` → `retry.scheduled{nextAttemptAtIso}` → `node.started(at-2, attemptNo 2)` → `outputs.committed` → `workflow.completed{state:succeeded}`.

This is the attempt model forgeflow wants. The production backend never used it (§3.2).

### 2.6 Attempt record and error classes (`LFTS/types.ts:392-413`; worth adopting)

`WorkflowNodeAttempt{attemptId, nodeRunId, attemptNo, leaseOwner?, leaseExpiresAtIso?, heartbeatAtIso?, idempotencyKey, errorClass?: 'user'|'data'|'transient'|'timeout'|'quota'|'external'|'canceled', externalExecutionHandle?{system:'slurm'|…, reference, submittedAtIso, statusRef?}, logRef?}`

### 2.7 `plan.json` schema

The envelope is defined in `LF/plan.py:84-126` and `plan_schema.py:440-468`; the generated artifact is `packages/labflow/schemas/plan_schema.json`.

```json
{"$schema": "...", "kind": "WorkflowTemplate", "apiVersion": "labflow.eleforge.dev/v1", "planVersion": 1,
 "metadata": {"id","version","title","runId?","labels","annotations","cachePolicy{maxAge,staleWhileRevalidate,varyOn}","rateLimit{…}"},
 "spec": {"inputs":[PortSpec], "outputs":[PortSpec], "nodes":[NodeDef], "edges":[EdgeDef], "outputBindings":[…]}}
```

**`NodeDef`** (`LF/models.py:165-187`), `extra="allow"`. Fields:
- identity and kind: `id`, `title`, `stage`, `shape`, `dispatchMode`, `interactionMode`;
- contract and handler: `contract{inputs,outputs}`, `handler{type,ref,version}`;
- execution: `executor{target,queue,resourceRequest{cpusPerTask,gpus,walltimeSec,memGb,partition,runtimeRef,mpiRanks}}`, `policy{timeoutMs,retry{maxAttempts,backoffMs,backoffFactor,retryOn},cacheable,continueOnFailure,maxParallelism,budgetRef}`;
- wiring: `bindings[{targetPort, source}]`;
- control: `activationPredicate`, `completionPredicate`, `fanoutSpec`, `joinPolicy`;
- subgraph: `subgraphRef`, `subgraph{Input,Output}Mappings`;
- `tags`.

**`InputSource.type`:** `constant | workflow-input | node-output | built`. **Edge:** `{fromNodeId, fromPort?, toNodeId, toPort?, type}`.

**Enumerations in the TypeScript reference** (`LFTS/types.ts:3-32`):
- shape: `task|subgraph|branch|fanout|join`
- dispatchMode: `sync|async|external`
- interactionMode: `automatic|approval|human-task|wait-time|wait-event`
- valueKind: `scalar|json|document|table|structure|dataset|artifact-bundle|citation-bundle|analysis-report|decision-record`

The Python backend uses a different interactionMode set (§3.3), so the two sides have drifted.

**forgeflow should:**
- keep the self-identifying envelope, deterministic serialization, and `PlanLoadError` codes (`INVALID_JSON, MISSING_KIND, WRONG_KIND, WRONG_API_VERSION, UNSUPPORTED_PLAN_VERSION, MISSING_SPEC, SPEC_VALIDATION`, plus `path` and `suggestion`);
- replace `shape/dispatchMode/handler` with `kind: agent|shell|function|job|gate`;
- make `plan.json` **mutable only through recorded amendments**. In LabFlow it is immutable after `save_plan`, and the `.tmp` + `os.replace` write is atomic.

**In the real templates** (60 registered; 48 build with empty params), every node is `shape=task`, `dispatchMode=sync`, and 301 of 302 nodes are `interactionMode=automatic`. All edges are `data` edges. The rich shape vocabulary (branch/fanout/join/predicates) went unused in practice; fan-out is done by sweeps or by unrolling.

---

## 3. Node state machine, rerun, interaction

### 3.1 States and transitions (`WF/workflow_state_machine.py:5-34`; self-transition always allowed, L38/L44)

```
RUN : accepted ->{planning,running,failed,canceled}   planning ->{running,suspended,failed,canceled}
      running  ->{running,suspended,succeeded,failed,canceled}   suspended->{planning,running,failed,canceled}
      succeeded|failed|canceled -> {running}        # "REOPEN" for single-node rerun
NODE: pending  ->{ready,leased,running,skipped,failed,canceled}
      ready    ->{leased,running,skipped,failed,canceled}
      leased   ->{running,ready,failed,canceled}
      running  ->{running,suspended,succeeded,failed,canceled}
      suspended->{ready,running,failed,canceled}
      succeeded|failed|skipped|canceled -> {}       # terminal; retry = NEW node_run row
```

**Differences in the TypeScript reference:**
- `LFTS/runState.ts:3-11` makes terminal run states immutable.
- `LFTS/nodeRuns.ts:66-76` allows `running→ready`, used for a re-queue.

**forgeflow recommendation:**
- Keep the 9 node states.
- Make terminal states final **per attempt**. A node's current state is the state of its latest attempt.
- Add an explicit `superseded`/`invalidated` marker, recorded as an event, for rerun+downstream.
- Add a **separate submit axis** for `job` nodes, mirroring `compute/models.py:11`: `accepted|submitting|submitted|submit_unknown`.

### 3.2 Failure propagation, finalize and rerun (production behavior)

**Partial completion** (commit `29f1b08f`):
- `_skip_descendants_after_node_failure` (`WF/workflow_manager_tasks.py:769-795`) does a DFS over `template.edges`. `pending`/`ready` descendants become `skipped`, with **no event**.
- Independent branches continue.
- Finalize (L797-844) collapses to the **latest node_run per node_id**. Then: `canceled` if any node was canceled; `succeeded` if at least one node succeeded (partial success, with failed nodes named in the message); otherwise `failed`.
- Optional workflow outputs from a failed producer become absent; a missing required output makes the run fail (`WF/workflow_manager.py:969-1036`).

**Whole-run retry** (`workflow_manager.py:341-366`): forks a new run (`retry_of_run_id`) and reuses nothing.

**Single-node rerun** (`WF/workflow_manager_rerun.py`):
- Inputs come from the **committed upstream outputs** (latest `committed_at`), plus `inputOverrides`.
- The terminal run is reopened to `running`, and a **new node_run row** is created.
- The execution shape is inherited from the oldest prior attempt. Remote nodes are re-dispatched asynchronously on the inherited queue (`13f6c842`).
- Idempotency key: `workflow:{run}:rerun:{node_run_id}`.

**Gaps forgeflow must close:**
- No downstream invalidation: stale downstream outputs survive, and `skipped` descendants are never revived.
- No rerun-with-downstream API.
- `workflow_attempts` (`WF/workflow_state_store.py:225-240`) is never written.
- `_append_completed_event` dedups `workflow.completed` within the first 2000 events, so a reopened run never re-notifies.
- `transition_node_run` returns silently when its compare-and-swap loses.
- A crash while a node is in `leased` is never recovered, because leases have no TTL.

**Cancel** (`WF/workflow_manager_tasks.py:984-1047`):
- Idempotent on a terminal run.
- Cancels the child jobs, sets every non-terminal node to `canceled`, sets the run to `canceled`, and appends `workflow.completed`.
- A sweep parent cancel does **not** cascade; late children are coerced to canceled.

**Pause and resume** (`workflow_manager.py:399-438`):
- `suspended` blocks dispatch and finalize. Running jobs continue.
- Resume re-dispatches and re-finalizes (`1734fdf2`).

**Restart recovery** has three parts:
1. A durable outbox consumer cursor (`WF/progression_service.py:56-133`).
2. A read-time reconciler (`WF/workflow_manager_native.py:84-128`).
3. Deterministic ids (`job-{node_run_id}`, `WF/workflow_manager.py:853`).

**Ready rule** (`_promote_pending_task_nodes`, `tasks:716-767`):
- A node is ready when all **edge** parents' latest node_runs are `succeeded`. Bindings alone do not count.
- `maxParallelism` counts the run's leased + running nodes.
- The TypeScript reference (`LFTS/scheduler.ts:49-103`) also honours `activationPredicate`. Its join nodes accept any terminal upstream, and `skipped` counts as satisfied.

### 3.3 Interaction modes and checkpoints

The backend supports `{"automatic","configure_on_input","confirm_on_output"}` (`WF/workflow_manager_tasks.py:846`).

| Mode | Where it pauses | Event | Resume | Durability |
|---|---|---|---|---|
| `configure_on_input` | Before the lease; the node stays `ready` (L607-638) | `node.awaiting_config{inputSchema,resolvedInputs}` | `POST …/nodes/{id}/config`, then `set_configured_inputs`, then dispatch | `_CONFIGURED_INPUTS` dict, **lost on restart** |
| `confirm_on_output` | The job succeeded but nothing is committed; the node stays `running` (L336-375) | `node.awaiting_confirm{preview,patchKeys}` | `…/confirm {accept\|reject}`. Accept commits; reject **fails the whole run** | `_PENDING_CONFIRM` dict, rebuilt by replay |
| function `checkpoint` (`FunctionResult.status=="checkpoint"`) | Same path as confirm (L263-330) | `node.checkpoint{inputType,question,options,fields,actions,context}` | same | same; with an agent session, `preview` checkpoints are auto-accepted |
| TypeScript `approval` | The node is `suspended` (`LFTS/runtime.ts:500-530`) | `node.suspended` + `approval.requested` → `approval.resolved{approved}` (actor `human`) | `resolveApproval()`: approved → `succeeded`, else `canceled` | in the log |

**forgeflow's `gate` kind should be the TypeScript approval model**: everything in the log, and the request and resolution are events carrying actor and rationale. Also:
- Configure and confirm become *properties of any node*, both recorded as events.
- **Lesson `3646e095`:** every pause type needs a programmatic CLI or tool to respond, not just an HTTP/UI path. Otherwise agent-driven runs deadlock.
- **Lesson `bbf97142`:** never truncate or elide open interaction events. Status views must always surface still-open gates.

### 3.4 Dynamic growth: what exists

**Subgraph expansion** (`WF/subgraph_expander.py`) runs at submit time only (`WF/workflow_manager_submit.py:68`):
- Ids are `f"{subgraph_node.id}.{inner_id}"` (L117-124).
- Inner `workflow-input` pins are rewired through `subgraphInputMappings`; unmapped required pins are auto-filled from the inner `metadata.defaults` (`d7e54aca`).
- **Barrier edges** run from the parent's predecessors to the inner roots (L223-243).
- Outer `node-output(subgraph,X)` is re-pointed through the inner `outputBindings` (L245-304).
- Depth is capped at 4.
- No event records the expansion; the flattened template simply *is* the plan.

**ADAPT this as the splice primitive for forgeflow amendments.** An approved amendment would:
1. emit `plan.amendment.proposed`;
2. emit `plan.amendment.approved{diff, contentHash}`;
3. write `plan.json` atomically (tmp+rename) as version n+1;
4. make the fold derive new node runs from the amendment.

**Sweep two-phase manifest** (`WF/workflow_manager_sweep.py:170-287`):
1. Write the `node.sweep.launched{manifestSeq:1, rows: pending}` event **before** any submit.
2. Submit each child with idempotency key `sweep:{run}:{node_run}:{label}`.
3. Write the manifest again with seq 2.
4. On recovery, backfill `pending` rows from the job links.

Children carry back-pointer labels. The finalize step uses a single-winner lock.

**Lessons from `2bd03af1`:**
- Query the manifest by event type with no limit; a 2000-event window lost it behind heartbeat floods.
- Use a sequence number, not timestamps.
- A label-scoped rerun must not touch un-named items.

---

## 4. Slurm and SSH (compute)

### 4.1 Job model (`apps/backend/compute/models.py:9-26`, `compute/jobs/state_machine.py:5-11`)

```python
JobState    = Literal['queued','running','completed','failed','canceled']
SubmitState = Literal['accepted','submitting','submitted','submit_unknown']
# queued->{running,completed,failed,canceled}; running->{queued(requeue),running,completed,failed,canceled}; terminal->{}
```

`JobSnapshot` (L50-85) carries the fields worth mirroring in forgeflow's `job` attempt record:
- `job_version` (for compare-and-swap);
- `idempotency_key`;
- `connection_ref`;
- `remote_ref` (the Slurm job id or PID);
- `remote_workdir`;
- `remote_submit_fingerprint`;
- `submit_status`;
- `cancel_requested`;
- `payload_hash`.

**Submit protocol** (`compute/jobs/state_submission.py`):
- `begin_submit` (L323) moves to `submitting` with a compare-and-swap.
- It ends in one of `complete_submit` (L615), `reset_submit_state` (L487) or `mark_submit_unknown` (L777).
- An uncertain failure (retryable or HTTP 429) becomes `submit_unknown`, which a later reattach recovers.

### 4.2 Submit (`compute/runners/slurm/runner.py`, `launch_script.py`)

**Script generation** (`launch_script.generate_sbatch_script` L277-422) works in phases: resolve, then env exports, then pure `emit_sbatch_directives`. The preview and the real submit share the formatter (`230dc7c7`).

Directives and body:
- `--job-name=<submit_key>` and `--comment=<fingerprint>`: the idempotency keys.
- `--output/--error={wd}/slurm.{out,err}`, `--chdir`, `--ntasks=1 --cpus-per-task=N`, `export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-N}` (`7a0b4f2a`).
- Body: `worker … --result-path {wd}/result.json >> job.log; WORKER_EXIT=$?; exit $WORKER_EXIT`.

**Find-existing before submit** (L420-469) looks the job up with `squeue -h -o '%A|%j|%k'` and `sacct -n -P -X -o JobIDRaw,JobName,Comment,Submit`, matching on name and comment. If the result is ambiguous, it refuses.

**Remaining steps:**
- Write the marker `{wd}/.nanoforge-submit.json`.
- Upload the script to `/tmp/nf_submit_{id}.sh`, then run `sbatch`.
- Job id parsing takes the first all-digit word of stdout (L615-622). **forgeflow should use `sbatch --parsable`.**

### 4.3 Polling, the state map and its bugs

**Polling** (`get_statuses` L715-786):
- One batched `squeue -h -j a,b,c -o '%A|%T'` per tick.
- For ids missing from squeue, `sacct -j … -n -P -X -o JobIDRaw,State`.
- If both are empty, `_resolve_status_via_workdir` (L679-709) checks the workdir:
  - a `canceled` file means `canceled`;
  - a non-empty `result.json` means `completed`, or `failed` if `status=="error"`;
  - otherwise unknown.
  - This handles clusters without slurmdbd (`ec3d1b37`, `31d458d5`).
- Any other exception reports **every job as `running`** (L784-786); `RemoteAuthRequiredError` is re-raised instead.

**State map** (`_map_slurm_state`, L477-496, verified):

```python
PENDING -> queued | RUNNING, COMPLETING, CF, CG -> running | CANCELLED, TIMEOUT, REVOKED -> canceled
FAILED, NODE_FAIL, OUT_OF_MEMORY, DEADLINE -> failed | COMPLETED -> completed | <else> -> running
```

**Bugs that forgeflow must not copy:**
1. `sacct` reports `CANCELLED by 1234`, which falls through to `running`, and the workdir fallback is skipped. **Match on the first token.**
2. `TIMEOUT` maps to `canceled`. It should be `failed` with `errorClass=timeout`.
3. Unmapped states (`PREEMPTED`, `BOOT_FAIL`, `OOM`, `SUSPENDED`, `REQUEUED`, `RESIZING`, the short codes `PD`/`R`/`CA`/`F`/`TO`/`NF`) all fall to `running`.
4. There is no LOST watchdog for Slurm. A job that is gone from squeue and sacct and has no `result.json` stays `running` forever.
5. `ExitCode` is never queried. The exit status comes only from `result.json` plus "soft fail" (`manager_reconcile.py:53-69`).

**Fix:**
- Make the mapping table total, with an explicit `unknown` state that counts toward LOST.
- Use `sacct -o JobIDRaw,State,ExitCode,Elapsed,MaxRSS -P -X`.
- Unit-test the mapping table; Eleforge has no test for it.

**Cancel:** `scancel <id>` followed by `touch {wd}/canceled` (L643-677).

**ssh_machine runner** (no scheduler):
- Launch: `nohup setsid bash -c 'echo $$ > runner.pid; exec bash launch.sh' > launcher.out 2>&1 < /dev/null &`.
- Status is one shell expression: `canceled` file → `kill -0 pid` → `exit_code` file → LOST.
- Cancel: `kill -TERM -pgid`, sleep 1, then `-KILL`.

### 4.4 SSH layer (`compute/remote/ssh/openssh.py`)

**Automated commands** (`base_args` L67-105):

```
ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0 -o ConnectTimeout=30 -o ServerAliveInterval=20 -o ServerAliveCountMax=4 [-i key] [-o StrictHostKeyChecking=…] [-p port] target
```

They deliberately use **no ControlMaster** (comment at L84-91; commits `e372517d`, `0d06b919`).

**Interactive use** (MFA/Duo login, shell):
- `ssh -MNf -o ControlMaster=yes -o ControlPath=~/.nanoforge/ssh/%C -o ControlPersist=8h`.
- `%C` (a hash of the connection details) avoids the socket path length limit.
- `ssh -O check|exit` manages the master, and the socket directory is created with mode 0700.

**`run()`** (L199-275):
- Wraps the command as `/bin/bash -lc '<cmd>'`.
- Uses `Popen(start_new_session=True)` and `communicate(timeout=60)`.
- On timeout: `killpg(SIGKILL)`, a drain of at most 2 s, close the fds, `ssh -O exit`, unlink the socket, then raise `TimeoutError` (`dbb0fd2d`).

**Auth detection** (L319-337): exit code 255 plus stderr markers (`permission denied`, `keyboard-interactive`, `duo`, …) raises `RemoteAuthRequiredError`.
- **Do not retry** on this error.
- Leave the jobs untouched until re-auth (`doc/design/compute/compute_ssh_connection_module_redesign.md:408-418`).

**File transfer** has no rsync or scp: `put_text` pipes stdin into `umask 077 && cat > p && chmod`, and `get_text` is `cat`. Artifacts come back as tar.gz over base64 (`ssh_machine/runner.py:664-730`):
- excludes `*CHARGE-DENSITY*`, `*WAVEFUNC*` and `*.restart`;
- capped at 50 MB;
- extracted with a path-traversal guard.

forgeflow should add rsync for large outputs, but keep the "outputs stay remote, by reference" default.

**Retry:** `remote_retry(3, ×2)` for transport errors only. Each submit retry builds a fresh session, and each attempt is idempotent through the marker plus find-existing (`c62bb9f0`).

**Doc vs code:** the redesign doc (L323-342) still prescribes `ControlMaster=auto` and `ConnectTimeout=15` for background commands. **The code is right and the doc is stale.**

### 4.5 Watcher and harvest semantics to replicate in `forgeflow tick`

**Batching and claims:**
- Batch status by (runner, connection), running connections in parallel with `ThreadPoolExecutor(min(4, n))` (`c8a5ba36`). One hung connection otherwise stalls the whole tick.
- Take a per-job claim before acting (Redis `SET NX EX 10`; in forgeflow, a file lock).

**Harvest order on terminal:** `fetch_result` → `fetch_artifacts` (best effort) → effective status (completed + `result.status=="error"` becomes failed) → transition. The harvest is idempotent: it returns early if the stored status is already terminal or the remote status is unchanged.

**No fake success:** if `result.json` is unreadable, defer, and after `_RESULT_FETCH_MISS_LIMIT=5` misses fail explicitly.

**LOST:** declared after `_LOST_CONFIRM_LIMIT=3` consecutive LOST observations. Any non-LOST observation resets the count. Eleforge keeps both counters in memory; **forgeflow should record observations as `external.state_observed` events** so they survive restarts.

**Detached runners:** shutdown never cancels remote jobs.

**Watcher liveness:** it must exist and be observable (`9a592618`: "jobs done for days, never terminal" because no watcher thread ran). forgeflow therefore needs `forgeflow tick`, plus a `status` view that shows the last tick time.

**Progress:** short-lived `tail -c +<offset> job.log` polls with the offset tracked locally. `__NF_PROGRESS__<json>` lines emitted by the worker are authoritative (`af9cd6d7`).

---

## 5. Durable-execution design lessons from the LabFlow migration

These come from `doc/workspace/2026-04-24-s3-projection-authoritative.md`, `doc/design/labflow_refactor.md` and the commits.

- **Dual-write never converged.** S3a needed 11 parity sub-fixes, and S3c ("remove the SQL writes") is still blocked.
  - Examples: transition events bypassed the mirror (S3a.2); `save_plan_to_labflow` was never called (S3a.11); empty envelopes caused divergence (S3a.9).
  - **forgeflow should have exactly one writer and one store: the log.** Any index must be rebuildable from it.
- **The mirror soft-fails** (a broken mirror cannot stop execution). That is acceptable for a mirror and fatal for a source of truth. forgeflow must fail closed on a failed append.
- **`labflow_refactor.md:274-279`:** "LabFlow records policy + events; host owns retry/pause/resume/rerun; replay must show every attempt, every produced output, and why the final state was accepted." This is a good principle statement to adopt verbatim.
- **Provenance spec** (`labflow_refactor.md:251-279`): node *creates* versus workflow *returns*; exit record (0 or nonzero with code and message); `value` versus `artifact` ports (URI + hash + size + media type). Agent steps record prompt, tools, schemas, evidence, exit status, and the verification or human-gate result (L324-326). This was never implemented, so forgeflow can own it.
- **Event-type parity guard** (`apps/backend/tests/test_labflow_event_type_parity.py`): it greps the emitters for `event_type="…"` and asserts each one is in the vocabulary. It misses f-strings, which is how `node.succeeded` escaped. forgeflow should use an enum or a constructor function per event type.
- **Read-time healing** (`refresh_active_workflow_status`) worked well in practice. The equivalent for forgeflow: `status` folds the log, optionally runs one reconcile tick, and appends any observations.

---

## 6. Agent-harness invocation

### 6.1 Claude Code (`tools/claude-brain/claude-brain-loop.sh:73-89`)

**argv is always built as an array** (`98bc6af9`, `6d9672cc`):

```
claude -p --mcp-config $CFG --strict-mcp-config --allowedTools "mcp__eleforge__*" \
  --disallowedTools "Bash,Edit,Write,NotebookEdit" --append-system-prompt "$PLAYBOOK" [--model M] "$PROMPT"
```

- The prompt is a positional argument.
- The process runs in a subshell after `cd "$BRAIN_HOME" || exit 1`. The isolated cwd prevents `CLAUDE.md` ancestor discovery.
- stdout and stderr are appended to a log file. **The result is never parsed**: the brain reports through MCP tool calls.

**For forgeflow `agent` nodes:**
- Add `--output-format stream-json --verbose` (the `--verbose` flag is required with `-p`; see `context/claude-code/src/cli/print.ts:787`).
- Add `--session-id <uuid>` for resume and replay (see `context/claude-code/src/bridge/sessionRunner.ts:287-303`).
- Have the agent write a declared result file, or call a `forgeflow` CLI/MCP tool, to commit outputs.

### 6.2 pi

**Command:** `pi -p -e eleforge-mcp-bridge.ts --no-context-files --provider P --model M:thinking --no-session "$PROMPT"` (`tools/pi-brain/pi-brain-loop.sh:111`).

**SDK daemon** (`pi-brain-service.mjs`):
- `createAgentSession({ resourceLoader: noContextFiles/noSkills, appendSystemPrompt:[playbook], sessionManager: inMemory, noTools:"builtin", customTools })`, with a fresh session per wake.
- `Promise.race` timeout (`TURN_TIMEOUT_S=600`).
- A `message_end` subscription detects `stopReason==="error"`, because `session.prompt` resolves even on provider errors. The daemon throws, then tries the next model candidate.
- `PI_MAX_OUTPUT_TOKENS` clamp.

### 6.3 Codex

- `codex exec "<prompt>"` with `codex mcp add`; the script was never committed (`doc/design/pi-agent-brain.md:47`).
- `codex exec -s read-only` served as a review gate (`doc/workspace/codex_gated_lanes_2026-06-16.md`).
- A capability-probe pattern (`--version`, `--help`, a test prompt, an auth check) is in `context/workflow-design/examples/cross-model-review.workflow.mjs:120-160`.

### 6.4 "Pluggable harness"

This exists only as design: `doc/design/pluggable-agent-harness.md:58-69` sketches `AgentRuntime` with `PiRuntime`, `CodexRuntime` and `ClaudeCodeRuntime`, plus credential paths A-D. Nothing was implemented. forgeflow's per-harness adapter (argv builder, result capture, session/resume, cost) is new work.

### 6.5 Contract patterns worth keeping

- A **versioned playbook** (`PLAYBOOK_VERSION`, served by a tool) pinned into the *system* prompt and refetched every cycle (`f4ff2672`: a tool-result playbook has the lowest precedence and conflicts with repo AGENTS.md/CLAUDE.md). Keep it under 40 KB; when a playbook rule grows, move it into a validator that returns structured 400s (`038d457c`).
- **Tool groups** with an `expand_toolset` meta-tool (`tool_groups.py`).
- **Server-side gates:**
  - permission: never trust the agent, and strip any permission it supplies (`3646e095`);
  - budget: check-then-increment per session and per message, returning a relayable 429-style result (`48160c39`);
  - dead-letter after N redeliveries (`ecf3ea20`);
  - leases plus idempotent ack (`bc5b6f3a`).

---

## 7. Real DAG shapes and the band hand-off gap

There are 60 registered templates (`apps/backend/functional/workflow_templates/`); the 48 that build with default params range from 1 to 23 nodes (median about 5). Typical shapes, from `build_template(id, {})`:
- `bulk_workflow`: relax → {scf, dos, bands}. These are **fan-out from relax with no scf→bands edge.**
- `band_structure_workflow`: relax → band. `phonon_workflow`: relax → phonon.
- `neb_workflow`: {relax_init, relax_final} → neb.
- `eos_workflow` / `convergence_workflow`: N independent single points → an aggregator. Sweeps are unrolled at build time.
- `research_pt_her_water_dissociation`: 17 nodes, two parallel chains (adsorption → dissociation → NEB; clean/H → single points → HER), plus dos → dband.

**Audit** (`doc/workspace/2026-06-28-dag-template-audit.md`; 106 agents, review plus adversarial verify): 53 templates, of which 3 broken, 15 needs-fix, 33 minor, 2 sound; 6 critical and 21 high issues.

**Band hand-off gap** (audit L61). `spectrum_bands_abacus` maps to ABACUS `calculation nscf` plus `init_chg file` (`abacus.py:101,257-259`), which needs the SCF charge density in its own `OUT.<suffix>/`. The DAG has **no scf→bands edge and no cross-node charge hand-off**, because each node is a separate job with a separate workdir. As a result the nscf finds no charge file.

**Implications for forgeflow:**
1. **Artifact hand-off between `job` nodes must be first-class.** An upstream output port can be a *remote directory or file reference* (path, hash, host) that the downstream job stages, by copy or symlink, into its workdir. Alternatively, an explicit "same-workdir chain" option.
2. Domain-heavy outputs are excluded from harvest on purpose (`*CHARGE-DENSITY*`, `*WAVEFUNC*`), which is right. But that means hand-off must happen **remote-to-remote by reference**, not by pulling files back.
3. **Other systemic audit classes:**
   - Binding/port contract mismatches (bindings to undeclared ports, or required ports left unbound). This argues for strict plan validation at approve time, with `PlanLoadError`-style path, code and suggestion.
   - Advertised outputs that the engine never produces. Validate declared outputs at commit time and fail loudly.
   - Physics defaults (U/spin off, PW vs LCAO mismatch across nodes). This is something an agent "plan review" gate can catch.

---

## 8. Hard-won lessons and pitfalls (do not relearn)

| Commit | Area | Lesson |
|---|---|---|
| `98bc6af9` | harness | `${VAR:+--flag "$VAR"}` expansion made `claude -p` fail intermittently ("Input must be provided"): an empty value collapsed the argv, and a 40 KB value word-split. **Spawn with an argv list, never a shell string.** Never run without the playbook. `cd` must succeed or the run aborts. |
| `6d9672cc` | harness | bash 3.2 with `set -u` and `"${EMPTY[@]}"` is an unbound-variable error. `claude` never started, backoff reached 300 s, and the process sat idle for an hour while stdout stayed empty. |
| `ecf3ea20` | harness | A message that is read but never acked was redelivered every 14 s for 40+ turns. Fix with a dead-letter after N deliveries. `session.prompt` resolves even when `stopReason=error`, so detect it. |
| `eb97122b` | harness | OpenRouter checks the balance against the reserved `max_tokens` (default 128k), so every turn returned 402 and the loop spun silently. Clamp output tokens. |
| `f32484ec`, `8b6d8bc4` | harness | Under TLS interception pi reports a bare "Connection error." (each turn spins exactly 19 s) while curl works. Set `NODE_EXTRA_CA_CERTS` to a system CA bundle. A debug mode that logs the stop reason found it. |
| `857d8856` | harness | A ChatGPT-account Codex backend accepts only certain model ids; others return 400, giving empty turns and a hot loop. Probe the model and treat 4xx as fatal, not retryable. |
| `89588927` | harness | The runtime-config fetch had no auth header (401), silently fell back to defaults, and spent tokens on the wrong model. Use exponential backoff keyed on cycle duration; never use `while true; sleep 1`. |
| `f4ff2672` | harness | A playbook delivered as a tool result loses to repo CLAUDE.md/AGENTS.md. Pin it into the system prompt and disable context-file discovery (`--no-context-files` or an isolated cwd). |
| `5d5e5e51` | harness | Polling `pi -p` per cycle spends an LLM call while idle. Use a zero-token wait plus a fresh bounded session per wake. SDK `dispose()` does not fire `session_shutdown`, which leaks connections. |
| `2b642e26`, `5696d980`, `pluggable-agent-harness.md:39-43` | auth | A Claude OAuth token used through a third-party harness is billed per token; only Claude Code itself uses plan quota. Reused CLI tokens are only a bootstrap: refresh rotates the token, re-seeding clobbered a fresh token with a dead one, and the CLI's own login broke. **Import must never overwrite by default.** |
| `8b6d8bc4` | harness | Downgrading multi-step tasks to a fast model caused 25+ idle rounds. Some task families must keep the strong model. |
| `bc5b6f3a` | concurrency | Two daemons double-processed a message because reads were non-destructive. Use advisory leases with a TTL plus an idempotent ack. |
| `3646e095` | gates | Enforce permissions on the server. HTTP-only resume endpoints deadlocked agent-driven runs, so every pause type needs a programmatic respond. |
| `038d457c`, `aa2595e9` | planning | Move wiring rules into validators that return structured `misalignments[{stepId,needs,upstreamProduces,hint}]`. Staged advancement: emit a segment, run it to terminal, review, then design the next. Repair failed steps in place; never rebuild the whole graph. |
| `2be9cd9d` | UX | 30-90 s waits with no streaming look hung. Post a status line and update it in place. |
| `dbb0fd2d` | ssh | `subprocess.run(timeout=)` does not bound ssh when a ControlPersist master inherits the pipes; `communicate()` blocks forever (18 jobs stuck running for an hour). Use `Popen(start_new_session)` + `killpg` + a bounded drain + `ssh -O exit`. |
| `e372517d`, `0d06b919` | ssh | A shared ControlMaster over a flaky relay wedges and hangs every later poll. **No multiplexing for automated BatchMode commands; ControlMaster only for interactive/MFA.** Do not null ControlPath globally, because MFA connect and teardown need it. |
| `8770c159` | ssh | Relayed-tunnel budgets: ConnectTimeout 30 s, keepalive 4×20 s, command timeout 60 s. Tighter values failed submits mid-way. |
| `c62bb9f0` | ssh | A submit that shares one session fails whole on a mid-transfer drop. Rebuild the session per retry, retry only transport errors, and make each attempt idempotent (marker + find-existing). |
| `0fb36657` | ssh | "Cluster offline" was really a wrong key file. Distinguish auth/key errors from unreachable. |
| `c8a5ba36` | harvest | Never synthesize empty success when the result is unreadable: defer, then fail after N tries. A dead PID with no exit code means LOST, confirmed over 3 polls. Reconcile per connection in parallel. |
| `9a592618` | watcher | Jobs finished for days but never went terminal because no watcher ran (the role default disabled it). Watcher liveness must be explicit and visible (ticksTotal was 0). |
| `ec3d1b37`, `31d458d5` | slurm | Without slurmdbd a finished job vanishes from squeue and sacct, and so does a canceled one. Workdir markers (`result.json`, `canceled`) are the fallback truth. |
| `7a0b4f2a` | slurm | Threaded engines need `--cpus-per-task` + `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK`; otherwise they run single-threaded and hit the walltime. |
| `9a1eaa55` | config | The reader accepted only camelCase `cpusPerTask` while the writer used snake_case, so the setting was silently dropped. Validate config strictly, or accept both forms explicitly. |
| `230dc7c7`, `5cc234cf` | slurm | Pure resolve → emit, with one formatter shared by preview and submit, so the preview cannot drift from what is submitted. |
| `af9cd6d7` | progress | Use short-lived `tail -c +offset` polls with the offset tracked locally, not a long-lived channel. Explicit progress lines beat log parsing. |
| `13f6c842` | rerun | A remote node rerun must re-dispatch on the inherited queue, never inline. Reopen the run, and collapse to the latest attempt per node, or stale attempts wedge finalize. |
| `29f1b08f` | DAG | Fail-fast threw away hours of good DFT output. Skip only transitive descendants, keep independent branches, and allow partial success; optional outputs become absent. |
| `8e0d21b5`, `8ee235cd` | rerun | Rerun inputs come from committed upstream outputs. Each attempt is a new record. For the fail→fix diff, use the immediately previous attempt, not the oldest. |
| `1734fdf2` | pause | `suspended→succeeded` is illegal, and the ValueError thrown inside ingest left runs suspended forever. Skip finalize while paused and re-finalize on resume. |
| `536e6886` | cancel | A UI-only cancel left the cluster burning. Cancel must cascade to jobs, terminalize nodes, and append the completed event. Replay re-emitted checkpoints, so gate them on idempotency. Late non-terminal syncs must not downgrade a terminal state. |
| `2bd03af1` | fan-out | Write the manifest before submitting. Look up by type with no window limit, and order by sequence number, not timestamp. Finalize needs a single-winner lock. Label-scoped reruns must not touch other items. |
| `bbf97142` | gates | An open interaction hidden by payload elision or tail windows parked runs forever. Always surface open gates. |
| `75ad4ad6`, `d9193efb` | idempotency | Set the "terminal handled" guard synchronously before any await (completion fired twice). Two writers mapping pause states differently flip-flopped every tick, so keep a single writer. |
| S3a.2-S3a.11 (`doc/workspace/2026-04-24-s3-…md`) | event log | Dual-write diverges in many small ways. Transition events must carry full snapshots; the plan must actually be saved; empty-envelope semantics must match. **One writer, one store.** |
| audit 2026-06-28 | templates | No cross-job charge-density hand-off (nscf). Bindings were made to undeclared ports. Advertised outputs were never produced. Validate the contract at approve time and outputs at commit time. |

---

## 9. Concrete forgeflow plan derived from this inventory

1. **`forgeflow/store`**
   - Start from `LF/repository.py` + `LF/events.py`.
   - Add a `flock` on `events.jsonl` and a monotonic `seq`.
   - Fail closed on append errors.
   - A corrupt line other than the last one is fatal.
   - Plan versions are content-hashed with `template_cas.canonical_serialize`.
2. **Event vocabulary**
   - Start from LabFlow's 27 types.
   - Add `node.succeeded`, `node.skipped`, `node.canceled`, `node.invalidated`, `plan.amendment.{proposed,approved,rejected,applied}`, `gate.{requested,resolved}` (reuse `approval.*`), `agent.decision`, `agent.output.recorded{sessionId, resultHash, transcriptRef}`, and `external.state_observed{raw, mapped}`.
3. **State machine**
   - Lift the node table.
   - State is tracked per attempt; a node's state is that of its latest non-superseded attempt.
   - Rerun+downstream appends `node.invalidated` for descendants plus a new `pending` attempt for each.
   - Add the submit axis for jobs.
   - Leases get a TTL.
4. **Job backend**: lift `openssh.py`, `shell.py`, `command.py` and `errors.py`. Adapt the sbatch emitter, idempotent submit, batched poll, workdir fallback and cancel marker. Then make four fixes:
   - a total Slurm state map with `--parsable`;
   - `ExitCode` from sacct;
   - a LOST watchdog for Slurm;
   - observation counters stored as events.
5. **Tick**: a stateless `forgeflow tick` (cron, systemd, or an outer agent loop) that folds the log, polls jobs batched per connection in parallel, harvests with defer/N-miss, and dispatches ready nodes. No resident daemon is required.
6. **Agent nodes**
   - argv array, isolated cwd, `--strict-mcp-config`, allow/deny lists, a system-prompt-pinned contract, `stream-json` capture, session ids, and a declared result file.
   - Detect error stop reasons, clamp tokens, back off keyed on duration, and fail fast on auth/4xx.
   - Never clobber credentials.
7. **Validation**: `PlanLoadError`-style `{code, path, message, suggestion}` for the plan, bindings and ports at approve time. Output-contract checks at commit time.
