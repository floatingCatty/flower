# Agent-workflow runners and the pi harness: source-level digest for flower

Date: 2026-10-02. Scope: source-level reads of the shallow clones under `flower/context/repos/`: `archon` (coleam00/Archon @ d26e3cd), `orc` (tjdals12/orc v1.3.1 @ fe58b1c), `yak` (lchase/yak v0.5.0 @ fcd5825), `gh-aw` (github/gh-aw @ de6e5b9) and `pi` (earendil-works/pi @ a276dab). I also cross-checked the Smithers 0.x CLI-agent adapters (`repos/smithers-0.x/packages/agents/src/`), which fill a gap left open by the prior-art survey. Background reading: `notes/prior-art-survey/closest_prior_art_deep_dive.md`.

File paths are relative to each repo root unless stated otherwise. "Verbatim" marks text copied from source. Unmarked text is my summary.

**Corrections to the prior-art survey that came from reading the source:**
- **yak**
  - `yak replay --from` and `yak run --from` appear in `spec.md` §4.4/§6 only. The CLI (`src/cli/index.ts`) implements `run`, `resume`, `cancel`, `status`, `pending`, `graph`, `watch` and `artifacts`, with no `--from` and no `replay`.
  - Agent outputs are stored inline in the cache entry JSON (`.yak/cache/<semanticKey>.json`, field `artifact`) and in `artifacts/`. The journal carries only hashes.
- **orc**
  - It has **no tests**.
  - It has no JSONL log: events are SQLite rows.
  - It captures no cost or tokens and has no per-node timeouts.
  - It records session ids but never uses them to resume.
  - Its `stop` runs `git reset --hard` and `git clean -fd`.
- **pi**
  - The SDK auth surface is now `ModelRuntime` (`authPath`, `modelsPath`), not `authStorage`.
  - In `--mode json`, pi exits 0 even when the agent errored, so the caller has to inspect `stopReason`.
  - Print and json modes have no SIGINT handler. Cancel with SIGTERM.

---

## 1. yak (lchase/yak)

**Positioning.** yak describes an agentic workflow engine as a "build system whose compilers are nondeterministic" (`spec.md` §1). Its principles include "Everything on disk, everything greppable. No hidden state in a database." It is the closest prior art to flower's storage philosophy.

### 1.1 Workflow file format

The authoring format is YAML. It is normalized into an IR (`src/ir/load.ts` → `normalize.ts` → `validate.ts`), and the engine reads only the IR. The schema below is verbatim from `src/ir/types.ts`, which is authoritative and newer than `spec.md` §13; comments are trimmed.

```ts
export type Expr = string | { fn: string }          // jexl expression, or named fn in .yak/predicates.ts
export interface Budget { maxIterations: number; maxTokens?: number; maxUsd?: number
                          noProgress?: { signal: Expr; rounds: number } }
export interface BaseStep {
  id: StepId
  needs?: ArtifactName[]      // edges are ARTIFACT names, not step ids
  produces?: ArtifactName     // exactly one artifact per step
  cache?: 'strict' | 'loose'  // default 'strict'
  skipIf?: Expr               // command/transform/agent: skip entirely; gate: auto-answer from schema defaults
  finally?: boolean           // eligible once every needs-producer has *settled* (completed OR failed)
}
export interface AgentStep extends BaseStep { kind: 'agent'
  prompt: { file: string } | { inline: string }
  schema?: string | { inline: JSONSchema }   // key in .yak/schemas.ts (Zod) or inline JSON Schema (ajv)
  context?: 'fresh' | { inherit: ArtifactName[] } | { session: StepId }
  tools?: string[]; model?: string; repairAttempts?: number   // default 2
  sandbox?: 'docker' | 'none'; image?: string }
export interface CommandStep extends BaseStep { kind: 'command'
  run: string; cwd?: string; failOn?: 'exitCode' | 'never'   // default 'exitCode'
  capture?: ('stdout' | 'stderr' | 'exitCode')[]; idleTimeoutMs?: number
  sandbox?: 'docker' | 'none'; image?: string }
export interface TransformStep extends BaseStep { kind: 'transform'; fn: string }   // key in .yak/transforms.ts
export interface GateStep extends BaseStep { kind: 'gate'
  schema: string | { inline: JSONSchema }; render: { file: string } | { inline: string } }
export interface MapStep extends BaseStep { kind: 'map'; over: ArtifactName; step: Step
  concurrency?: number; isolation?: 'worktree' | 'none'; onItemFailure?: 'skip' | 'fail' | 'retry' }
export interface LoopStep extends BaseStep { kind: 'loop'; body: Step[]; until: Expr; budget: Budget
  onExhausted?: 'suspend' | 'fail' | 'continue'   // default 'suspend'
  freshContext?: boolean }                         // default true
export interface Workflow { name: string; version: string; inputSchema?: string | { inline: JSONSchema }; steps: Step[] }
// reserved artifact `input` written from `yak run --input k=v` before any step runs
export interface StepFailure { reason: 'needs-decision'|'needs-context'|'schema-invalid'|'budget-exhausted'
  |'tool-denied'|'adapter-error'|'command-failed'|'timeout'|'sandbox-error'; detail: string; recoverable: boolean }
```

**How a step is written in YAML.** The kind is a nested key: `command: {run: ...}`, `agent: {...}`, `gate: {...}`, `loop: {...}`, `transform: {fn}`. The fields `id`, `needs` and `produces` sit beside it.

**Other conventions**
- **Prompt templating** (`src/expr/template.ts`): `{{ jexl-expr }}` placeholders are evaluated against the input artifacts. A placeholder that resolves to `undefined` is an error. The load-time check is that every template root must be in `needs ∪ context.inherit`.
- **Loops cannot be back edges.** The graph is acyclic by construction (§4.3); `loop` is a bounded combinator.
- **Load-time rule** (§9 #6): reading `<step>.exitCode` while that step has `failOn: exitCode` is an error.

**Representative example** (`fixtures/loop-demo/workflow.yaml`, abridged verbatim):
```yaml
name: loop-review-revise
version: "1"
steps:
  - id: fix-until-approved
    loop:
      until: "review.approved == true"
      budget: { maxIterations: 5, noProgress: { signal: "testResult.numFailedTests", rounds: 2 } }
      body:
        - id: implement
          needs: [priorFeedback]
          agent:
            prompt: { file: "fixtures/loop-demo/prompts/implement.md" }
            schema: { inline: { type: object, properties: { summary: { type: string } }, required: [summary] } }
            tools: [Read, Edit]
          produces: implementResult
        - id: test
          needs: [implementResult]
          command: { run: "npx vitest run --reporter=json --root fixtures/loop-demo", capture: [stdout, exitCode], failOn: never }
          produces: testRaw
        - id: review
          needs: [testResult]
          agent: { prompt: { file: ".../review.md" }, schema: {...approved, feedback...}, tools: [Read] }
          produces: review
```
Gate example, from `test/workflows/gate-suspend.yaml`:
`gate: { schema: { inline: { type: object, properties: { decision: { enum: [approve, reject] }, notes: {type: string} }, required: [decision] } }, render: { inline: "Approve this change?" } }`.

### 1.2 Harness invocation

**Adapter interface** (`src/adapters/types.ts`, verbatim):
```ts
export interface AgentAdapterRequest { prompt: string; systemPrompt?: string; tools: string[]; cwd: string
  model?: string; schema?: unknown /* JSON Schema */; sessionId?: string /* resume */; signal: AbortSignal }
export interface AgentAdapterResponse { output: unknown /* parsed if schema given, else string */
  sessionId: string; tokens: { input: number; output: number }; filesChanged: string[]
  stopReason: 'complete' | 'max_turns' | 'error' | 'aborted' }
export interface AgentAdapter { id: string; run(req: AgentAdapterRequest): Promise<AgentAdapterResponse> }
```

**Claude adapter** (`src/adapters/claude-code.ts`). It uses the in-process Claude Agent SDK `query()`, not `claude -p`.
- **Options:**
  - `permissionMode: 'bypassPermissions'` and `allowDangerouslySkipPermissions: true` are fixed engine-wide. The stated reason: "agent steps have no channel to ask a human".
  - `tools` maps to the SDK's availability allowlist, not to `allowedTools`.
  - `resume: sessionId` when the step has `context: {session}`.
  - `outputFormat: {type:'json_schema', schema}` when the step has a schema.
  - `abortController`, `cwd`, `model`, `systemPrompt`.
- **Transcript:** each raw SDKMessage is appended verbatim to `sessions/<step>.jsonl`.
- **Output:** `result.structured_output ?? result.result`.
- **Tokens:** from `result.usage.input_tokens`/`output_tokens`. These are journalled as `budget.consumed`; usd is optional and not filled.
- **`filesChanged`:** scraped from `Edit`/`Write`/`NotebookEdit` `tool_use.input.file_path`.
- **Error mapping:**
  - `permission_denials` non-empty → `tool-denied`
  - `error_max_budget_usd` → `budget-exhausted`
  - `error_max_turns` / `error_max_structured_output_retries` → non-recoverable `adapter-error`
- **Docker sandbox option:** `spawnClaudeCodeProcess` is replaced by `docker run --rm -i --network <net> -v cwd:/workspace -e HTTPS_PROXY=... -e ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN <image> /app/claude-cli <sdk args>`. Network egress goes through a squid proxy allowlist (`docker/agent-proxy/`).
- **Mock adapter** (`src/adapters/mock.ts`): canned responses come from `test/fixtures/<workflow>/<step>[.<iteration>]`. The spec says to build this second, before the real adapter.

**Schema repair loop** (`src/steps/agent.ts`).
- If the output fails the schema, yak re-prompts in the **same session**. It appends a suffix: "Your previous output did not match the required schema.\nSchema:\n…\n\nValidation errors:\n…\n\nReturn output that satisfies the schema."
- It does this up to `repairAttempts` times. The final rejected output goes to `artifacts/.rejected/<step>.<n>.txt`, and the failure reason is `schema-invalid`.

**Isolation, timeouts and cancellation**
- `--isolation worktree` creates `.yak/worktrees/<run-id>/` on branch `yak/<run-id>`. A `map` can isolate each item in a sibling worktree.
- `command` supports `idleTimeoutMs`, measured from the last output line: SIGTERM, then SIGKILL. Agent steps have no timeout.
- Cancellation is described in §1.4.

**Side-effect rule** (`spec.md` §5.1, table verbatim):

| Effect | Owner |
|---|---|
| Read, edit, run tests, build, lint, local git commit | Agent step, inside the sandbox |
| `git push`, `gh pr create`, Slack post, deploy, any shared-state mutation | `command` step, run by the engine outside the sandbox, gated |

### 1.3 State and events

**Run directory** (`spec.md` §4.1; implemented in `src/engine/run.ts`, runs at `<cwd>/.runs/<run-id>/`):
```
workflow.json        # frozen IR of the graph as executed
journal.jsonl        # append-only event log — source of truth
artifacts/<name>.json  (loop/map: <name>.<iteration>.json), artifacts/.rejected/
pending/<step>.request.json | <step>.answer.json
sessions/<step>.jsonl  # raw adapter transcript, debugging only
```
The global cache is `.yak/cache/<semanticKey>.json`. Entries are written atomically (tmp file plus `rename`) and hold `{semanticKey, definitionKey, artifactName, artifactHash, artifact}`, so the artifact value is stored inline (`src/engine/cache.ts`).

**Journal event union** (`src/ir/types.ts`, verbatim). Every event is wrapped as `{...event, at: ISO, runId}`:
```ts
| { t: 'run.started';      runId; workflow; inputHash; adapter: AdapterId; isolation: RunIsolation; tag?: string; pid?: number }
| { t: 'step.started';     stepId; iteration?; semanticKey: string; definitionKey: string }
| { t: 'step.completed';   stepId; iteration?; artifact?; artifactHash?; cached: boolean; stale?: boolean; skipped?: boolean }
| { t: 'step.failed';      stepId; iteration?; failure: StepFailure }
| { t: 'artifact.written'; name; hash: string; bytes: number }
| { t: 'budget.consumed';  stepId; tokens: number; usd?: number }
| { t: 'loop.iteration';   stepId; n: number; signal?: unknown }
| { t: 'map.item.retried'; mapStepId; itemIndex: number; attempt: number; error: string }
| { t: 'gate.opened';      stepId; requestPath: string }
| { t: 'gate.answered';    stepId; skipped?: boolean }
| { t: 'run.suspended';    reason: 'gate' | 'budget' | 'exhausted'; loopStepId?; iteration?; tripped?: 'maxIterations'|'maxTokens'|'noProgress' }
| { t: 'run.resumed';      pid: number }
| { t: 'run.finished';     status: 'ok' | 'failed' | 'suspended'; reason?: 'cancelled' }
```
The writer (`src/engine/journal.ts`) is a plain `appendFile` of `JSON.stringify(envelope)+"\n"`, with no fsync and no lock.

**Replay** (`completedStepsFromJournal`).
- A later `step.started` for an id resets that step's completion. `step.failed` removes it.
- A step that has `step.started` with no later terminal event counts as interrupted, not completed.

**Cache keys** (`src/engine/cache.ts`, verbatim doc):
- `semanticKey = sha256(step id + input artifact hashes + adapter id + model id [+ iteration/map index])`
- `definitionKey = sha256(prompt + tools + schema + budget + engine version)`

Under `strict` (the default) a mismatch on either key re-runs the step. Under `loose`, yak reuses the artifact when only `definitionKey` moved and journals `stale: true`. Prompts belong in `.md` files, and the spec warns: "Never interpolate churn — dates, run ids, hostnames, absolute paths — into a prompt template."

**How resume decides what to skip** (`src/engine/scheduler.ts`, `trustResumedSteps`):
1. Replay the journal to get the completed set.
2. Walk to a fixpoint. A replayed step is trusted only if all its dependencies are trusted **and** its recomputed keys match the journalled keys.
3. Gates that the journal shows answered are trusted unconditionally.
4. Anything untrusted re-runs, which cascades downstream through artifact hashes.

**Frozen spec.** `workflow.json` holds the frozen IR. On resume, the adapter must match the one the run started with.

### 1.4 Control verbs and gate mechanics

CLI (`src/cli/index.ts`):
- `run <wf> [--adapter mock|claude-code] [--interactive] [--isolation worktree|none] [--input k=v...] [--tag s]`
- `resume <run-id> [--adapter]`
- `cancel <run-id>`
- `status [run-id]`, `pending`, `graph <wf>` (Mermaid), `watch [run-id]` (Ink TUI tailing the journal), `artifacts [run-id]`

`run` prints `run <id> started` to stderr as soon as `run.started` is journalled, so a launcher can capture the id without waiting.

**Gate protocol** (`src/engine/suspend.ts`)
1. The gate writes `pending/<step>.request.json`. The request is a discriminated union, verbatim:
   ```ts
   { kind:'gate', stepId, runId, rendered, answerSchema, context:{artifacts:[...needs]}, openedAt }
   { kind:'loop-exhausted', stepId, runId, tripped, iteration, answerSchema, openedAt }
   // fixed answer schema for exhaustion: { action: 'continue'|'abort', addIterations?: number }
   ```
2. It journals `gate.opened`, then `run.suspended`, and the process **exits 78** (`EX_SUSPEND`).
3. Any frontend writes `pending/<step>.answer.json`.
4. `yak resume` validates the answer against the schema. If it is missing or invalid, the run stays suspended. If valid, the answer becomes the gate's artifact and `gate.answered` is journalled.
5. Open gates are computed from the journal, not from which files exist ("the journal is authoritative, not file presence on disk").
6. `yak pending` lists open gates across every run in the repo.
7. `--interactive` writes both files itself, inline.

**Cancel** (`src/engine/cancel.ts`)
- It reads the live pid from the last `run.resumed`, or else from `run.started`.
- It sends SIGTERM to the **process group** (`-pid`), waits a 5 s grace period, then sends SIGKILL.
- It journals `run.finished{status:'failed', reason:'cancelled'}` unless the process already wrote a terminal event.
- It is idempotent.
- Known limitation: pid reuse.

**Spec ideas not yet built**
- `--from <step>`.
- "Demotable gates": collect labelled gate answers, then a predictor runs in shadow mode and is promoted to `skipIf` (`spec.md` §9 #10).

### 1.5 Outer-agent surface
There is no skill file and no MCP server; agents use the CLI. A separate `yak-harness` package (`specs/yak-harness.md`, moved to `lchase/yak-harness`) consumes yak "only through the documented CLI and the on-disk journal / `pending/` contract". That is the same decoupling flower wants.

### 1.6 License, size, tests
- MIT (`LICENSE`, © 2026 Lawrence Chase). Node ≥22.
- Dependencies: `@anthropic-ai/claude-agent-sdk`, `ajv`, `commander`, `ink`, `jexl`, `p-limit`, `yaml`, `zod`.
- `src/`: 5,286 LOC of TypeScript. `test/`: 5,340 LOC across 39 test files, with **289** `it/test` cases under vitest. The mock adapter makes the suite deterministic.

---

## 2. orc (tjdals12/orc)

### 2.1 Workflow file format

**Location.** `<project>/.orc/workflows/<id>.yml`, where `id` must equal the file basename. The schema is a strict zod object (`src/workflow/schema.ts`, `src/workflow/node/schema.ts`), so unknown keys are rejected.

```ts
WorkflowSchema = z.strictObject({ version: int<=1, id: /^[a-z0-9_-]+$/, description: string,
  input: z.strictObject({ required: boolean, description: string }).optional(),   // ONE free-text $INPUT
  nodes: z.array(WorkflowNodeSchema).min(1) })
WorkflowNodeBase = { id, depends_on?: id[], produces?: artifactName[], consumes?: artifactName[] }
bash     = { type:'bash', script }
agent    = { type:'agent', prompt, loop?: { completion_signal? | completion_bash?, max_iterations },
             provider:'claude', model, options?:{effort?, max_turns?}
           | provider:'codex',  model, options?:{model_reasoning_effort?}
           | provider:'grok',   model, options?:{reasoning_effort?, max_turns?} }
approval = { type:'approval', message, on_reject?: {type:'bash', script} | {type:'agent', provider, model, prompt} }
```

**Missing fields.** There is no `cwd`, `env`, timeout, retry, condition or fan-out, and the prompt cannot come from a file.

**Tokens** (`src/workflow/node/text-token.ts`).
- Four tokens: `$INPUT`, `$ARTIFACTS_DIR`, `$ARTIFACT(name)` and `$REASON` (the last only inside `on_reject`).
- `$ARTIFACT(x)` inlines the file only if it is UTF-8, non-empty and at most 2000 chars. Otherwise the node fails with "Name the path in the prompt instead".
- Bash nodes get the values as env vars `INPUT` and `ARTIFACTS_DIR`.

**Artifact checks.** Artifacts are files in a flat per-run directory. `consumes` must exist before a node starts and `produces` must exist after it succeeds; otherwise the node fails. `consumes` does **not** imply ordering, so the node must also declare `depends_on`.

**Loop mechanics.**
- Each iteration starts a fresh session.
- `completion_bash`: exit 0 means done, exit 1 means continue, anything else is an error.
- `completion_signal` is matched in assistant prose: tag-wrapped, at the end of the text, or on its own line.
- Exceeding `max_iterations` fails the node.

**Example** (`skills/orc/examples/plan-and-implement.yml`, verbatim minus comments):
```yaml
version: 1
id: plan-and-implement
description: Plan a change, implement it, and verify the result
input: { required: true, description: What to build, or a summary of the conversation that decided it }
nodes:
  - id: plan
    type: agent
    provider: claude
    model: sonnet
    prompt: |
      **Request**: $INPUT
      Explore the codebase and write an implementation plan to
      $ARTIFACTS_DIR/plan.md. List the files to touch and the order to touch
      them in. Do not change any code.
    produces: [plan.md]
  - id: implement
    type: agent
    provider: claude
    model: sonnet
    depends_on: [plan]
    consumes: [plan.md]
    prompt: |
      Implement this plan:
      $ARTIFACT(plan.md)
      ... write a one-paragraph summary of what changed to $ARTIFACTS_DIR/summary.md.
    produces: [summary.md]
  - id: verify
    type: bash
    depends_on: [implement]
    script: |
      pnpm typecheck
      pnpm lint
```
**Approval with rework** (from the README):
```yaml
- id: review-gate
  type: approval
  depends_on: [plan]
  consumes: [plan.md]
  message: |
    Review the plan before the implementation starts.
    $ARTIFACT(plan.md)
  on_reject:
    type: agent
    provider: claude
    model: sonnet
    prompt: |
      $REASON
      Revise plan.md to address the feedback.
```

### 2.2 Harness invocation (exact argv, verbatim from `src/workflow-run/agent-node/*/runner.ts`)

**Spawning.**
- Every harness is spawned with `spawn(bin, args, {cwd, detached:true})`, so each child gets its own process group. No `env` is passed; the child inherits the parent's.
- A CLI version check runs first, giving `not-found`, `too-old`, `unsupported-major` or `check-failed`.

**claude** (the prompt goes on stdin as one stream-json user message, then stdin is closed):
```ts
['-p','--verbose','--input-format','stream-json','--output-format','stream-json','--model',model,
 '--permission-mode','bypassPermissions','--allow-dangerously-skip-permissions','--setting-sources','project',
 /* + '--effort', e */ /* + '--max-turns', n */]
// stdin: {"type":"user","message":{"role":"user","content":prompt},"parent_tool_use_id":null,...}
```
- Events consumed: `system/init` (`session_id`, `model`), `assistant` (text/tool_use), `user` (tool_result), `result{subtype,is_error,result,errors}`.
- Success requires `subtype==='success' && !is_error && exit 0`.

**codex** (prompt as raw text on stdin, with `-` as the prompt argument):
```ts
['exec','--json','--model',model,'--sandbox','danger-full-access','--cd',cwd,'--skip-git-repo-check',
 '--config','approval_policy="never"','--config','sandbox_workspace_write.network_access=true',
 /* + '--config', `model_reasoning_effort="..."` */ '-']
```
- Events consumed: `thread.started{thread_id}`, `item.started|item.completed` (with `agent_message{text}`, `command_execution{exit_code,aggregated_output}`, `file_change{changes[{path,kind}]}`), `turn.completed`, `turn.failed{error.message}`, `error`.

**grok:**
```
--no-auto-update --cwd C --model M --permission-mode bypassPermissions --sandbox off --no-plan --output-format streaming-json -p <prompt>
```

**Output capture.** Nothing is captured as the node's value.
- Data passes **only through files the agent writes to `$ARTIFACTS_DIR`**.
- Stream items become `agent_output` log rows, with previews truncated to 200 chars.
- The last message is used only for completion-signal detection.

**Tokens, cost and sessions.**
- No tokens or cost are captured. The usage in the claude `result` event is ignored.
- Session and thread ids are recorded (`agent_session_started`) but never used for resume.

**Cancel** (`src/shared/process-group-registry.ts`): the SIGINT goes to the child process; after 5 s, SIGTERM goes to the process group (`-pid`); after another 5 s, SIGKILL. The pgids are persisted in the `workflow_run_node_process_groups` table, so a separate `__stop-finalizer` process can kill orphans after the worker has died.

**Timeouts.** There are no wall-clock timeouts, only `max_turns` and `max_iterations`.

**Worktrees.**
- Each run creates one worktree with `git worktree add <~/.orc/projects/<pid>/<runId>/worktree> -b orc/<workflowId>-<runId[:8]> HEAD`. All nodes in the run share it.
- `--no-worktree` runs in place.
- `.orc/config.yml` sets `worktree.include`/`exclude` globs for copying gitignored files, plus hooks (`post-create`, `pre-remove`, `post-remove`).
- `run.max_concurrent_nodes` defaults to 2.

### 2.3 State and events

**Database.** SQLite at `~/.orc/orc.db` (WAL, kysely migrations in `src/database/migrations/`). Tables:
- `projects`, `execution_environments(kind in-place|worktree, path, branch)`
- `workflow_runs(id, project_id, workflow_id, input, status, pid, started_at, finished_at)`
- `workflow_run_nodes(run, node_id, position, status, attempt, message, reason, pgid)`
- `workflow_run_events(run, sequence, type, node_id, data JSON)`
- `workflow_run_node_logs(run, sequence, type bash_output|agent_output, node_id, data)`
- `workflow_run_hook_logs`, `workflow_run_node_process_groups`

Events and logs share **one per-run monotonic `sequence`**.

**Statuses**, verbatim:
- Run: `pending|running|stopping|stopped|stop_failed|paused|succeeded|failed|cancelled`
- Node: `pending|running|stopped|awaiting_decision|rejected|succeeded|failed`

**Event types**, verbatim: `node_started node_succeeded node_failed node_stopped agent_session_started iteration_started iteration_completed decision_requested decision_approved decision_rejected run_started run_succeeded run_failed run_cancelled run_stop_requested run_stopped run_stop_failed run_resumed run_paused worktree_creating files_copying hook_started`.

**Frozen spec per run.** `~/.orc/projects/<pid>/<runId>/spec/workflow/<id>.yml` plus `spec/config.yml` and copied hooks. README: "A run — and a resume — works from these copies, not the originals."

**Process model.** There is no daemon. Each `run`/`resume` spawns a detached `node … workflow __worker <runId>` and calls `unref()`.
- The worker claims the run with a compare-and-set: `pending→running, pid`.
- The foreground CLI only *follows* by polling the DB. Pressing Ctrl-C prints "workflow continues in background".
- A worker is considered alive if `kill(pid,0)` succeeds and `started_at` is after the host boot time.
- Control is DB-polled every 2 s: status `cancelled` or `stopping` aborts the run.

**Resume.**
- A node is skipped **only if its status is `succeeded`**. There is no hashing.
- Other non-terminal nodes are reset to `pending` with `attempt+1`.
- Resume is refused while a live worker exists or while orphan pgids remain.
- **There is no per-node rerun verb.**
- `stop` is destructive: it runs `git reset --hard HEAD && git clean -fd` and deletes the interrupted nodes' `produces`.

### 2.4 CLI surface and gate mechanics

The `orc workflow` subcommands:
- `list`, `validate <id> --json`
- `run <id> [--input|--input-file] [--no-worktree] [--base] [--branch] [--detach] [--json]`
- `resume <run-id>`, `approve <run-id> <node-id>`, `reject <run-id> <node-id> [--reason]`
- `approvals <run-id>`, `events`, `logs [node]`, `hook-logs`, `stream -f`, `status`, `runs`
- `cancel`, `stop`, `prune`

Other commands: `orc setup|doctor|auth|project add|skill install|hook run`. Most verbs take `--json`.

**Gate mechanics.**
1. When an approval node is reached, its `message` is rendered and stored, and the node is marked `awaiting_decision` with a `decision_requested` event. The other branches drain, the run becomes `paused`, and **the worker exits**.
2. `approve` does a compare-and-set to `succeeded` and writes `decision_approved`. It does **not** continue the run; README: "records the decision only — continue the run with resume".
3. `reject` without `on_reject` ends the run as `cancelled`.
4. `reject` with `on_reject` sets the node to `rejected` and stores the reason. The next `resume` runs the rework body with `$REASON`, then reopens the gate. Rework rounds are unlimited.

### 2.5 Outer-agent surface: `skills/orc/SKILL.md` (419 lines) plus `references/` and `examples/`

**Frontmatter**, verbatim:
```yaml
name: orc
description: "Runs and writes orc workflows. Use when the user wants work done by an orc workflow, asks what a run is doing
  or wants to cancel or resume it, needs to decide a run's approval request, or wants a workflow written or fixed.
  Not for doing the work directly: this skill delegates to the orc CLI."
compatibility: Requires the orc CLI.
metadata: { version: '5' }
```

**Sections.** A "Pick a flow" router, then: Run a workflow · Watch and control a run · Handle an approval request · Write a workflow · When orc is not set up. Each has numbered Steps with JSON samples, followed by Guardrails.

**Key rules.**
- Write a self-contained brief to a file, then run `orc workflow run <id> --input-file … --detach --json`.
- Run `stream -f` in the background: "Do not poll".
- **"The decision is the user's. Never run `approve` or `reject` on your own judgment; a gate an agent waves through protects nothing."**
- Prompts written for nodes must not leak orc concepts.
- Iterate on `validate --json` until the workflow is valid.

**Installation.** `orc project add` copies the skill into both `.claude/skills/orc` and `.agents/skills/orc`. Staleness is detected through `metadata.version`.

### 2.6 License, size, tests
- MIT (© 2026 Seongmin Lee). Node ≥24.
- `src/`: 17,749 non-test LOC across 212 files.
- **Zero test files.** CI runs only lint, typecheck and build.

---

## 3. Archon (coleam00/Archon)

Archon is a Bun/TS monorepo at v0.11.1. The engine lives in `packages/workflows/src/`; its core is `dag-executor.ts`, which is 11.8k lines. The provider contract is in `packages/provider-contract/src/`, and the providers are in `packages/providers/src/{claude,codex,community/pi}/`. Persistence is in `packages/core/src/db/` and the CLI in `packages/cli/src/commands/workflow.ts`. The docs are `packages/docs-web/src/content/docs/guides/authoring-workflows.md`, which runs to 3.3k lines.

### 3.1 Workflow file format

**Top level.** The schema is `packages/workflows/src/schemas/workflow.ts:176`, verbatim:
```ts
workflowBaseSchema = z.object({ name, description, provider?, model?, modelReasoningEffort?, webSearchMode?,
  interactive?: boolean, effort?, fallbackModel?, betas?, sandbox?, worktree?: { enabled? },
  container?: { enabled?, write_back?: 'approve'|'auto' }, evidence_policy?: { required },  // $ARTIFACTS_DIR/evidence.json must exist
  mutates_checkout?: boolean, persist_sessions?, tags?, requires?: ['github'],
  inputs?: Record<string, { required?, default?, description? }>,
  returns?: string /* node id whose output IS the workflow result */, outcome_field?: string, deprecated? })
  .extend({ nodes: z.array(dagNodeSchema) })
```
- Unknown keys trigger a warning and are stripped, and a `workflow_parse_warnings` event is recorded.
- The file layout is `.archon/workflows/<pack>/<wf>/<name>.yaml`, with sibling `commands/` and `scripts/` directories. Repo-level workflows override those in `~/.archon/workflows/`.

**Node base** (`schemas/dag-node.ts:192`, verbatim fields):
- **Graph and conditions:** `id, description?, depends_on?, when?, trigger_rule?`.
- **Provider and model:** `model?, provider?`.
- **Context:** `context?: 'fresh'|'shared'|{resume: nodeId}`.
- **Output:** `output_format?: JSONSchema`.
- **Tools and limits:** `allowed_tools?, denied_tools?, idle_timeout? (ms), retry?`.
- **Harness add-ons:** `hooks?, mcp? (path to MCP JSON), skills?, plugins?, agents?, pi?: {enableExtensions?, interactive?, extensionFlags?}`.
- **Agent tuning:** `effort?, maxBudgetUsd?, systemPrompt?, fallbackModel?, settingSources?, betas?, sandbox?`.
- **Run behaviour:** `always_run?, mutates_checkout? (false ⇒ snapshot git tree, fail if changed), persist_session?, output_type?`.

Related enums and policies:
- `trigger_rule`: `all_success` (the default), `one_success`, `none_failed_min_one_success`, `all_done`.
- `retry`:
  - Shape: `{max_attempts 1..5, delay_ms 1000..60000, on_error: 'transient'|'all'}`.
  - AI nodes retry twice by default, with a 3 s exponential backoff. Bash nodes retry only when `retry` is set.
  - The failure classes `auth`, `quota_exhausted`, `budget_exceeded` and `misconfigured` are never retried.

**Node types.** A node carries exactly one body key, and a transform turns it into an internal `kind`:

| YAML key | Internal kind | Notes |
|---|---|---|
| `command: <name>` | `agent` | Body comes from `commands/<name>.md`, with `with:` bindings |
| `prompt: <text>` | `agent` | Inline prompt |
| `bash: <script>` | `exec`, runtime `sh` | `timeout` defaults to 120 s; `on_timeout: skip` |
| `script: <inline\|name>` | `exec` | `runtime: bun\|uv`, `deps` |
| `loop: {...}` | `loop` | Fields listed below |
| `loop_group: {nodes: [...]}` | `loop_group` | A sub-DAG loop |
| `approval: {...}` | `gate` | |
| `wait: {...}` | `wait` | |
| `cancel: <reason>` | `halt` | |
| `include: <wf>` | — | Expanded at load time |
| `workflow: <name>` | `workflow` | A governed child run with `input`, `with` and `isolation: inherit\|worktree` |

`fan_out` applies to `workflow` and `include` nodes. Its schema is `{items: "$node.output[.field]" → JSON array, as?, max_parallel=5, join: all_success|all_done}`.

**`when:` conditions.** `when:` takes a tiny expression language:
- operators `$id.output == 'X'`, `!=`, `>`/`>=`/`<`/`<=` (numeric), with `&&` binding tighter than `||` and no parentheses;
- `$id.output.field` dot paths, `$INPUTS.x`, and `$LOOP_PREV.<id>.output` inside a loop group.

An unparseable expression evaluates to false and the node is skipped with a warning. Skips propagate downstream. Nodes in the same topological layer run in parallel through `Promise.allSettled`.

**Outputs.**
- `$node.output` is the concatenation of an AI node's assistant text, or a bash node's stdout.
- When `output_format` is set, the value is validated structured JSON.
  - Claude enforces it with SDK `outputFormat`, and Codex with `TurnOptions.outputSchema` normalized to strict mode.
  - Pi and Copilot are best effort: the schema is appended to the prompt, then JSON is extracted, repaired and re-asked up to 3 times.
- Outputs are stored in the DB as a 32 KiB preview. Anything larger spills to `$ARTIFACTS_DIR/.archon/node-output-spills/<node>.nodeoutput`.
- When spliced into bash, outputs are shell-quoted. User-controlled values arrive as env vars (`ARGUMENTS`, `REJECTION_REASON`, `INPUTS_<NAME>`, …).

**Variable substitution.** It is a regex chain (`executor-shared.ts:712`) over `$ARGUMENTS/$USER_MESSAGE, $WORKFLOW_ID, $ARTIFACTS_DIR, $STATE_DIR, $BASE_BRANCH, $CONTEXT, $LOOP_USER_INPUT, $REJECTION_REASON, $LOOP_PREV_OUTPUT, $INPUTS.<name>`, followed by `$nodeId.output`.

**Loop fields** (`schemas/loop.ts`):
- `until` is a completion signal, either `<promise>SIG</promise>` or a final line.
- `max_iterations`: exhausting it **fails** the node.
- `fresh_context` defaults to false, which resumes the previous iteration's session.
- `until_bash` (exit 0 means done), `until_field`, `interactive` plus `gate_message`, `signal_completes`.

**Approval fields** (`dag-node.ts:572`):
```ts
approvalConfigSchema = { message, decisions?: [{id, label?}] /* must include 'approve'; output {decision,text} routed via when: */,
  capture_response?: boolean, on_reject?: { prompt /* $REJECTION_REASON */, max_attempts?: 1..10 /* default 3 */ } }
```

**Wait fields** (`dag-node.ts:398`). Each variant is a strict object, and exactly one is allowed:
- `{duration_ms}`
- `{until: ISO | $ref}`
- `{event, deadline_ms}`
- `{attention: "<what the human must do>"}`

The node's output is `{status:'satisfied'|'expired', waited_ms, event?, payload?}`. Wait nodes cannot set `output_format`, `retry` or `always_run`.

**Example** (`guides/approval-nodes.md`, verbatim):
```yaml
name: plan-approve-implement
description: Plan, get approval, then implement
interactive: true
nodes:
  - id: plan
    prompt: |
      Analyze the codebase and create a detailed implementation plan.
      $USER_MESSAGE
  - id: review-gate
    approval:
      message: "Review the plan above before proceeding with implementation."
    depends_on: [plan]
  - id: implement
    command: implement
    depends_on: [review-gate]
```
A second example, from `.archon/workflows/test-workflows/e2e-pi-all-nodes-smoke.yaml`. It covers a pi provider, a loop, bash, script and `when`:
```yaml
provider: pi
model: anthropic/claude-haiku-4-5
nodes:
  - { id: prompt-node, prompt: "Reply with exactly the single word 'ok'...", allowed_tools: [], effort: low, idle_timeout: 30000 }
  - { id: loop-node, loop: { prompt: "Reply with exactly 'DONE'...", until: 'DONE', max_iterations: 2 } }
  - { id: bash-json-node, bash: "echo '{\"status\":\"ok\"}'" }
  - { id: gated, bash: "echo 'gated-ok'", depends_on: [bash-json-node], when: "$bash-json-node.output.status == 'ok'" }
```

### 3.2 Harness invocation

Archon calls each harness **in process through its SDK**, not through a CLI.

**Contract** (`packages/providers/src/types.ts:664`):
```ts
interface IAgentProvider {
  sendQuery(prompt: string, cwd: string, resumeSessionId?: string, options?: SendQueryOptions): AsyncGenerator<MessageChunk>
  getType(): string; getCapabilities(): ProviderCapabilities }
```
- **Options:** `model, abortSignal, systemPrompt, outputFormat, env, maxBudgetUsd, fallbackModel, forkSession, persistSession, nodeConfig{mcp, hooks, skills, allowed_tools, denied_tools, effort, sandbox, …}`.
- **Chunks** (`provider-contract/src/events.ts`): ACP-like events `agent_message_chunk, agent_thought_chunk, tool_call, tool_call_update` (output truncated to 16 KiB), `warning, mcp_server_status, compaction, subtask, hook, state_update, result, settled`.
- **`result`** (`result.ts`): `{sessionId, tokens{input,output,cacheRead?,cacheWrite?,total?,cost?}, structuredOutput?, failure?, isError, errorSubtype, cost?, stopReason: end_turn|max_tokens|max_turn_requests|refusal|cancelled, numTurns, resolvedModel, resumed?}`.
- **Failure classes:** `auth, quota_exhausted, budget_exceeded, misconfigured, rate_limited, transient, unknown`, each with `retryAfterMs` and `resetAt`.
- **Node completion:** "The engine finishes a node on `settled`, not on `result`". Claude can emit `result` while background agents are still running.
- **Capability matrix:**

| Capability | claude | codex | pi |
|---|---|---|---|
| sessionResume | ✓ | ✓ | ✓ |
| sessionFork | ✓ | ✗ | ✓ |
| structuredOutput | enforced | enforced | best-effort |
| costReporting | ✓ | ✗ | ✓ |
| costControl | ✓ | ✗ | ✗ |
| toolRestrictions | ✓ | ✗ | ✓ |
| mcp | ✓ | ✓ | ✗ |

**Claude** (`providers/src/claude/provider.ts:909`):
- `query({prompt, options})`. The options include `cwd, env (+CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1), model, abortController, permissionMode:'bypassPermissions', allowDangerouslySkipPermissions:true, systemPrompt: {type:'preset',preset:'claude_code'}`, plus `settingSources ['project','user']`, `resume`, `outputFormat` and `maxBudgetUsd`.
- `allowed_tools` maps to `tools`, and `denied_tools` to `disallowedTools`.
- Cost comes from `total_cost_usd`, made session-differential when resuming.

**Codex** (`codex/provider.ts:113`):
- `new Codex({codexPathOverride, env}).startThread|resumeThread(id, {workingDirectory, skipGitRepoCheck:true, sandboxMode:'danger-full-access', networkAccessEnabled:true, approvalPolicy:'never', model, modelReasoningEffort})`, then `thread.runStreamed(prompt, {outputSchema, signal})`.
- There is no system-prompt channel, so the system prompt is prepended to the prompt. There is no cost figure. A failed resume falls back to a fresh thread.

**Pi** (`community/pi/provider.ts`):
- `createAgentSession({cwd, model, modelRuntime, sessionManager, settingsManager, resourceLoader, thinkingLevel, customTools?})`, then `session.bindExtensions()` and `session.prompt()`. The abort signal triggers `session.abort()`, and the session is always `dispose()`d.
- Resume works by finding the session through `SessionManager.list(cwd)` and calling `.open(path)`, or `.forkFrom(path)` to fork.
- Usage is summed from `agent_end.messages`, with cost taken from `usage.cost.total`.

**Engine side** (`dag-executor.ts:1980`):
- Each node gets its own AbortController.
- `withIdleTimeout` defaults to 30 min (`utils/idle-timeout.ts`) and is reset by every chunk.
- The DB run status is polled every 10 s for cancellation, and an activity heartbeat is written every 60 s.
- Session threading: a sequential layer with the same provider resumes the previous session unless `context: fresh` is set. `context:{resume:id}` forks that node's stored handle (table `..._run_node_sessions`).

**Isolation** (`packages/isolation/src/providers/worktree.ts`):
- Each run gets a git worktree at `~/.archon/workspaces/<owner>/<repo>/worktrees/<branch>`.
- The branch is `archon/task-<slug(workflow-<ts>)>` unless `--branch` is given.
- `--no-worktree`, `--folder` and `--container` change this. The container mode is a Docker overlay with a write-back gate.
- Only one active run is allowed per `working_path`, unless `mutates_checkout: false`.

### 3.3 State and events

**Database.** SQLite (`~/.archon/archon.db`) or Postgres. The DDL is in `migrations/000_combined.sql`.

`remote_agent_workflow_runs` columns:
- `id, workflow_name, conversation_id, codebase_id`
- `status` (`pending|running|completed|failed|cancelled|paused`), `outcome` (`succeeded|failed`)
- `user_message, metadata JSONB, parent_run_id, adopted_from_run_id`
- `started_at, completed_at, last_activity_at, working_path, output_root, checkout_baseline`

Gate, wait and quota state lives in `metadata`: `approval`, `rejection_reason/count`, `wait{kind,resumeAt,signaledAt,payload}`, `execution_owner` and `scheduled_resume`.

`remote_agent_workflow_events` columns: `id, workflow_run_id, event_order, event_type, step_index, step_name, data JSONB, created_at`.

There are also `..._run_node_sessions(run, node_id, provider, provider_session_id)` and the cross-run `..._workflow_node_sessions`.

**Event types** (`packages/workflows/src/store.ts:91`, verbatim):
```ts
NODE_LIFECYCLE = ['node_started','node_suspended','node_completed','node_failed','node_skipped','node_skipped_prior_success']
NODE_STATE = [...NODE_LIFECYCLE, 'node_prior_cache_invalidated','node_always_run_reset']
WORKFLOW_EVENT_TYPES = ['workflow_started','workflow_completed','workflow_failed','workflow_resumed','workflow.run_adopted', ...NODE_STATE,
 'loop_iteration_started','loop_iteration_completed','loop_iteration_failed','provider_event','ralph_story_started','ralph_story_completed',
 'approval_requested','approval_received','wait_started','wait_signaled','wait_completed','wait_expired',
 'quota_resume_scheduled','quota_resume_triggered','quota_resume_exhausted','quota_resume_skipped','workflow_cancelled','workflow_artifact',
 'integration_operation','node_session_resumed','container_created','container_stopped','container_resumed','container_destroyed',
 'writeback_requested','writeback_applied','writeback_discarded','evidence_validation_failed','workflow_parse_warnings',
 'workflow_deprecation_notice','fan_out_instances']
```
The node event `data` (`node-record-serialization.ts`) holds:
- execution facts: `type, provider, model, effort, duration_ms`
- usage: `tokens, cost_usd, stop_reason, num_turns`
- failures: `error, failure_kind`
- output: `node_output` (a 32 KiB preview), `node_output_spill_path, structured_output`
- cache and loop state: `prior_output*, invalidating_deps, iteration`

**JSONL transcript.**
- Location: `~/.archon/workspaces/<project>/logs/<run-id>.jsonl` (`logger.ts`). It is append-only, and write errors are swallowed.
- Line types: `workflow_start|workflow_resume|workflow_complete|workflow_error|node_start|node_complete|node_skipped|node_error|node_suspended|gate_decision|exec_output|validation|watchdog_reset|provider_event`.
- Each line carries `{workflow_id, step?, content?, duration_ms?, tokens?, cost_usd?, error?, decision?, stdout_tail?, exit_code?, ts}`.
- A `provider_event` line wraps the event as `{attemptId, seq, observedAt, event}`.
- **The DB is authoritative for resume. The JSONL is an audit and streaming copy.**

**How resume decides what to skip.**
1. `getDagResumeSnapshot` (`core/src/db/workflow-events.ts:615`) folds node-state events in order. Any later state for a node clears it. Only `node_completed` or `node_skipped_prior_success` with an output restores it.
2. In `runLayers` (`dag-executor.ts:9335`), a node completed earlier is skipped and emits `node_skipped_prior_success`, and its cached output is fed downstream, **unless**:
   - it is marked `always_run`, which emits `node_always_run_reset`; or
   - a dependency's output now *differs in value* from the prior snapshot, which emits `node_prior_cache_invalidated`.
3. Resume is always explicit. It needs `--resume`, `workflow resume`, or a continuation through approve or reject.

**Frozen source** (`packages/workflows/src/workflow-source.ts`).
- Source is captured to `<project>/workflow-source/runs/<run-id>/manifest.json`. The manifest holds `engine_version, origin, captured_at, digest` (sha256 folded over per-file sha256), `file_count, byte_count, scopes, workflow_name`.
- The digest is re-verified on load. A mismatch raises `WorkflowSourceIntegrityError` and fails the run closed.
- The docs put it plainly: "A run does not change shape while it is running."

### 3.4 Control verbs and gate/wait mechanics

**Workflow commands:**
- `workflow list|run|status|runs|get [--events]|logs [--follow --format jsonl|text]`
- `workflow wait <id> [--timeout]`: exit 0 means the run said something; 3 means timeout.
- `workflow resume|cancel|abandon|approve [--comment]|reject [--reason]|respond <decision>`
- `workflow cleanup|reset-sessions|event emit --run-id --type [--data]`

**Other commands:** `validate workflows|commands`, `serve`, `skill install`.

**`run` flags:** `--branch --no-worktree --folder --container --input k=v --model tier=spec --resume --detach --dry-run --stubs --pause-at-gates --json`.

**Output contract.** `--json` prints one document on stdout. `--detach` prints the acknowledgement `{ok, runId, action, detached, continues, logPath, transcriptPath?}`.

**Approval gate** (`executeApprovalNode`, `dag-executor.ts:6894`):
1. The gate renders its message and emits `approval_requested`.
2. The node becomes `node_suspended`, and the run is set to `paused` with `metadata.approval`.
3. **The process exits**, printing "Workflow paused — waiting for approval".
4. `approve` resolves the gate with a CAS and writes the gate's `node_completed` (output `''`, the comment, or `{decision,text}`) and `approval_received`. In human CLI mode it then resumes inline. With `--json` it only records the approval.
5. `reject` with `on_reject` stages `rejection_reason`. On resume, the rework prompt runs as a synthetic AI node and the gate pauses again. After `max_attempts` rejections the run is cancelled with reason `approval_rejected`.
6. `reject` without `on_reject` cancels the run.

**Durable wait** (`executeWaitNode`, `dag-executor.ts:6675`):
1. The node persists `metadata.wait`, sets the run to `paused`, emits `wait_started`, and frees the worker slot.
2. A foreground or `--detach` owner process **stays alive**. Its `awaitDurableWaitDeadline` polls the DB every 5 s.
3. `archon serve` also runs a continuation scan (`server/src/services/workflow-resume-service.ts`) every 5 s, in batches of 25. It claims runs whose wait has a `signaledAt` or a `resumeAt <= now`, using a cursor CAS, and resumes them, so orphaned runs are recovered.

**Signals.** `POST /api/workflows/runs/{id}/signal {event, resumeAt, payload}` or `archon workflow event emit`.
- The handler (`signalWorkflowWait`, `core/src/db/workflows.ts:1929`) is a single conditional UPDATE: `WHERE status='paused' AND wait.event=$2 AND wait.nodeId=$3 AND wait.resumeAt=$4 AND signaledAt IS NULL`.
- `resumeAt` acts as an **occurrence token**, so duplicate or stale signals are no-ops.

**Wait expiry and limits.**
- An event wait that passes its deadline completes as `expired`; it does not fail.
- `attention` waits are never resumed automatically.
- Waits are not supported in container runs.

**Cancel and abandon.**
- `cancel` applies to running runs. If the owner is in this process, cancel takes effect at the next 10 s poll through the AbortController. If the owner is a detached child, cancel goes through the live-owner endpoint and kills the process tree. If no owner answers, cancel refuses and points to `abandon`.
- `abandon` applies to paused or orphaned runs and cascades to sub-runs.

### 3.5 Outer-agent surface

**Skill.** `.claude/skills/archon-cli/SKILL.md` is 100 lines and routes to sub-docs:
- `running-workflows/`
- `manage-run/{manage-runs.md, troubleshooting.md}`
- `setup-and-config/`
- `authoring-workflows/{authoring-workflows.md, node-reference.md, variables.md}`
- `prompting-mistakes/`

Frontmatter: `name: archon-cli`, `description` ("Drive Archon through its CLI: run AI workflows…, manage those runs (inspect, approve, reject, cancel, resume)…, author new workflows… NOT for: doing the coding work yourself"), and `argument-hint`.

Key instructions:
- Discover workflows with `workflow list --json`, then `list <name> --full`.
- **Before launching, check the brief against six items: problem, why, why now, outcome, invariants, acceptance.**
- Launch with `--detach`, then run `archon workflow wait <id> --json` as a background task. Do not poll.
- A completed run is not necessarily a success. Check `outcome`.
- The approve/resume two-step: `--json` only records the decision.

The skill is bundled into the binary and installed with `archon skill install`.

**MCP.** There is no standalone MCP server for Archon. The chat agent gets an in-process `manage_run` tool (`packages/core/src/orchestrator/manage-run-tool.ts`) with actions `list, get, start, resume, cancel, abandon, approve, reject, respond`. Destructive actions require `confirm=true`.

### 3.6 License, size, tests
- MIT (© 2025-2026 Cole Medin).
- `packages/**/*.ts`: 150.7k non-test LOC and 232.5k test LOC, in 372 test files. The workflows package alone has 43k src and 88k test lines.
- Tests use `bun test` with coverage on and no threshold. CI covers Postgres integration, schema-upgrade vintages, provider-contract conformance suites, and e2e smoke workflows for claude, codex and pi.

---

## 4. gh-aw (github/gh-aw)

**Layout.** gh-aw is written in Go plus JavaScript.
- `pkg/parser` holds the frontmatter parser and its JSON Schema, `pkg/parser/schemas/main_workflow_schema.json`.
- `pkg/workflow` holds the compiler and the engines.
- `pkg/cli` holds the audit, logs and mcp-server commands.
- `actions/setup/js` holds the runtime pieces: the safe-outputs MCP server, the validators and the log parsers.

### 4.1 How a markdown workflow compiles to an agent job

**Compilation.** `gh aw compile` runs `pkg/workflow/compiler.go:CompileWorkflow` → `buildJobs`.
- Input is `.github/workflows/<name>.md`: YAML frontmatter followed by a markdown body.
- Output is `<name>.lock.yml`. The frontmatter is validated against the embedded JSON Schema plus Go validators; the `strict:` mode additionally forbids write permissions and a bare `*` in `network`.

**Main frontmatter keys.**
- Trigger, permissions and runtime: `on` (the only required key), `permissions`, `engine` (string, or object `{id, model, max-turns, permission-mode, args, env, command, harness, bare, …}`), `timeout-minutes`.
- Agent capabilities: `tools` (`github`, `bash: [allowlist]`, `edit`, `web-fetch`, `cache-memory`, …), `mcp-servers`, `network` (domain allowlist enforced by the AWF squid firewall), `sandbox`.
- Side effects: `safe-outputs`.
- Composition and extra steps: `imports`, `steps`, `pre-steps`, `post-steps`, `jobs`.
- Budgets: `max-turns`, `max-ai-credits`, …

**Jobs emitted:**
1. `pre_activation`: role checks.
2. `activation`: builds `prompt.txt` and checks the lock file for staleness.
3. `agent`: **read-only permissions**; the engine runs inside the AWF sandbox.
4. `detection`: threat detection.
5. `safe_outputs`: holds the write permissions. It runs only when `needs.detection.result == 'success'`.
6. `conclusion`.

**Prompt assembly** (`compiler_yaml_prompt.go`). The prompt is built from fixed system fragments in `actions/setup/md/`:
- `xpia.md`, a cross-prompt-injection warning;
- `safe_outputs_prompt.md`;
- a generated block `<safe-output-tools>Tools: add_comment, missing_tool, missing_data, noop(max:2)</safe-output-tools>`.

**Body handling.** The body itself is a `{{#runtime-import …md}}`, resolved at run time, so prose edits need no recompile.

**Template expressions.** `${{ }}` expressions in the body are checked against an allowlist (`expression_safety_validation.go`) and moved into env vars. The text keeps placeholders such as `__GH_AW_EXPR_<hash>__`, so untrusted values are never spliced into YAML or shell.

**Lock-file header** (verbatim shape):
- `# gh-aw-metadata: {"schema_version":"v4","frontmatter_hash":…,"body_hash":…,"strict":true,"agent_id":"claude","agent_model":…,"engine_versions":{"claude":"2.1.286"}}`
- `# gh-aw-manifest: {secrets[], actions[{repo,sha,version}], containers[{image,digest}], mcp_servers[{name,tools[]}]}`

At run time, `check_workflow_timestamp_api.cjs` recomputes the frontmatter hash and fails if it does not match. This is staleness detection, "not tamper protection".

**Example** (`.github/workflows/smoke-github-claude.md`, abridged verbatim):
```markdown
---
on: { schedule: every 2 days, slash_command: { name: smoke-github-claude, events: [pull_request, pull_request_comment] } }
permissions: { contents: read, pull-requests: read }
engine: { id: claude, model-provider: github, bare: true }
strict: true
tools: { github: { mode: gh-proxy } }
safe-outputs:
  allowed-domains: [default-safe-outputs]
  add-comment: { max: 1, hide-older-comments: true }
timeout-minutes: 10
sandbox: { agent: { id: awf } }
---
# Smoke Test: Claude on GitHub Provider PR Summary
1. If this run is not in PR context, call `noop` and stop.
2. Read the current PR details for `${{ github.event.pull_request.number }}` ...
4. Post exactly one `add_comment` safe output to the current PR with this summary.
```

### 4.2 Engine abstraction and argv

**Engine interface** (`pkg/workflow/agentic_engine.go`, verbatim method sets):
```go
type CodingAgentEngine interface { Engine; CapabilityProvider; WorkflowExecutor; MCPConfigProvider
                                   LogParser; SecurityProvider; ModelEnvVarProvider; ConfigRenderer }
// Engine: GetID, GetDisplayName, GetDescription, IsExperimental, GetGHSkillAgentName
// WorkflowExecutor: GetDeclaredOutputFiles() []string; GetInstallationSteps; GetSecretValidationStep;
//   GetExecutionSteps(workflowData, logFile string) []GitHubActionStep; GetFirewallLogsCollectionStep; ...
// LogParser: ParseLogMetrics(logContent string, verbose bool) LogMetrics; GetLogParserScriptId; GetLogFileForParsing;
//   GetErrorDetectionScriptId; GetInternalLogsDir
// SecurityProvider: GetDefaultDetectionModel; GetRequiredSecretNames; GetSupportedEnvVarKeys
```

**Capability flags.** `EngineCapabilities{ToolsAllowlist, MCP, MaxTurns, ContextWindow, WebSearch, MaxContinuations, NativeAgentFile, BareMode, BashCommandAllowlist, BashDisable, Plugins}`.
- The compiler **errors** when a frontmatter restriction cannot be enforced by the chosen engine. The code comment calls this preventing "the allowlist illusion".
- `LogMetrics` holds `{TokenUsage, EstimatedCost, Turns, ToolCalls, ToolSequences, inter-turn timing stats}`.
- A declarative `EngineDefinition`, expressed in YAML, can add an engine without writing Go.

**Rendered argv:**
- **claude:**
  ```
  claude_harness.cjs claude --print --no-chrome --strict-mcp-config [--mcp-config F] --allowed-tools 'Bash(cat),Edit(/tmp/*),…'
  --debug-file /tmp/gh-aw/claude-debug.log --verbose --permission-mode acceptEdits --output-format stream-json [--max-turns N] --prompt-file /tmp/gh-aw/aw-prompts/prompt.txt
  ```
  - The model is passed through `ANTHROPIC_MODEL`.
  - `ANTHROPIC_MAX_RETRIES=0`, because the harness owns retries.
  - The harness retries with `--continue` when partial output exists: backoff 5→10→20 s, at most 3 attempts.
- **codex:**
  ```
  codex exec [--model M] -c web_search="disabled" -c fetch="disabled" --sandbox workspace-write --skip-git-repo-check -c approval_policy="never" [--output-schema S -o R] --prompt-file …
  ```
  Under the firewall, the sandbox flags become `--dangerously-bypass-approvals-and-sandbox`. Config lives in `config.toml` under `CODEX_HOME`.
- **pi:**
  ```
  cat user.txt | pi --print --mode json --no-session --no-approve --model aw-gateway/auto --append-system-prompt system.txt
  --extension pi_provider.cjs --extension pi_steering_extension.cjs --extension pi_tool_policy.cjs --extension builtin:mcp … | tee pi-streaming.jsonl
  ```
  Tool policy is enforced inside pi by an extension, `pi_tool_policy.cjs`. A `--session-dir`/`--session-id`/`--fork` variant exists.

### 4.3 Safe outputs: validated writes

**Principle.** "agents run read-only and request actions via structured output, while separate permission-controlled jobs execute those requests". The formal spec is `docs/src/content/docs/specs/safe-outputs-specification.md`. It rests on four principles:
- P1: write permissions live in separate execution contexts from the AI reasoning.
- P2: declarative over imperative.
- P3: configurable constraint enforcement.
- P4: fail secure.

**Flow:**
1. **Compile.** The `safe-outputs:` frontmatter produces three things: (a) a filtered set of MCP tool schemas (`safe_outputs_tools.json`, 62 types), (b) a handler config, and (c) `validation.json`, whose source of truth is the Go `ValidationConfig`.
2. **Agent job.** The `safeoutputs` MCP server is also available as a CLI, `safeoutputs <tool>`.
   - Each call appends **one JSONL line** to `$GH_AW_SAFE_OUTPUTS`, e.g. `{"type": "create_issue", "title": "…", "body": "…"}`.
   - Per-type `max` is enforced **at call time** (JSON-RPC `E002: <type> limit reached — 1 of 1 already used this run`).
   - PR-type outputs do not push. They write `git format-patch` or bundle files and record `base_commit` and `diff_size`.
3. **Ingest** (`collect_ndjson_output.cjs`). Every line is checked again for `type` membership, max/min counts, field schema, and sanitization (URL-domain redaction, @mention and command neutralization, unicode hardening). The result is written to `agent_output.json = {items, errors}`.
4. **Detection job.** A separate engine run, with no MCP, reads the prompt, the agent output and the patch. It must emit `THREAT_DETECTION_RESULT:{"prompt_injection":false,"secret_leak":false,"malicious_patch":false,"reasons":[]}` (Codex uses `--output-schema`). Any finding fails the job and blocks the apply step.
5. **`safe_outputs` job.** It applies each item through its handler and writes a manifest `safe-output-items.jsonl`, e.g. `{"type":"create_issue","url":…,"number":1,"repo":…,"timestamp":…}`.

**Further features:**
- `staged: true` turns the run into a dry run that shows a preview.
- `temporary_id` (`#aw_xxx`) lets one intent reference another item that has not been created yet.
- Custom `safe-outputs.jobs.<name>` define user deterministic jobs, which are exposed to the agent as tools.
- Patches that touch agent configuration (`CLAUDE.md`, `.claude/`), lockfiles or workflow files take protected fallback paths.

**Terminal-intent rule** (`safe_outputs_prompt.md`): "You MUST call one of the safe-output tools before finishing… If no GitHub action was taken… you MUST call `noop`". `missing_tool`, `missing_data` and `report_incomplete` turn capability gaps into data.

### 4.4 `gh aw audit`, `logs` and `mcp-server`

**`gh aw audit <run-id|url>…`** (`pkg/cli/audit_command.go`, `audit_report.go`)
- It downloads artifact sets: `activation`, `agent` (logs, safe outputs, usage), `mcp`, `firewall` and `detection`. It also reads the `aw_info.json` run record, which holds `engine_id, model, version, run_id, run_attempt, sha, staged, allowed_domains, firewall_enabled, created_at, …`.
- `--json` emits `AuditData`; the source says the "top-level fields are stable". Its fields:
  - `schema_version, overview, comparison` (an auto-selected baseline run)
  - `behavior_fingerprint, metrics{token_usage, turns, error_count…}`
  - `key_findings[{code, category, severity, title, description, impact}]`, `recommendations[{priority, action, reason}]`
  - `jobs[]`, `downloaded_files[{path, description}]`
  - `safe_output_summary, created_items[{type,url,…,before_state,after_state}]`
  - `missing_tools, noops, mcp_failures, errors, warnings, tool_usage, firewall_analysis`
- Finding codes are stable machine IDs, for example `workflow_failed, workflow_timeout, high_token_usage, many_iterations, mcp_server_failures, blocked_network_requests, threat_detected`.
- `gh aw json-schema audit` emits a JSON Schema derived from the Go types.
- Passing two or more runs produces a diff: firewall domains new or removed, tool-call changes, and % deltas for tokens, turns and duration, with anomaly flags.

**`gh aw logs`** does bulk download with a per-run `run_summary.json` cache, filters, `--audit` and `--tool-graph` (Mermaid).

**`gh aw mcp-server`** (`pkg/cli/mcp_server.go`) exposes these tools:
- `status`, `compile` (its description says "Any change to .github/workflows/*.md files MUST be compiled using this tool"), `mcp-inspect`, `checks`, `add`, `update`, `fix`.
- Privileged, requiring write access: `logs`, `audit` (`run_ids_or_urls[]`), `audit-diff`.

The same server can be mounted inside a workflow (`tools: agentic-workflows:`), so one agent can audit other runs.

### 4.5 Agent-facing documentation
- The root `SKILL.md` is 14 lines: install, then load the router skill.
- The router, `.github/skills/agentic-workflows/SKILL.md`, is a dispatcher. It loads one of about 80 task prompts in `.github/aw/*.md` (create, update, debug, upgrade, …) on demand. A repository overlay, `.github/aw/instructions.md`, takes precedence.
- `create.md` addresses the reader as "you, a coding agent". Each action lists "Load when / Prompt file / Use cases".
- Every lock file header points to `debug.md`.

### 4.6 License, size, tests
- MIT (© GitHub, Inc.).
- About 318k non-test Go LOC in 1,415 files, plus about 141k LOC of runtime JS.
- 1,844 `*_test.go` files, about 598k LOC.
- `.github/workflows/` holds 649 files, mostly dogfooded `.md` workflows paired with their `.lock.yml`.

### 4.7 What flower can borrow
1. **Declare permitted side effects per agent node.** Use an `effects:` block that the compiler turns into the agent's tool list, a prompt block and a validation config, and error at compile time if the chosen harness cannot enforce it.
2. **Emit intents, apply them deterministically.** The agent emits intent JSONL through `flower emit` or MCP, and limits are enforced at call time and again at apply time. A separate deterministic node applies the intents and writes a manifest.
   - For HPC, `submit_job` should be an intent that the engine validates and executes, never a raw `sbatch` run by the agent.
3. **Mandatory terminal intent.** Require one of `noop`, `report_incomplete` or `missing_data`, so a silent exit is detectable.
4. **Code changes as patches.** The agent produces a patch with a `base_commit`, and the apply step checks protected paths and size limits.
5. **Lock-file header with hashes.** Record frontmatter and body hashes, engine versions and per-node effect tools, and treat a mismatch as a staleness check.
6. **Audit command.** A stable-schema `audit --json` with finding codes and run-vs-run diffs.
7. **Engine capability flags.** These stop flower promising restrictions a harness cannot enforce.

---

## 5. pi (earendil-works/pi) as a headless agent node

The agent lives in `packages/coding-agent/` (npm `@earendil-works/pi-coding-agent` v1.0.0, binary `pi`). All paths in this section are relative to that directory. pi is not installed on this host, so everything here comes from source and docs.

### 5.1 CLI flags (`src/cli/args.ts`)

**Flags relevant to a node:**
- `-p/--print`; `--mode text|json|rpc`. `--mode json` alone is already one-shot.
- Model: `--provider`, `--model <provider/id[:thinking]>`, `--thinking off|minimal|low|medium|high|xhigh|max`, `--api-key` (requires `--model`).
- System prompt: `--system-prompt <text|file>`, `--append-system-prompt <text|file>` (repeatable).
- Sessions: `--session <path|id>`, `--session-id <id>` ("Use exact project session ID, creating it if missing"), `--fork <path|id>`, `--session-dir <dir>`, `--no-session`, `-c/--continue`, `-r/--resume` (a TUI picker, so unusable headless), `--name`.
- Tools: `--tools a,b`, `--exclude-tools`, `--no-tools`, `--no-builtin-tools`.
- Resources: `-e/--extension <path|builtin:name>`, `--no-extensions`, `--skill <path>`, `--no-skills`, `--no-context-files`.
- Trust: `--approve` / `--no-approve` set project-local trust for this run.
- `--offline`, and `--` to end option parsing.

**Input and preflight.**
- `@file` arguments include a file in the first prompt; RPC mode rejects them.
- Non-TTY stdin is read to EOF and prepended to the prompt with **no separator**. If flower leaves the stdin pipe open, pi hangs.
- `pi auth check --provider X [--json]` exits 0 for ready, 1 for not_ready and 2 for invalid. It works as a preflight check.

**Exit codes:**
- 0: success. **In json mode this includes an assistant error or abort**; inspect `stopReason`.
- 1: in text/print mode, a final `stopReason` of error or aborted. In any mode: config, model or session errors.
- 143: SIGTERM. 129: SIGHUP.

### 5.2 `--mode json` stream (`src/modes/json-event.ts`, `docs/json.md`)

**Framing.** Strict LF-delimited JSONL. The docs say not to use Node `readline`, because it also splits on U+2028.

**Line 1 is a session header**, even with `--no-session`:
`{"type":"session","version":3,"id":"<uuid>","timestamp":"…","cwd":"/path"}`.

**Events:**
- Agent: `agent_start`, `agent_end{messages,willRetry}`, `agent_settled`, meaning no more automatic work follows.
- Turns: `turn_start`, `turn_end{message,toolResults}`.
- Messages: `message_start`, `message_update{usage, assistantMessageEvent}` (deltas only), `message_end{message}`.
- Tools: `tool_execution_start{toolCallId,toolName,args}`, `tool_execution_update`, `tool_execution_end{toolCallId,toolName,result,isError}`.
- Housekeeping: `queue_update`, `entry_appended`, `compaction_start|end`, `auto_retry_start|end`, and others.

**Assistant message and usage shape:**
```ts
AssistantMessage { role:"assistant"; content:(Text|Thinking|ToolCall)[]; provider; model;
  usage:{input,output,cacheRead,cacheWrite,totalTokens, cost:{input,output,cacheRead,cacheWrite,total}};
  stopReason:"stop"|"length"|"toolUse"|"error"|"aborted"|...; errorMessage? }
```

**Extracting a result.**
- Final text: the last assistant `message_end`, with its text blocks joined.
- Cost: sum `usage.cost.total` over assistant `message_end` events, plus `compaction_end.result.usage`. Alternatively, recompute it from the session file.

### 5.3 `--mode rpc` (`docs/rpc.md`, `src/modes/rpc/rpc-types.ts`, client `src/modes/rpc/rpc-client.ts`)

**Protocol.** Commands are stdin JSONL `{id?, type, …}`. Responses look like `{"id","type":"response","command","success","data"|"error"}`. Events use the same format as json mode.

**Commands:**
- Prompting: `prompt{message, streamingBehavior?: steer|followUp}`, `steer`, `follow_up`, `abort`, `clear_queue`, `new_session`.
- State and model: `get_state` (returns `sessionFile`, `sessionId`, `isStreaming`, …), `set_model`, `cycle_model`, `get_available_models`, `set_thinking_level`, `set_steering_mode`, `set_follow_up_mode`.
- Compaction and retry: `compact`, `set_auto_compaction`, `set_auto_retry`, `abort_retry`.
- Bash: `bash`, `abort_bash`.
- Session: `get_session_stats` (returns `{sessionFile, sessionId, tokens{input,output,cacheRead,cacheWrite,total}, cost, contextUsage, …}`), `export_html`, `switch_session`, `fork`, `clone`, `get_entries`, `get_tree`, `get_last_assistant_text`, `get_messages`, `get_commands`, `set_session_name`.

**Client gotcha.** `RpcClient.promptAndWait` and `waitForIdle` default to a **60 s timeout**, which must be raised for long runs.

RPC is the right mode for **steering a live node**, the equivalent of Smithers' `steer`.

### 5.4 SDK (`src/core/sdk.ts`, `docs/sdk.md`, `examples/sdk/01..14`)

**`createAgentSession({ … })` options:**
- `cwd`, `agentDir` (default `~/.pi/agent`)
- `modelRuntime` (`ModelRuntime.create({authPath, modelsPath})`), `model`, `thinkingLevel`
- `tools`, `noTools`, `excludeTools`, `customTools`
- `resourceLoader`, `sessionManager`, `settingsManager`

It returns `{session}`. Session methods:
- `prompt()`, `steer()`, `followUp()`, `abort()`, `subscribe()`
- `getLastAssistantText()`, `getSessionStats()`, `sessionFile`, `dispose()`

Extensions only receive `session_start` after `session.bindExtensions()` is called.

### 5.5 Session files (`src/core/session-manager.ts`, `docs/session-format.md`)

**Location.**
- Default path: `~/.pi/agent/sessions/--<cwd with / → ->--/<ISO-timestamp with :. → ->_<id>.jsonl`.
- With `--session-dir D`, files go flat into `D`, and lookup filters by the header's `cwd`.
- Ids default to UUIDv7. Custom ids must match `/^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/`.

**Format.**
- Line 1 is the header `{type:"session",version:3,id,timestamp,cwd,parentSession?}`.
- Each later line is an entry `{type,id,parentId,timestamp,…}`, forming a **tree**.
- Entry types: `message`, `model_change`, `thinking_level_change`, `usage`, `compaction`, `context_edit`, `branch_summary`, `custom`, `custom_message`, `label`, `session_info`.

**Durability.** The file is created lazily, once the first message exists. After that, every entry is written with `appendFileSync`, so a killed process still leaves a valid prefix.

**Inside the agent.** The bash tool receives `PI_SESSION_ID` and `PI_SESSION_FILE` in its environment.

### 5.6 Structured output

pi has **no native output schema**: no `--json-schema` and no `--output-schema`.

**Pattern** (`examples/extensions/structured-output.ts`): an extension registers a `submit_result` tool whose parameters are the JSON schema.
- Set `constrainedSampling:{type:"json_schema",strict:"prefer"}`, and have `execute` return `{details: params, terminate: true}`.
- flower reads `tool_execution_end{toolName:"submit_result"}.result.details`.
- If the tool is never called, re-prompt the same session.

### 5.7 Auth, config, extensions and permissions

**Agent directory.** `~/.pi/agent`, overridable with `PI_CODING_AGENT_DIR`. It holds `settings.json`, `auth.json` (mode 0600, file-locked), `models.json`, `mcp.json`, `trust.json`, `extensions/`, `skills/`, `prompts/` and `AGENTS.md`.

**`auth.json`** is keyed by provider:
- `{"anthropic":{"type":"api_key","key":"sk-…"}}`. The key may be `"!shell cmd"`.
- `{"openai-codex":{"type":"oauth","access","refresh","expires"}}`.

**Credentials.**
- OAuth logins via `/login`: anthropic, openai-codex, github-copilot and others.
- Env vars: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, … .
- Custom endpoints go in `models.json` `providers.<name>.{baseUrl, api:"openai-completions", apiKey, models[]}`.

**Other environment variables:** `PI_CODING_AGENT_SESSION_DIR`, `PI_OFFLINE`, `PI_SKIP_VERSION_CHECK`, `PI_TELEMETRY`.

**Headless trust pitfall.** Project `.pi/*` resources and `.agents/skills` are **silently skipped** when `defaultProjectTrust` is `"ask"`, which is the default. Always pass `--approve` or `--no-approve`. `AGENTS.md` and `CLAUDE.md` load regardless; use `-nc` to disable them.

**Extensions.**
- An extension is `export default function(pi){ pi.registerTool / registerCommand / registerFlag / on(event) }`.
- Gate a tool call with `pi.on("tool_call", e => ({block:true, reason}))`. In print and json modes `ctx.hasUI === false`.
- Examples: `examples/extensions/permission-gate.ts`, `protected-paths.ts`, `sandbox/`.

**Permissions.** pi has **no built-in permission system**. `docs/security.md` recommends isolating it in a container or VM.

**Skills.** Discovered from `~/.pi/agent/skills`, `.pi/skills`, `~/.agents/skills` and `.agents/skills`, using the Agent Skills `SKILL.md` format (`name`, `description`, …).

### 5.8 Cancel, cost and timeouts
- Print and json modes handle **only SIGTERM and SIGHUP**. Each kills tracked detached children and disposes the runtime. SIGINT kills the process without that cleanup, **so flower should send SIGTERM**.
- There is no run-level timeout; flower must enforce its own.
- Built-in auto-retry: 3 attempts with backoff.
- `httpIdleTimeoutMs` defaults to 300 s.

### 5.9 Recommended pi node invocation
```bash
# cwd = node workdir; stdin = prompt then EOF
pi --mode json --session-dir "$RUN/sessions/pi" --session-id "$RUN_ID-$NODE-$ATTEMPT" --name "ff:$NODE" \
   --model anthropic/claude-sonnet-4-5 --thinking medium --no-approve \
   [-e /abs/flower-submit.ts --append-system-prompt /abs/node-contract.md] < prompt.md > events.jsonl 2> stderr.log
# follow-up / resume same session:
pi --mode json --session "<abs session .jsonl>" --no-approve … < followup.md
# branch instead of append:  --fork <path> --session-id <new-id>
```

### 5.10 License, size, tests
- MIT (© 2025 Mario Zechner).
- `packages/coding-agent/src`: about 85k LOC, or about 53k excluding the interactive TUI and experimental code.
- 310 test files with about 2,519 cases (vitest).

---

## 6. Cross-check: Smithers 0.x CLI adapters (exact argv)

Source: `repos/smithers-0.x/packages/agents/src/`, MIT. This closes the survey's gap on how 0.x builds its argv.

**`ClaudeCodeAgent.buildCommand`.**
- Argv: `claude --print --output-format stream-json --verbose`, with `--verbose` "required when --print is combined with --output-format=stream-json". Optional flags: `--json-schema`, `--max-budget-usd`, `--resume <id>`, `--session-id`, `--fork-session`, `--permission-mode`, `--allowed-tools`/`--disallowed-tools`/`--tools`, `--mcp-config` plus `--strict-mcp-config`, `--setting-sources` and a single merged `--settings` JSON.
- The prompt is the final positional argument.
- Env: `CLAUDE_CONFIG_DIR`, `ANTHROPIC_API_KEY`.

**`CodexAgent`.**
- Argv: `codex exec [resume <thread>] --json [--sandbox …] [--cd …] [--skip-git-repo-check] [--add-dir] [--output-schema <file>] --output-last-message <file> -`, with the prompt on **stdin**.
- `codex exec resume` does not accept `--output-schema`.
- JSON events are mapped as: `thread.started` → started{resume: thread_id}; `item.*` → action; `turn.completed{usage}` → completed{answer, usage}; `turn.failed` → completed{ok:false}.

**`PiAgent`.** Argv: `pi --print --mode json|text …`. `--print` is applied in every non-RPC mode "so json task executions also process one prompt and exit" (#284). It uses `--session`/`--session-dir`/`--no-session` for sessions; `--mode rpc` runs through `runRpcCommandEffect`.

**Normalized event model.** All CLI adapters share it (`BaseCliAgent/AgentCliEvent.ts`, verbatim types):
```ts
type AgentCliEvent =
 | { type:"started"; engine; title; resume?: string; detail? }
 | { type:"action"; engine; phase:"started"|"updated"|"completed"; entryType?:"thought"|"message";
     action:{ id; kind:"turn"|"command"|"tool"|"file_change"|"web_search"|"todo_list"|"reasoning"|"warning"|"note"; title; detail? };
     message?; ok?; level?:"debug"|"info"|"warning"|"error" }
 | { type:"completed"; engine; ok: boolean; answer?: string; error?: string; resume?: string; usage? }
type NormalizedTokenUsage = { inputTokens?; outputTokens?; cacheReadTokens?; cacheWriteTokens?; reasoningTokens?; totalTokens? }
type CliOutputInterpreter = { onStdoutLine?(line); onStderrLine?(line); onExit?(result) }  // each → AgentCliEvent[]
```

**Process safety.**
- Timeouts are `{totalMs, idleMs}` (`resolveTimeouts.js`).
- `parentDeathWatchdog.js` wraps the agent so it dies if the engine dies.

**Claude flags.** I verified locally against `claude` 2.1.284 `--help` that these all exist: `-p`, `--output-format text|json|stream-json`, `--json-schema`, `--max-budget-usd`, `-r/--resume`, `--session-id`, `--fork-session`, `--no-session-persistence`, `--permission-mode acceptEdits|auto|bypassPermissions|manual|dontAsk|plan`, `--tools`, `--allowed-tools`, `--disallowed-tools`, `--mcp-config`, `--strict-mcp-config`, `--setting-sources`, `--settings`, `--bare`, `--append-system-prompt`, `--add-dir` and `--fallback-model`. `codex` is not installed on this host, so the codex flags above come from orc and Smithers source only.

---

## 7. Synthesis for flower

### 7.1 Side-by-side

| Aspect | Archon | orc | yak | gh-aw |
|---|---|---|---|---|
| Edge key | `depends_on` (node ids) + `when` + `trigger_rule` | `depends_on`; `consumes`/`produces` are separate and do not imply order | `needs` (**artifact names**) | n/a (one agent job) |
| Kind discriminator | one body key (`prompt:`, `bash:`, …) | `type:` | nested key in YAML, `kind` in IR | n/a |
| How the harness is called | in-process SDKs | CLI (`claude -p` stream-json, `codex exec --json`) | in-process Claude SDK | CLI inside a GH Actions job |
| Node output | assistant text or validated `output_format` JSON | files in `$ARTIFACTS_DIR` only | one schema-validated artifact per step | intents JSONL |
| Structured output | enforced for claude/codex, best-effort with re-ask for pi | none | SDK json_schema + repair loop | codex `--output-schema` (detection) |
| State | SQLite/PG events table (authoritative) + JSONL transcript | SQLite rows with a per-run sequence | **`journal.jsonl` only** + `workflow.json` | Actions artifacts |
| Resume skip rule | prior success, unless `always_run` or upstream output changed by value | `succeeded` status only | two content-addressed keys | n/a |
| Frozen spec | digest-verified `workflow-source/runs/<id>/` | `spec/` copy | `workflow.json` IR | `.lock.yml` + hashes |
| Gate | `approval` + `decisions`/`on_reject{max_attempts}`; process exits | `approval` + `on_reject`; worker exits; approve ≠ resume | request/answer files; exit 78 | PR review; detection job |
| Long wait | `wait:` (time/event+deadline/attention); owner polls or server scan; signal API | none | none | none |
| Per-node rerun + downstream | no (resume only) | no | spec-only (`--from`) | Actions re-run |
| Cost capture | yes (claude, pi) | no | tokens only | yes (log parsers) |
| Tests | very heavy (232k LOC) | **none** | 289 cases | very heavy |

### 7.2 Recommended plan-file schema (sketch)

The plan file is YAML. Each field is annotated with the project its convention comes from. Where projects disagree, I picked the majority or the safer convention.

```yaml
flower: 1                       # schema version int                       [orc `version`]
id: si-vacancy-formation           # ^[a-z0-9_-]+$, == file basename          [orc]
description: "…"                   # required; used by the outer agent to pick plans [orc, Archon]
inputs:                            # typed named inputs                       [Archon `inputs`, yak `inputSchema`]
  structure: { type: path, required: true, description: "POSCAR of the bulk cell" }
defaults:                          # workflow-level harness defaults          [Archon top-level provider/model/effort]
  harness: { name: claude, model: sonnet, effort: high }
  timeout: { total: 4h, idle: 30m }             # total+idle pair             [Smithers resolveTimeouts; Archon idle_timeout]
  retry:   { max_attempts: 2, on_error: transient } # failure-class aware     [Archon retry + failure classes]
  concurrency: 2                                 #                            [orc max_concurrent_nodes, yak concurrency]
nodes:
  - id: setup-cells
    kind: agent                    # agent|shell|function|job|gate|wait|loop|map — explicit discriminator [orc `type`, yak IR `kind`]
    prompt: { file: prompts/setup.md }   # prompts in files → stable cache keys; {inline: …} allowed [yak]
    context: fresh                 # fresh | {resume: <node>} | {fork: <node>} [yak/Archon `context`]
    consumes: [input.structure]    # artifacts read; must exist before start   [orc consumes]
    produces: [cells/]             # artifacts written; checked after success  [orc produces]
    output:                        # structured result, validated by the engine [Archon output_format; yak schema]
      schema: schemas/setup.json
      repair_attempts: 2           # same-session re-prompt with validation errors [yak §3.5]
    tools: { allow: [Read, Edit, Bash], deny: [WebFetch] } # mapped per harness; error if not enforceable [Archon allowed/denied_tools; gh-aw capabilities]
    effects:                       # permitted side-effects as validated intents [gh-aw safe-outputs]
      submit_job: { max: 4, partitions: [cpu] }
      noop: {}
    budget: { max_usd: 5, max_turns: 80 }   # [Archon maxBudgetUsd; orc max_turns; yak Budget]
    cache: strict                  # strict | loose | never (= Archon always_run) [yak; Archon]
  - id: relax
    kind: job                      # HPC job node — flower-specific
    depends_on: [setup-cells]      # node-id edges                             [Archon, orc]
    when: "$setup-cells.output.n_cells > 0"  # tiny expression lang, fail-closed [Archon `when`; yak jexl `skipIf`]
    trigger_rule: all_success      # [Archon]
    job: { cluster: hpc1, template: templates/vasp.sbatch.j2, resources: { nodes: 2, time: 24:00:00 } }
    wait: { poll: 10m, deadline: 7d }        # parks, holds no process     [Archon wait {event, deadline_ms}]
  - id: review-energies
    kind: gate
    depends_on: [relax]
    message: { file: prompts/review.md }     # rendered with upstream outputs  [yak `render`; Archon/orc `message`]
    decisions: [approve, revise, abort]      # answer schema; output {decision, text} [Archon `decisions`; yak gate schema]
    on_reject: { rerun: [relax], max_attempts: 3 }  # rework routes to nodes + $REJECTION_REASON [Archon/orc on_reject]
  - id: converge
    kind: loop
    body: [tweak-incar, rerun-scf]
    until: "$rerun-scf.output.converged == true"
    budget: { max_iterations: 5, no_progress: { signal: "$rerun-scf.output.max_force", rounds: 2 } } # [yak Budget]
    on_exhausted: suspend          # suspend (opens a gate) rather than fail    [yak; Archon/orc fail instead]
```

Run-level files follow yak's layout, with orc and Archon additions:
- `runs/<id>/plan.lock.yaml`: the frozen plan. Its header records `plan_digest`, the `prompt_digests` per node, the harness versions and the effect tools per node. This combines Archon's manifest digest, gh-aw's lock header and orc's `spec/`.
- `journal.jsonl`: the only source of truth.
- `artifacts/`, `pending/` and `signals/`.
- `nodes/<id>/<attempt>/{prompt.md, argv.json, transcript.jsonl, stderr.log, result.json}`. `argv.json` records the exact invocation, which none of the four projects keeps.

**Journal events.** Use yak's envelope `{t, at, runId}` plus orc's monotonic per-run `seq`. Proposed types:
- Run and plan: `run.started{plan_digest, pid, pgid}`, `run.resumed{pid}`, `run.suspended{reason}`, `run.finished{status, reason?}`, `plan.revised{rev, diff, digest}`, `plan.approved{rev, digest, by}`.
- Node lifecycle: `node.started{attempt, semanticKey, definitionKey, sessionRef?}`, `node.completed{artifactHash, cached, stale?}`, `node.skipped{reason: when|prior_success}`, `node.failed{failure}`, `node.invalidated{cascade, reason}` (the rerun verb).
- Jobs: `job.submitted{jobid, cluster}` and `job.state{state}`.
- Gates and waits: `gate.opened`, `gate.answered{decision, by}`, `wait.signaled{token}`.
- Cost: `budget.consumed{usd, tokens}`.

Harness stream events do **not** go in the journal. They go to per-attempt `transcript.jsonl`, wrapped in Archon's envelope `{attemptId, seq, observedAt, event}`. This mirrors Archon's split between the DB and the transcript, and yak's "sessions/ for debugging only".

**Replay and resume rule.**
1. Fold the journal. A later `node.started` or `node.invalidated` clears completion, as in yak.
2. Reuse a node only if its yak-style `semanticKey`/`definitionKey` still match, and the upstream artifact hashes still match. Matching on hashes gives Archon's value-equality invalidation for free.
3. Agent outputs are reused from `artifacts/` and the cache. They are never regenerated.

**Amendments** are new plan revisions: a `plan.revised` event plus a stored diff. They gate on the same answer mechanism, and approval binds to the digest. This follows the Smithers 1.0 PlanCard and the brief's approved-amendment requirement. Archon instead freezes the shape "a run does not change shape while it is running", and flower relaxes that only through approved revisions.

### 7.3 Harness adapter interface

The interface wraps `claude -p`, `codex exec --json` and pi. It is built from yak's `AgentAdapter`, Archon's `ProviderResult`, capabilities and failure classes, Smithers' `CliOutputInterpreter` and `AgentCliEvent`, and gh-aw's `CodingAgentEngine` split.

```ts
interface HarnessAdapter {
  id: 'claude' | 'codex' | 'pi' | string
  capabilities(): { sessionResume: boolean; sessionFork: boolean; structuredOutput: 'enforced'|'best-effort'|false
                    costReporting: boolean; costControl: boolean; toolAllowlist: boolean; bashAllowlist: boolean; mcp: boolean } // [Archon, gh-aw]
  preflight(): Promise<{ ok: boolean; version: string; detail?: string }>   // [orc version check; `pi auth check`]
  build(req: NodeRunRequest): Invocation          // PURE; result is journalled (argv.json) for audit/replay
  interpret: { onStdoutLine(l): HarnessEvent[]; onStderrLine?(l): HarnessEvent[]; onExit(code, signal): HarnessEvent[] } // [Smithers]
  finalize(events: HarnessEvent[], files: Record<string,string>): NodeResult
}
interface NodeRunRequest { runId; nodeId; attempt; cwd; prompt: string; systemAppend?: string; model?; effort?
  tools?: { allow?: string[]; deny?: string[] }; outputSchema?: JSONSchema
  session: { mode: 'fresh' } | { mode: 'resume' | 'fork'; ref: SessionRef }; sessionId?: string /* pre-assigned */
  budget?: { maxUsd?; maxTurns? }; timeouts: { totalMs; idleMs }; artifactsDir; env: Record<string,string> }
interface Invocation { argv: string[]; env: Record<string,string>; stdin: string | 'ignore'; files: Record<string,string> /* schema, ext */
  cancel: { first: 'SIGINT'|'SIGTERM'; graceMs: number } }
type HarnessEvent =                                // [Smithers AgentCliEvent]
  | { type: 'started'; sessionRef: SessionRef } | { type: 'action'; kind: 'tool'|'command'|'file_change'|'message'|'reasoning'|'warning'; phase; title; detail? }
  | { type: 'usage'; usage: Usage } | { type: 'completed'; ok: boolean; text?: string; structured?: unknown; error?: string }
interface NodeResult { ok: boolean; stopReason: 'complete'|'max_turns'|'budget'|'timeout'|'cancelled'|'error'
  text: string; structured?: unknown; sessionRef: SessionRef /* {id, path?} */; filesChanged: string[]
  usage: { input; output; cacheRead?; cacheWrite?; costUsd?: number; costSource: 'harness'|'price-table' }
  failure?: { class: 'auth'|'quota'|'budget'|'misconfigured'|'rate_limited'|'transient'|'schema_invalid'|'tool_denied'|'timeout'|'cancelled'|'unknown'
              retryAfterMs?; detail } }                                     // [Archon failure classes + yak StepFailure]
```

**The engine owns the generic parts**, so adapters stay small:
- Spawning in a new process group, with `detached` and the pgid journalled.
- The total and idle timers.
- Cancellation: the adapter's first signal, then SIGTERM to the process group, then SIGKILL.
- The schema repair loop, used when structured output is best-effort or validation fails.
- Retries by failure class.
- Writing the transcript.

**Per-harness `build`/`interpret` mapping:**

| | claude | codex | pi |
|---|---|---|---|
| argv | `claude -p --output-format stream-json --verbose --permission-mode bypassPermissions --setting-sources project [--model M] [--session-id <uuid> \| --resume <id> [--fork-session]] [--json-schema '<schema>'] [--max-budget-usd X] [--tools …\|--allowed-tools …] [--append-system-prompt F]` [orc, Smithers, gh-aw; flags verified on 2.1.284] | `codex exec --json --cd C --skip-git-repo-check --sandbox workspace-write\|danger-full-access -c approval_policy="never" [--model M] [-c model_reasoning_effort=…] [--output-schema F] -o last.txt -` ; resume: `codex exec resume <thread_id> … -` (no `--output-schema`) [orc, Smithers, gh-aw] | `pi --mode json --session-dir R/sessions/pi --session-id <id> --no-approve [--model p/id] [--thinking L] [--tools …] [-e submit_result.ts] [--append-system-prompt F]`; resume `--session <path>` or `--fork` [pi docs, gh-aw] |
| prompt | stdin (stream-json user message, as orc does) or positional arg | stdin with `-` | stdin then EOF (never leave it open) |
| session ref | `system/init.session_id`; can be **pre-assigned** with `--session-id` | `thread.started.thread_id` | header line `id` + derived file path; pre-assigned |
| final text | `result.result` | `-o` file or the last `agent_message` | last assistant `message_end` text |
| structured | `result.structured_output` (enforced) | `--output-schema` (enforced; strict-mode normalize) | `submit_result` tool → `tool_execution_end.result.details` (best-effort) |
| usage/cost | `result.usage`, `total_cost_usd` | `turn.completed.usage` (tokens only → price table) | sum `message_end.usage.cost.total` |
| failure signals | `result.subtype` (`error_max_turns`, `error_max_budget_usd`, …), `permission_denials`, exit≠0 | `turn.failed`, `error`, exit≠0 | `stopReason` error/aborted (**exit 0!**), exit 1/143 |
| cancel | SIGINT, then process group SIGTERM, then SIGKILL [orc] | same | **SIGTERM** (no SIGINT handler) |

### 7.4 Gate and wait mechanics to adopt

1. **Gate = yak's file protocol + Archon's and orc's semantics.**
   - Reaching a gate writes `pending/<node>.request.json`, discriminated by `kind`: gate, loop-exhausted, budget or amendment. It journals `gate.opened` and the process **exits** with a distinct code (yak 78; Smithers uses 3 for waiting).
   - The answer comes from any frontend writing `pending/<node>.answer.json`, or from `flower approve|reject|answer <run> <node> [--reason]`. The answer is validated against the gate's schema, and `gate.answered{decision, by, notes}` is journalled.
   - Open gates are derived from the journal, not from which files exist [yak].
   - Approval **does not auto-continue in `--json` mode**. Keep the explicit `resume` two-step [orc, Archon]. A human-mode convenience flag can continue inline.
   - Rework: `on_reject` re-runs named nodes with `$REJECTION_REASON`, bounded by `max_attempts` (default 3) [Archon]. When attempts run out, cancel and record that outcome.
   - `flower pending` lists every open gate across runs [yak].
   - The skill must carry orc's rule verbatim: *"The decision is the user's. Never run approve or reject on your own judgment."*
2. **Budget and loop exhaustion suspends rather than fails** [yak §3.3, §9 #9]. It opens a `loop-exhausted` request with the fixed answer `{action: continue|abort, addIterations?}`.
3. **Waits and jobs park; no owner process stays alive.**
   - Archon keeps an owner process alive and uses a server scan only for recovery. flower should instead use a stateless `flower tick` scanner, modelled on CatGo, run from cron, scrontab or a systemd timer, or from an optional `flower serve` loop.
   - The scanner (a) polls Slurm (`sacct`/`squeue` over SSH) for `job` nodes, (b) applies signals, (c) fires time and deadline expiries, and (d) resumes the runs that became ready.
   - Signals use Archon's occurrence-token idea. The wait records a `token`, and the signal must quote it: `flower signal <run> <node> --token T --data @payload.json`, or a file drop into `signals/`. A Slurm epilogue can call it, and duplicate or stale signals are no-ops.
   - Archon's expiry semantics apply: `deadline` → `wait.expired`, which completes with `status: expired` and does not fail, so downstream `when:` can branch on it.
   - An `attention` wait is never auto-resumed [Archon].
4. **Cancel.** Journal the pid and pgid at start [yak, orc]. `flower cancel` signals the process group with a grace period, `scancel`s any submitted job ids, then journals `run.finished{status: cancelled}`. It is idempotent [yak], and it must **not** run `git reset --hard` (orc's destructive stop is an anti-pattern).
5. **Rerun a node and its downstream.** None of the three ships this; orc and Archon lack it, and yak's version exists only in its spec. Implement it as a journal event `node.invalidated{node, cascade: true}`, which the fold honours, followed by `resume`. This is Smithers' `retry-task` semantics on top of yak's journal.
6. **Outer-agent surface.** Install one skill into both `.claude/skills/flower/` and `.agents/skills/flower/` [orc], with version staleness checked via `metadata.version`.
   - Structure: a router to sub-docs [Archon, gh-aw].
   - Instructions to keep:
     - launch with `--detach --json`, then `flower wait <run> --json` as a background task, never polling [Archon, orc];
     - check the brief before launch [Archon's six items];
     - validate the plan until it is valid [orc];
     - a completed run is not necessarily a success, so check `outcome` [Archon].
   - Optional MCP: expose `status, validate, logs, audit, pending, answer` [gh-aw `mcp-server`], with the decision tools gated behind a confirmation.
