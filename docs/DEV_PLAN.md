# forgeflow — Development Plan (draft v0.1)

Date: 2026-10-02 · Status: draft for review · Working name, easy to rename before the first release.

> **Implementation status (2026-10-03): M0–M6 implemented.**
>
> **Built:**
> - **Core:** the store and journal (flock, `seq`, idempotency, torn-line repair), the plan model and
>   validation, the engine, and the detached process runner.
> - **Node kinds:** `shell`, `function`, `agent` (claude, codex, pi and script harnesses), `job` (Slurm over
>   local/ssh), `gate` and `wait`, plus `foreach`.
> - **Plan changes:** amendments with compare-and-swap and policies; `rerun`; cache reuse; cross-run
>   `fork`/replay.
> - **Readability:** reports (Markdown/HTML), RO-Crate export.
> - **Interfaces:** the CLI with its JSON envelope and exit codes, a skill, and an MCP server.
>
> **Validation:** 653 tests, plus a real end-to-end run with Codex agents (`docs/demo/`).
>
> **Deviations from the plan below:**
> - **Language:** stdlib argparse instead of typer/click, and no pydantic. The only dependencies are
>   pyyaml and jsonschema.
> - **Ownership:** a gate's on_reject `max_attempts` counts rework rounds.
> - **Harnesses:** pi is tested against recorded fixtures only, because it is not installed here. Claude
>   could not be run for real on this host, because its stored login has expired.

This plan is assembled from existing practice, not invented. Every design decision cites where it comes
from. The evidence lives in `context/notes/`:

| Note | What it covers |
|---|---|
| `notes/prior-art-survey/REPORT.md` | Survey of ~60 projects. Nothing combines everything below; Smithers is closest. |
| `notes/eleforge-reuse.md` | What to LIFT / ADAPT / LEAVE from Eleforge (LabFlow, Slurm/SSH, agent harness), plus ~35 commit-cited pitfalls. |
| `notes/smithers.md` | Smithers 0.x and 1.0, read at source level: harness argv, durability, control verbs, plan digests, park-on-job. |
| `notes/agent-runners.md` | Archon, orc, yak, gh-aw and pi: plan-file schemas, the harness adapter interface, gates and waits. |
| `notes/hpc-execution.md` | jobflow-remote, dpdispatcher, psij, snakemake-slurm, aiida, catgo: job state machine, idempotent submit, polling. |
| `notes/workflow-semantics.md` | jobflow `Response`, DBOS replay, AiiDA provenance, the event vocabulary, the replay rule. |

Reference clones are in `context/repos/` (git-ignored). Re-create them with `context/fetch-repos.sh`.

---

## 1. What forgeflow is

A **lightweight, file-first workflow CLI for long-running research**. Coding agents (Claude Code, Codex,
pi) and people use it to plan, run, monitor and grow multi-step computations. Typical work is DFT/MD
chains on Slurm that run for days, but nothing in the core is domain-specific.

- **The plan is a contract.** The user and a planning agent iterate on `plan.yaml` until it validates.
  The user approves its digest. Only then does it run. *(Smithers 1.0 PlanCard; Spec Kit / Kiro
  approve-before-execute)*
- **Mixed node kinds.** `agent` nodes run a headless harness with no fixed tool list. Next to them sit
  `shell`, `function`, `job` (HPC), `gate` (human decision) and `wait`. You use agents where judgment
  is needed and deterministic nodes where reproducibility is needed.
- **Durable without a daemon.** State is `plan` + `events.jsonl`. Every command folds the log. A Slurm
  job is "parked" and holds no process. A stateless `forgeflow tick` advances the run, called from cron,
  `forgeflow watch` or the outer agent. *(catgo stateless scanner; Smithers park-on-job; dpdispatcher
  `exit_on_submit`)*
- **Grows mid-run.** An agent or the user proposes an amendment. The user approves it. It is appended
  as a new plan generation, and history is never rewritten. *(Smithers plan-store generations + jobflow
  `Response` verbs)*
- **Replayable and auditable.** Replay reuses recorded results, including agent outputs. Every attempt
  records inputs hash, argv, harness version, model, transcript, cost and rationale. *(DBOS
  record-and-reuse; yak two-key cache; AiiDA data vs logical provenance)*
- **Driven by an outer agent.** The CLI emits JSON and uses fixed exit codes. A skill teaches the
  protocol. An optional MCP server exposes the same verbs, but never the approval verbs. *(Smithers
  incur CLI + skill + MCP; gh-aw `mcp-server`/`audit`)*

### Non-goals
- **Not another agent harness.** There is no LLM loop and no tool registry. Reasoning happens inside
  existing harnesses, as Eleforge decided on 07-02 (`doc/design/external-agent-brain.md`).
- **Not a scheduler or a cluster manager.** forgeflow submits to Slurm; it does not replace it.
- **No server, database or UI in the core.** Eleforge's web UI can become a viewer on top later (M6).
- **No convergence or scientific judgment inside `job` nodes.** That belongs to a downstream `function`
  or `agent` node. *(catgo: "COMPLETED ≠ converged")*

### Design rule that resolves "agent vs. deterministic"
> **Agents decide; forgeflow executes durable effects.** An agent node produces files and a structured
> result. When it wants a long computation, it does not run `sbatch` itself. It emits an *amendment
> proposal* or a declared *effect intent*, and forgeflow turns that into a `job` node with idempotent
> submit, polling and provenance.
> *(gh-aw "safe outputs": the agent runs read-only and its side effects are validated JSONL intents)*

This rule keeps long waits and exactly-once submission in forgeflow, where they can be made durable,
instead of inside an agent session that can die.

---

## 2. Key design decisions

### D1. Language and packaging: Python ≥ 3.10, one pip package, a few small dependencies
- **Why Python.** Science users and their tools (ASE, pymatgen, jobflow) are Python. Eleforge's
  reusable code is Python (LabFlow, Slurm/SSH). Smithers is TS/Bun, which is foreign to HPC users.
  (`smithers.md` §8)
- **Dependencies:**
  - `pydantic` for models and schemas;
  - `pyyaml` for plans;
  - a CLI framework (`typer` or `click`).

  Nothing else in the core: no DB, no message broker, no ORM.
- Distribution: `pipx install forgeflow` / `uv tool install forgeflow`. The CLI entry point is `forgeflow`,
  with the alias `ff`.

### D2. On-disk layout (file-first, the log is the only source of truth)
```
<project>/.forgeflow/
  plans/<plan-id>/plan.yaml                     # editable draft (contract)
  runs/<run-id>/
    plan.lock.yaml                              # frozen gen-0 plan + digests (approved)
    amendments/<gen>.yaml                       # approved plan growth, append-only
    events.jsonl                                # append-only, fsync'd, flock'd — ONLY source of truth
    lock                                        # flock target {pid, host, started_at}
    pending/<id>.request.json | .answer.json    # gates (file protocol)
    signals/                                    # external signal drop-box
    artifacts/<node>/...                        # declared outputs
    nodes/<node>/a<N>/{prompt.md, argv.json, transcript.jsonl, stderr.log, result.json, job.sh, scheduler/}
    cache/clusters/<name>.json                  # poll bookkeeping (derived, deletable)
```
**Sources.**
- yak: `journal.jsonl` as the only state; `pending/` files.
- orc / Archon: frozen spec per run.
- `hpc-execution.md` §8.2: attempt directories.
- New in forgeflow: `argv.json`. None of the surveyed runners records the exact invocation.

**Why not SQLite.** WAL mode is unsafe on NFS/Lustre, where HPC home directories live (`smithers.md` §1).
Eleforge's "SQL first, files as a mirror" never converged (`eleforge-reuse.md` §5).

### D3. Event envelope and vocabulary: a superset of LabFlow
Keep LabFlow's envelope (`eventId, runId, eventType, occurredAtIso, nodeRunId, attemptId, actorType,
actorId, payload`) and add five fields:
- `seq`: contiguous; assigned under `flock`.
- `idempotencyKey`: duplicates are dropped on append.
- `writerEpoch`: fencing.
- `planGeneration`.
- `schemaVersion`.

The vocabulary is closed. It is listed in full in `workflow-semantics.md` §5.3:
`run.*`, `plan.{proposed,approved}`, `plan.amendment.{proposed,approved,rejected}`,
`node.{ready,started,submitted,observed,output,succeeded,failed,skipped,stale,superseded,cancel.*}`,
`gate.{requested,answered}`, `agent.decision`, `job.*` (`hpc-execution.md` §8.2).

**Fixes to LabFlow gaps** (`eleforge-reuse.md` §2):
- add the missing `node.succeeded`, `node.skipped` and `node.cancelled`;
- emit no event types through f-strings;
- payloads are deltas, not snapshots.

A `--labflow-compat` projection keeps Eleforge able to read runs.

### D4. Plan file schema: YAML, with every convention borrowed
Full sketch in `agent-runners.md` §7.2. Summary:
- **Top level:** `forgeflow: 1`, `id`, `description`, typed `inputs`, `defaults` (harness, timeout
  `{total, idle}`, retry by failure class, concurrency).
- **Nodes:** `id`, `kind ∈ {agent, shell, function, job, gate, wait, loop, map}`, `depends_on`, `when`,
  `trigger_rule`, `consumes` / `produces`, `output.schema`, `budget`, `cache: strict|loose|never`.
- **`agent` nodes:** `prompt.file`, `context: fresh|resume|fork`, `tools.allow/deny` (an error if the
  harness cannot enforce them), `effects` (permitted intents), `output.repair_attempts`.
- **`job` nodes:** `cluster`, `template`, `resources`, `restart` mapping, `wait {poll, deadline}`.
- **`gate` nodes:** `message`, `decisions`, `on_reject {rerun, max_attempts}`.
- **Output references:** `${node}.outputs.key[.path]`. They resolve to the latest succeeded revision,
  and the resolved hash is frozen into `node.started` (jobflow `OutputReference`, `which="last"`).
- **Validation errors:** `{code, path, message, suggestion}`, so an LLM can fix them in a loop (LabFlow
  `PlanLoadError`; orc's validate-until-valid skill rule).

### D5. Approval and amendments: digest-bound, append-only generations
- `forgeflow plan approve` binds the approval to `baseDigest` (canonical JSON + SHA-256; LabFlow
  `template_cas.py`, or RFC 8785).
- An amendment carries `(parentGeneration, parentDigest)`. It is applied by compare-and-swap, which
  gives `generation+1` and a new `digest`; `baseDigest` stays unchanged. (Smithers `Plan.append`,
  `PlanStore` CAS)
- **Op vocabulary from jobflow `Response`:**
  - `add` (addition);
  - `detour` (insert before existing children, with jobflow-remote `_append_flow` rewiring);
  - `replace` (keeps the node id and bumps its revision, so references follow);
  - `stop_children`;
  - `stop_run`;
  - `supersede` (for an already-completed node: its downstream cone becomes stale).
- **Difference from Smithers.** Every amendment goes through a gate, either a human or a
  pre-authorised policy such as "agent may add ≤3 analysis nodes under $5". Smithers lets growth ride
  the base approval; our brief asks for approved and recorded amendments. Changes are shown to the
  reviewer as `PlanDiff {added, removed, rekeyed(changed fields)}` (Smithers `PlanDiff`).

### D6. Replay and reuse rule: three contexts, not prefix replay (`workflow-semantics.md` §3)
1. **Resume (same run, same generation).** Every terminal recorded result is final. An in-flight job is
   re-attached by its Slurm id or submit key, never resubmitted. An in-flight *agent* attempt whose
   driver died is cancelled and retried as attempt N+1. Its resume pointer is dropped: "costs
   conversation context, never work". (Smithers #1610)
2. **Amend (same run).** Only pending nodes change. Changing a done node requires `supersede`, which
   makes its downstream cone stale. A stale node is skipped if both `decl_hash` and `input_hash` still
   match (early cut-off).
3. **Fork / new run.** A node is reused when `(decl_hash, input_hash)` both match (yak two-key cache).
   `--loose` reuses on input match alone and marks the node `stale`. Cross-run reuse is opt-in, and
   only for `function`/`shell`/`job` nodes. Agent outputs are reused within a run lineage, but not
   across unrelated runs unless asked.
- `decl_hash` leaves out scheduler resources (AiiDA hash-ignored attributes).

### D7. `job` node: port Eleforge's Slurm/SSH code, harden it with aiida and snakemake algorithms
Full design in `hpc-execution.md` §8.
- **Transport.** LIFT Eleforge's stdlib OpenSSH layer (`apps/backend/compute/remote/{shell,command,errors}.py`,
  `remote/ssh/openssh.py`). It uses `~/.ssh/config` aliases. Automation never starts a ControlMaster;
  it checks the user's master and falls back. (Eleforge commits dbb0fd2d, e372517d, 0d06b919)
- **States.** 13 states: PREPARED → STAGING → SUBMITTING → QUEUED → RUNNING → EXITED → RETRIEVING →
  COMPLETED / FAILED{TIMEOUT, OOM, NODE_FAIL, PREEMPTED, …} / CANCELLED, plus REMOTE_ERROR and LOST.
  "Exited, no verdict yet" is a distinct state (jobflow-remote RUN_FINISHED, aiida DONE).
- **Idempotent submit.**
  - Write-ahead `job.submit_intent`, then a remote marker.
  - One SSH call: dedupe by `--job-name=<submit_key>` in `squeue`/`sacct`, then
    `sbatch --parsable --comment=<fingerprint>`, then write `.forgeflow/jobid` atomically.
  - An in-job mkdir guard against duplicates.

  This combines Eleforge (the only surveyed system that closes the crash window) with dpdispatcher's
  job-id file.
- **Polling.**
  - Batched `squeue`, then `sacct` for jobs that left the queue, then work-dir evidence (`ec`, `done`,
    `cancelled`).
  - Tolerate `sacct` lag; a miss counter leads to LOST; an adaptive 40–180 s interval (snakemake).
  - A per-cluster minimum poll interval (aiida).
  - Poll errors never change verdicts (unlike psij).
- **Fix Eleforge's Slurm state map bugs** (`eleforge-reuse.md` §4.3):
  - "CANCELLED by uid" maps to running;
  - TIMEOUT maps to canceled;
  - PREEMPTED and BOOT_FAIL fall to running;
  - `ExitCode` is never read from sacct.

  Write a table-driven unit test.
- **Retry vs rerun** (jobflow-remote).
  - `retry` redoes a stuck infra step.
  - `rerun` creates a new attempt.
  - The automatic retry policy is keyed on the typed failure reason. `OOM` / `EXIT_NONZERO` are never
    retried blindly; they emit an amendment proposal.
- **Data hand-off between jobs** (Eleforge "band hand-off gap": nscf cannot see the SCF charge
  density). `consumes` can reference a remote path from an upstream attempt. forgeflow stages it
  remote-to-remote or symlinks it in the same filesystem, not via a download/upload.
- **Dependencies:** none. psij is an optional future backend for running *on* the login node.
  dpdispatcher is LGPL and its model fights an amendable DAG. aiida and jobflow-remote are too heavy.

### D8. `agent` node: harness adapters behind one interface
Interface in `agent-runners.md` §7.3; argv details in `smithers.md` Appendix A.
- `HarnessAdapter = {capabilities, preflight, build(req) → Invocation (pure, journalled as argv.json),
  interpret(line) → HarnessEvent[], finalize → NodeResult}`.
- The engine owns the generic parts: process-group spawn with a parent-death guard, total and idle
  timeouts, SIGTERM→SIGKILL escalation on the group, the schema-repair loop, retry by failure class,
  and transcript capture.
- **First adapters:**

  | Harness | Invocation | Final result |
  |---|---|---|
  | claude | `claude -p --output-format stream-json --verbose …` | `result.result` / `structured_output` |
  | codex | `codex exec --json … -` | `-o last.txt` |
  | pi | `pi --mode json --session-id …` | `stopReason`; **exit 0 on error** |

- **Pitfalls taken from Smithers and Eleforge** (`smithers.md` A §8, `eleforge-reuse.md` §8):
  - pass argv as an array, never as a shell string;
  - put prompts on stdin or in a file (argv is capped at 128 KB);
  - keep the stdout **tail** when output is capped;
  - strip `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT`;
  - decide explicitly whether to pass `ANTHROPIC_API_KEY` or clear it;
  - keep secrets off argv; use a 0600 settings file;
  - detect quota or session-limit banners that exit 0 (park until the reset time; do not fail);
  - treat auth/config errors as non-retryable;
  - on session loss, drop the resume id;
  - set the recursion guard env `FORGEFLOW_INSIDE_RUN`; inside a node the skill forbids driving forgeflow;
  - never write the user's credential files (Eleforge token-rotation incident).
- **Structured output.**
  - Prompt-inject the schema.
  - Extract in a fixed order: whole text, then last fence, then last balanced JSON.
  - Validate with pydantic.
  - A correction budget (default 3), separate from task retries, that resumes the session.
  - Use Codex `--output-schema` only for non-agentic nodes (it blocks tool calls).
- **Provenance per attempt:**
  - `decl_hash`, `input_hash`, resolved inputs, harness and version, model;
  - `sessionRef`, `transcript{path, sha256}`;
  - usage and cost, with `costSource` either harness or price table;
  - `rationale`, which the node is asked to return in its structured output (DREAMS v2 per-parameter
    justification).

### D9. Gates and waits: file protocol, explicit two-step resume
- Reaching a gate writes `pending/<id>.request.json`, journals `gate.requested`, and the run exits with
  code `3` ("parked"). The request kinds are node, plan, amendment, loop-exhausted and budget.
- The answer is written either as `pending/<id>.answer.json` (any frontend) or with
  `forgeflow answer|approve|reject`. It is validated against the gate's schema, and `gate.answered`
  is journalled.
- Open gates are derived from the log. (yak)
- An answer does not auto-continue in `--json` mode; `resume` is a separate step. (orc, Archon)
- When a loop or budget is exhausted, the run **suspends** with a gate instead of failing. (yak)
- `wait` nodes use tokenised signals (`forgeflow signal <run> <node> --token T`). A deadline expiry is
  a status the plan can branch on with `when:`, not a failure. (Archon)

### D10. Outer-agent surface
- **CLI output.** Every command takes `--json`. The output is `{ok, data | error{code, message,
  details}}`, and non-TTY output includes a `next` block of suggested commands (Smithers incur
  envelope and CTA).
- **Exit codes.** One vocabulary (Smithers 1.0):

  | Code | Meaning |
  |---|---|
  | 0 | ok |
  | 1 | failed |
  | 2 | usage error |
  | 3 | parked (gate, wait or job pending) |
  | 130 / 143 | signals |

- **Verbs:**
  - `plan new | validate | diff | approve`
  - `run [--detach]`, `status [--refresh]`, `watch`, `tick [--all-runs]`, `wait <run>` (block until
    terminal or parked; meant for harness background tasks)
  - `logs <node>`, `output <node>`, `pending`, `answer`, `approve`, `reject`, `signal`
  - `cancel [<node>]`, `resume`, `retry <node>`, `rerun <node> [--downstream]`
  - `amend propose | approve | reject`
  - `fork <run> [--from <node>]`, `replay` (pure fold, no execution)
  - `audit <run>` (stable JSON schema; gh-aw)
  - `export --ro-crate`
- **Skill** (`skills/forgeflow/SKILL.md`), installed into `.claude/skills/` and `.agents/skills/`. Key
  rules, each borrowed:
  - Rule 0: if `FORGEFLOW_INSIDE_RUN` is set, never drive forgeflow (Smithers).
  - Validate the plan until it is valid (orc).
  - "**The decision is the user's. Never approve on your own judgment**" (orc).
  - Launch detached, then `forgeflow wait` as a background task; never poll in a loop (Archon).
  - "Admission is not completion" (Smithers 1.0).
  - Act on the `next` block.
  - For multi-goal work, route by shape, not size.
- **MCP** (later). The same verbs, with `readOnlyHint`, a `--read-only` mode, and the **approval verbs
  withheld** so an agent cannot approve its own plan (Smithers 1.0).

### D11. Concurrency and fencing
- Every mutating command takes `flock(runs/<id>/lock)`.
- `writerEpoch` fences stale writers.
- Owner ids are `pid@host:session`. A holder on the same host whose pid is dead is broken
  automatically. A holder on a different host is reported, and only broken with `--break-lock`
  (Smithers liveness classifier; jobflow-remote `break_lock`). This matters on HPC, where login nodes
  rotate.

---

## 3. Reuse map

| Source | What | How | License |
|---|---|---|---|
| Eleforge `packages/labflow` `events.py`, `repository.py`, `plan.py`, `template_cas.py`, golden fixtures | Envelope, pure fold, atomic plan write, canonical hashing | **ADAPT**: add a cross-process flock, `seq`, fail-closed appends, the new vocabulary; drop the `nanoforge_runtime` import | in-house |
| Eleforge `compute/workflows/workflow_state_machine.py:5-34` | 9-state node transition tables | **LIFT**; add per-attempt state | in-house |
| Eleforge `compute/remote/*`, `remote/ssh/openssh.py` | OpenSSH command layer | **LIFT** | in-house |
| Eleforge Slurm runner (sbatch generator, idempotent submit, poll, harvest, LOST watchdog) | Job backend | **ADAPT** + fix the state-map bugs | in-house |
| Eleforge `subgraph_expander.py`, `workflow_manager_sweep.py:170-287` | `parent.inner` ids; manifest before fan-out | **REFERENCE** for `map` and amendments | in-house |
| Eleforge `tools/pi-brain`, claude invocation | Harness argv discipline, pi stopReason handling | **REFERENCE** | in-house |
| Eleforge SQL stores, dispatch queue, outbox, Redis leases, `platform_tools.py` | — | **LEAVE** | — |
| Smithers 0.x / 1.0 | Harness adapters, error classifiers, plan digest and CAS, PlanDiff, park-on-job, exit codes, skill rules | **Design + small ports with attribution**. Avoid `crates/smithers-ffi` (AGPL) | MIT |
| yak | Journal-only state, two-key cache, file gates, adapter interface | Design + small ports | MIT |
| Archon / orc | YAML schema conventions, approval `on_reject`, `wait` nodes, CLI argv for claude and codex | Design | MIT |
| gh-aw | Safe-output effect intents, `audit --json`, MCP tool set | Design | MIT |
| jobflow / jobflow-remote | `Response` amendment ops, `OutputReference`, retry vs rerun | Design + small ports | BSD-3 (+LBNL clause) |
| aiida-core | Scheduler polling batching, min poll interval, hash-ignored attributes, provenance split | Algorithms ported with attribution | MIT |
| snakemake-executor-plugin-slurm | sacct lag and MinJobAge handling, adaptive interval, fail states | Algorithms ported with attribution | MIT |
| DBOS | Fork/rewind semantics, record-and-reuse | Design | MIT |
| catgo | Stateless scanner, PENDING_REVIEW gate before HPC spend | **Design only, no code** | AGPL-3.0 |
| dpdispatcher, dflow | Job-id file, transient-error strings | **Design only** | LGPL-3.0 |
| psij-python | `JobState` shape, `.ec` file convention | Optional future dependency | MIT |

---

## 4. Milestones

Each milestone ends with a demo that runs and a short record in `docs/milestones/`.

### M0. Bootstrap (≈1 week)
- Package skeleton (`src/forgeflow/{store,model,engine,executors,cli}`), ruff, pytest, CI.
- Decide name and license (§6).
- **Store:** port LabFlow's `events.py`, `repository.py`, `plan.py`, `template_cas.py` into
  `forgeflow.store`, with flock + `seq` + idempotency keys + fail-closed reads. Port the golden
  fixtures through a name mapping.
- **Done when:** golden fold tests pass. Concurrent appenders (multiprocess test) never interleave or
  duplicate. Truncating the last line is tolerated; a corrupt earlier line is fatal.

### M1. Contract + local engine (≈2–3 weeks)
- Plan schema (pydantic → JSON Schema). `plan new/validate/diff/approve`, a digest bound to the
  approval, and the frozen `plan.lock.yaml`.
- Engine: fold → ready set → dispatch. Node kinds `shell`, `function`, `gate`, `wait`.
  `run / status / watch / tick / wait / cancel / resume / retry / rerun --downstream / pending / answer`.
- Lift the state machine; per-attempt state; downstream invalidation with `node.stale`.
- **Done when:**
  - A crash-injection suite (`kill -9` the driver between every pair of events) always resumes to the
    same final state with no duplicated side effects.
  - A gate parks with exit 3 and resumes after `answer`.
  - `rerun --downstream` invalidates exactly the dependent cone.

### M2. Agent nodes (≈2–3 weeks)
- `HarnessAdapter` plus `claude`, `codex` and `pi` adapters. Process-group spawning, timeouts,
  cancellation.
- Structured output with the repair loop. Capture transcripts, `argv.json` and usage. Error
  classification (quota park, auth non-retryable, session lost).
- Provenance fields; `rationale` in the output contract.
- **Test harness:** fake CLIs (`fake-claude`, `fake-codex`, `fake-pi`) that replay recorded
  stream-json/JSONL fixtures, including exit-0 errors, banners, truncation and hangs. These run in CI
  without LLM keys. Real-harness smoke tests run behind an env flag.
- **Done when:**
  - A 3-node plan (agent → shell → agent) runs with all three harnesses.
  - `replay` reuses agent outputs with zero harness calls.
  - Killing an agent mid-run leads to a clean attempt N+1.

### M3. HPC `job` nodes (≈3 weeks)
- Lift OpenSSH; port the sbatch template, the idempotent submit protocol, batched polling, retrieval,
  the verdict rules, LOST and orphan sweep, cancel, and retry vs rerun with typed failure reasons.
- Remote-to-remote `consumes` hand-off, which fixes the band-structure gap.
- **Test harness:**
  - a **fake Slurm** (shell shims for `sbatch/squeue/sacct/scancel` over a local dir, with injectable
    lag, vanishing jobs and preemption) used over `ssh localhost` in CI;
  - a real-cluster acceptance run on `eleforge-4090` (relax → scf → nscf → bands for Si).
- **Done when:**
  - Killing forgeflow at each step of the submit protocol never yields 0 or 2 Slurm jobs for one
    attempt.
  - A job that vanishes from `squeue` is resolved correctly from `sacct` or evidence.
  - A 24 h job is tracked by `tick` from cron alone.

### M4. Dynamic DAG (≈2 weeks)
- `amend propose / approve / reject` with generations, compare-and-swap, PlanDiff and the ops `add`,
  `detour`, `replace`, `stop_children`, `stop_run`, `supersede`.
- Agent nodes may emit amendment proposals as declared effects.
- Pre-authorised amendment policies (e.g. "analysis nodes only, ≤ N, ≤ $X").
- `loop` (until + budget + suspend on exhaustion) and `map` (fan-out over an output list; manifest
  before fan-out).
- **Done when:**
  - A failed SCF (OOM) leads an agent to propose a `detour` (smaller k-mesh test). After approval it
    runs, and the original chain continues.
  - The full history (base + generations + who approved what) can be read back from the log alone.

### M5. Outer-agent surface (≈1–2 weeks)
- The `--json` envelope with `next` hints, fixed exit codes, `audit --json`.
- `skills/forgeflow/SKILL.md` with an installer (`forgeflow skill install`).
- An optional MCP server (`forgeflow mcp`), read-only by default, approval verbs withheld.
- **Done when** the validation experiment (§5) passes.

### M6. Provenance export + Eleforge integration (≈2 weeks, can overlap)
- `export --ro-crate`: Workflow Run RO-Crate / Provenance Run Crate, with a PROV-O mapping
  (`workflow-semantics.md` §4).
- A `--labflow-compat` projection, so the Eleforge canvas can display forgeflow runs. Long term,
  Eleforge's own `packages/labflow` can depend on `forgeflow.store` instead of keeping a fork.

---

## 5. Validation experiment: does it actually help?

This is the experiment proposed in the design discussion. It is the M5 acceptance test.

- **Task:** a 4-step DFT chain (relax → scf → dos → bands) on a real cluster, plus one injected failure
  that needs a plan change.
- **A:** Claude Code with plain bash/ssh only.
- **B:** Claude Code with the forgeflow skill.
- **Protocol:** force a *new session* after each step, and a context compaction mid-way.
- **Measure:**
  - recoveries without human help;
  - duplicate or lost submissions;
  - human interventions;
  - tokens spent;
  - whether a third person can reconstruct "what was run, why, and who approved it" from the run
    directory alone.
- **Pass:** B has zero duplicate submissions, recovers across every session boundary, and its audit
  trail answers all of the provenance questions; A does not.

---

## 6. Open decisions (need the user)

1. **Name.** `forgeflow` is a working name. Checking PyPI and GitHub availability is a 5-minute task
   before M0 ends.
2. **License.**
   - Recommendation: **Apache-2.0** (patent grant, common in science tooling) or MIT.
   - Either way it constrains us: no code from AGPL catgo or LGPL dpdispatcher/dflow; design only.
3. **Relation to Eleforge's `packages/labflow`.**
   - Recommendation: forgeflow is a separate package that copies and adapts LabFlow's store now (M0).
   - Eleforge migrates to depend on forgeflow later (M6).
   - The alternative, evolving `packages/labflow` in place, ties forgeflow to Eleforge's release cycle
     and its stalled refactor.
4. **First harness.**
   - Recommendation: build `claude` first (best-specified output, already installed here), then pi
     (Eleforge's default brain), then codex.
   - codex is not installed on this machine, so its flags are verified only from orc, Smithers and
     gh-aw source.
5. **Where agent nodes run.**
   - Recommendation: on the machine running forgeflow (a workstation or login node), never inside
     Slurm jobs. This avoids putting LLM credentials on compute nodes and respects login-node policies.
   - Some HPC centres forbid long-lived processes on login nodes. The no-daemon `tick` design handles
     that: cron or `scrontab` on the cluster, or ticks from the workstation over SSH.
6. **`function` nodes.** Do they import user Python in-process (fast, risky) or always run in a
   subprocess (safer, slower)?
   - Recommendation: a subprocess by default (`python -m forgeflow.exec_function`).

## 7. Risks

| Risk | Mitigation |
|---|---|
| Smithers 1.0 or Anthropic adds Slurm park-on-job and closes the gap | Stay small and science-specific (HPC hand-off, provenance export, Eleforge integration); keep the store format simple enough to interoperate |
| Harness CLIs change flags and output formats quickly | Adapters are tiny, versioned and preflight-checked; fake-CLI fixtures are recorded per version; `argv.json` makes drift visible |
| Login-node daemon policies | No daemon is required; `tick` from cron/scrontab or remotely |
| Double submission under concurrency | Write-ahead intent + submit key + in-job guard + flock/epoch; crash-injection tests (M3) |
| Agent nodes make results non-reproducible | Agents decide and forgeflow executes; deterministic `job`/`shell` nodes hold the computation; agent outputs are recorded and reused on replay |
| Scope creep toward a platform | The non-goals above; UI and DB stay in Eleforge |
