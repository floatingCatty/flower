# Closest prior art for an agent-per-node, durable, plan-as-contract research-workflow CLI (deep dive, as of 2026-10-02)

Idea under evaluation (shorthand used below, "the Idea"): a lightweight, agent-first workflow CLI where a research workflow is a DAG and every node is run by a headless coding-agent harness (`claude -p`, `codex exec`, pi, OpenHands). The DAG is a contract that the user and a planning agent iterate on and approve before it runs. It runs durably: it survives sessions, and a node may wait hours or days on Slurm. It is monitorable, supports cancel/resume/rerun per node, and can grow during execution through recorded and approved amendments. State is a plan file plus an append-only event log, so runs can be replayed and audited, and a replay reuses recorded agent outputs. An outer harness drives it through a CLI plus a skill or MCP.

Method: I shallow-cloned the repositories (smithersai/smithers, coleam00/Archon, github/gh-aw, tjdals12/orc, lchase/yak, BattModels/material_agent) and read the source and docs trees directly. I also downloaded the published Smithers 0.35.0 npm tarball, which bundles the full docs as `docs/llms-full.txt`, about 1.1 MB. The CatGo local clone was read directly. Repository metadata (stars, releases, total commit counts) came from the unauthenticated GitHub REST API on 2026-10-02/03 UTC. Where I cite a repo file, the URL is the `main` branch path on GitHub.

---

## 1. Smithers (smithersai/smithers): how much of the Idea does it already implement?

### Takeaway
Smithers is by far the closest prior art. Its **0.x line** (npm `smthrs`, last release v0.35.0 on 2026-08-17) already covers most of the Idea:
- TSX workflows in which each `<Task>` can be a CLI coding agent (Claude Code, Codex, Pi, and others, spawned as child processes)
- durable state in SQLite, with waits that hold no process
- approvals, per-node retry with downstream reset, and rewind, fork and replay that reuse persisted outputs
- an NDJSON event log, a CLI with agent-oriented output, an MCP server, and an installed skill
- a graph that is re-derived every frame, so it can grow while the run executes.

The gaps against the Idea:
- no plan-level approval contract in 0.x
- graph amendments are not an approved or recorded diff
- no Slurm/HPC support
- agent nodes are not mandatory.

The repo's `main` is now a **1.0.0-rc ground-up rewrite** on Effect. 1.0 adds an explicit **plan → PlanCard (digest) → approve → run** contract and an append-only plan store. It is pivoting toward a hosted "maintain your codebase" product, the 1.0 core agent is an in-process cell-loop agent rather than `claude -p`, and 0.x run state is not portable to 1.0.

### Cited Findings

**Identity, maturity and licensing**
- Repo `smithersai/smithers`: 428 stars, 51 forks, MIT, created 2026-01-05, last push 2026-10-03T02:10Z, 316 open issues. GitHub description: "agentic workflow framework for defining workflows in simple TypeScript configuration files and executing them quickly, durably, and reliably". — [GitHub API](https://api.github.com/repos/smithersai/smithers)
- About 15.8k commits in total (pagination of the commits API). The top contributor `roninjin10` (William Cory) has 9,734 commits; the next is `cookesan` with 45. All 50 most recent commits, all dated 2026-10-02, are by William Cory. In practice it is a single-maintainer project. — [contributors API](https://api.github.com/repos/smithersai/smithers/contributors), local `git log`
- Latest GitHub releases: v0.35.0 (2026-08-17), v0.34.0 (2026-08-13), v0.33.0 (2026-08-02). The CHANGELOG has a `1.0.0-rc.1 (2026-09-22)` section, "3650 commits since v1.0.0-rc.0". — [releases API](https://api.github.com/repos/smithersai/smithers/releases), [CHANGELOG.md](https://github.com/smithersai/smithers/blob/main/CHANGELOG.md)
- npm: `smthrs` latest is 0.35.0, published 2026-08-17. Its description: "Multi-agent workflows with full observability and time travel: watch every step live, rewind, fork, and replay any run. Claude Code, Codex, Gemini, Hermes, OpenClaw, any model or harness." The earlier package `smithers-orchestrator` has 92 versions, the last being 0.32.0 on 2026-08-01. — [npm registry smthrs](https://registry.npmjs.org/smthrs), [npm smithers-orchestrator](https://registry.npmjs.org/smithers-orchestrator)
- The current `main` README has been repositioned: "Smithers maintains your codebase. It turns issues into reviewed, tested changes…". It points to a hosted app with Free/Pro pricing. "The 1.0 release candidate is not on npm. Install it from the source checkout", and the CLI is `smthrs init change` / `smthrs flow start change`. — [README.md](https://github.com/smithersai/smithers/blob/main/README.md)
- The 0.x → 1.0 migration tool says 1.0 "rewrites a JSX-era project onto Flow, Action, and Effect". It "never rewrites or resumes 0.x run state". 1.0.0-rc.0 "never loads, resumes, or migrates a 0.x run database". — [apps/docs/migrate index](https://github.com/smithersai/smithers/blob/main/apps/docs/migrate/src/content/docs/index.md), [replacements.md](https://github.com/smithersai/smithers/blob/main/apps/docs/smthrs/src/content/docs/replacements.md)

**Node model (0.x)**
- A workflow is a JSX tree: `<Workflow>`, `<Task>`, `<Sequence>`, `<Parallel>`, `<Branch>`, `<Loop>`, plus approvals, signals, timers, sagas and sandboxes. Each task has a Zod-validated output persisted to SQLite. — [smthrs README on npm](https://www.npmjs.com/package/smthrs)
- A Task has three modes. Agent mode calls an LLM. Compute mode runs a function. Static mode writes a literal value. Non-agent nodes are therefore allowed and are first-class. — smthrs-0.35.0 tarball `docs/llms-full.txt` §"Tasks: three modes" ([tarball](https://registry.npmjs.org/smthrs/-/smthrs-0.35.0.tgz))
- "CLI / full-OS agents run as a child process. `ClaudeCodeAgent`, `CodexAgent`, `OpenCodeAgent`, and every other CLI agent extend `BaseCliAgent`, which spawns the vendor binary via `node:child_process`." SDK agents (`AnthropicAgent`/`OpenAIAgent`) run in-process. — `docs/llms-full.txt` §"Where agents run & what's billed" (same tarball)
- The shipped adapters include `ClaudeCodeAgent`, `CodexAgent`, `CursorAgent`, `PiAgent` (Pi RPC mode), `NanocodexAgent`, `AntigravityAgent`, `GeminiAgent`, and `AnthropicAgent`/`OpenAIAgent`. A `PoolAgent` wraps `pool exec` using ACP-compatible NDJSON. — [smthrs README](https://www.npmjs.com/package/smthrs); `docs/llms-full.txt` lines on PoolAgent
- For CLI agents without native structured output, including `ClaudeCodeAgent`, Smithers injects JSON instructions, extracts and validates the JSON, and retries when validation fails. `agent={[primary, fallback]}` gives fallback chains. — `docs/llms-full.txt` §"Tasks: three modes"
- Agent session snapshots persist, and `fork="plan"` starts a task from a copy of another task's conversation. — `docs/llms-full.txt` §"Session snapshots & fork"

**Node model (1.0 rc on `main`)**
- "A model call is an ordinary action". `AgentAction.make(...)` declares a model-backed step with a `seat` such as `"anthropic:claude-sonnet-4-5"`, run by Smithers' own **cell-loop agent**: "the model writes JavaScript instead of calling tools… only authority is `ctx.call(flowName, input)`". The README says that "A future agent that drives a foreign CLI is another implementation of `Agent.Service`". — [packages/smithers/agent/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/agent/README.md), [agent/src/AgentAction.ts](https://github.com/smithersai/smithers/blob/main/packages/smithers/agent/src/AgentAction.ts), [agent/harness/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/agent/harness/README.md)
- In 1.0, `claude -p --output-format json --tools "" …` and `codex exec --json --sandbox read-only --ephemeral …` are still spawned, but only by the build-system `Agent.Lint/Diff/Pr` targets: "A session is one bounded conversation with a coding agent CLI — `claude` or `codex`", and the CLI is "spawned with no tools". — [build/build-cli/src/AgentSession.ts](https://github.com/smithersai/smithers/blob/main/packages/smithers/build/build-cli/src/AgentSession.ts)
- `@smthrs/harness-detect` detects 12 coding-agent CLIs: claude, codex, gemini, kimi, opencode, crush, amp, cursor-agent, hermes, pi, and others. — [harness-detect README](https://github.com/smithersai/smithers/blob/main/packages/smithers/agent/harness-detect/README.md)

**Plan-as-contract**
- In 0.x, `create-workflow` is an agent-run builder that "clarifies, scaffolds, and documents" a new TSX workflow from a plain-English ask. `init` installs a `smithers` skill into Claude Code, Pi and other agents so that "your agent runs Smithers on your behalf". `smthrs graph` "Render[s] the workflow graph without executing it". — [smthrs README](https://www.npmjs.com/package/smthrs); `docs/llms-full.txt` CLI catalog (`graph`, `make-workflow`)
- In 1.0, `Control.plan` returns a `PlanCard` containing "the flow, a canonical summary of the input, the envelope, the keyed node graph, and a digest over all of it. The card starts undecided, so the first `run` parks". An approval binds to the digest, so "an approval taken on this envelope cannot authorize a wider one later". Errors include `PlanDigestMismatch`. — [control/docs/quickstart.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/control/docs/quickstart.md), [control/docs/api.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/control/docs/api.md)
- `@smthrs/plan` / `plan-store` provide "The persisted plan: a keyed action graph, its append-only store, its diff". "Growth is append-only and the SQL enforces it… no verb here rewrites or deletes a node, and a trigger refuses one anyway". The store holds "the digest a run's approval binds to". — [flows/plan/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/flows/plan/README.md), [flows/plan-store/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/flows/plan-store/README.md)

**Durability and long waits**
- 0.x: "every completed step is persisted to SQLite the moment it finishes". The contract is "a completed task is never re-executed". Resume validates the workflow source hash plus the VCS revision. `smthrs supervise` auto-resumes runs whose owner died. — `docs/llms-full.txt` §"Durability & resume"
- 0.x: `up` exits when a run reaches `waiting-approval`, `waiting-event` or `waiting-timer`, "rather than burning a process". `<Timer duration="7d">`: "A waiting timer holds no worker and no CPU… only the absolute fire time persists". The Gateway (or `supervise`) wakes it. `<Signal>` and `<WaitForEvent>` give external-event waits. — `docs/llms-full.txt` §"Pauses, resume, and detached runs", §"<Timer>"
- 1.0 design doc "Durable external work: attach, don't restart" (status: proposal 2026-10-01, §3.5 approved 2026-10-02) targets "long external work (10-60 min coding agents in microVMs or Cloud workspaces)". It compares "attach inside one action" with "park on job": Start/Poll/Collect, where "A suspended run holds no lease… restarts resume on the timer, and any host can probe". — [docs/design/durable-external-work.md](https://github.com/smithersai/smithers/blob/main/docs/design/durable-external-work.md)
- HPC/Slurm: a grep of the 1.1 MB 0.x docs for `slurm|hpc|sbatch|pysqa` finds no matches, and nothing was found in the 1.0 docs. Sandboxes cover Docker, Bubblewrap, Microsandbox, Daytona and cloud. — local grep of `docs/llms-full.txt`; [smthrs README](https://www.npmjs.com/package/smthrs)

**Monitoring and control (0.x CLI)**
- Monitoring: `ps`, `inspect RUN_ID`, `logs RUN_ID -f`, `chat RUN_ID`, `why RUN_ID`, and `events RUN_ID` ("Query run event history with filters… and NDJSON output", with category filters agent|approval|frame|node|output|timer|tool-call…). There is a full-screen TUI (Tree/Graph/Logs/Timeline) and a Gateway web UI. Event-log files are NDJSON (`--log`, `--log-dir`). — `docs/llms-full.txt` CLI catalog
- Control: `cancel`, `approve`/`deny`, `signal`, `steer` (a durable steer message delivered to a node's next agent step), and `hijack` (take over the live agent session). `retry-task <workflow> --run-id --node-id` resets a node and by default its dependents; `--no-deps` resets only that node. — `docs/llms-full.txt` CLI catalog (`retry-task`, `steer`, `hijack`)
- Exit code 3 = "`up` ended in waiting-approval, waiting-event, or waiting-timer". In non-TTY mode each command emits a TOON/JSON envelope with "Next steps" CTAs aimed at AI agents. — `docs/llms-full.txt` §"Exit codes", §"Output envelope"

**Dynamic DAG**
- 0.x: "The plan is a **derived value**, recomputed on every state change". Each frame re-renders the JSX from persisted outputs, so `{tickets.map(t => <TicketPipeline …/>)}` grows the graph at runtime (the "Dynamic ticket discovery" recipe). "Every frame commit produces a `GraphSnapshot`." `up --hot` applies source edits "on the next render frame without losing in-flight task state". Schema changes need a fresh run, and resuming a stopped run after a source edit fails with `RESUME_METADATA_MISMATCH` unless `--accept-workflow-change` is passed. — `docs/llms-full.txt` §"Data flow is unidirectional", §"Dynamic ticket discovery", §"Hot reload while authoring", CLI catalog
- No document I found describes approval of graph amendments in 0.x. Graph growth comes from the workflow code, not from a recorded and approved amendment object.

**Audit and replay**
- 0.x: `timeline`, `diff`, `rewind RUN_ID FRAME`, `fork … --frame N --reset-node X`, `replay … --frame N --restore-vcs` and `timetravel`. "A plain `fork` without `--run` does not re-execute anything". A fork "Creates a child run… downstream dependents keep the parent's finished output unless you name them too". Each run keeps an effect journal (intended/succeeded/unknown/reverting/reverted…) with compensation handlers. — `docs/llms-full.txt` §"Time travel", §"Time-travel commands compared"
- 1.0: `@smthrs/journal` is "An append-only event history… Rows are appended and never updated", in SQLite. `@smthrs/time-travel` offers `replay`/`inspect`/`fork`/`rewind` "at a _frame_… none of them re-executes anything". "A durable fork reuses completed action results through its frame". — [flows/journal/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/flows/journal/README.md), [flows/time-travel/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/flows/time-travel/README.md)

**Interface for an outer agent**
- 0.x: `bunx smthrs --mcp` starts an MCP server whose "semantic surface" tools include `run_workflow` (background by default), `list_runs`, `get_run`, `watch_run`, `resolve_approval`, `revert_attempt`, `rewind_run`, `time_travel` and others. `bunx smthrs mcp add` registers it with the detected agents. `init` installs the skill. — `docs/llms-full.txt` §MCP; [smthrs README](https://www.npmjs.com/package/smthrs); npm `@smthrs/cli` description "Smithers command-line interface, MCP server, and local workflow tools" ([npm](https://registry.npmjs.org/@smthrs/cli))
- 1.0: `@smthrs/control` is the `Control` service (`plan, run, approve, deny, steer, signal, cancel, resume, list, watch`), described as the base "to build a host of your own, such as a gateway, an MCP server". `@smthrs/mcp` in 1.0 is an MCP client that exposes MCP tools *to* Smithers agents. — [control/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/control/README.md), [mcp/README.md](https://github.com/smithersai/smithers/blob/main/packages/smithers/mcp/README.md)

### Inferences
- Measured against the Idea, Smithers 0.x already implements roughly 70–80%:
  - agent nodes backed by headless harnesses
  - durable SQLite state
  - waits that do not hold a process
  - approvals
  - per-node retry with downstream reset
  - an NDJSON event log
  - time travel that reuses persisted outputs
  - a dynamic graph
  - CLI + MCP + skill for an outer agent.
- What the Idea would add beyond Smithers:
  - (a) plan-level approval in 0.x (1.0 has it, digest-bound)
  - (b) explicit, recorded and approved graph amendments: Smithers grows the graph implicitly through re-render, and hot reload is an authoring convenience
  - (c) an Slurm/HPC external-job node with "park on job" semantics: Smithers 1.0 has only just designed this, aimed at cloud coding agents
  - (d) a human-readable plan file plus a file-based event log; Smithers keeps state in SQLite tables and exports NDJSON
  - (e) a research or science focus, a "lightweight" footprint, and "every node is an agent" as a principle.
- Weight vs "lightweight": 0.x needs Bun, React, TSX and jj/git, with about 40 `@smthrs/*` dependencies. 1.0 needs Node 26.4, Effect 4 rc, a Rust FFI build, and is not on npm.
- Strategic risk if building on or competing with Smithers: the project is mid-rewrite with no state compatibility, has a single maintainer, and is pivoting to a hosted product.

### Gaps
- I did not confirm how the 0.x `ClaudeCodeAgent` builds its argv (that is, whether it uses `claude -p --output-format stream-json`), because the 0.x source is not in the shallow clone. The docs only say it spawns the vendor binary.
- I could not confirm whether the hosted docs at smithers.sh now describe 1.0 or 0.x. I relied on the 0.35.0 tarball docs and on `main`.
- Whether 1.0 records LLM responses for deterministic replay of a *partial* agent action, beyond reusing completed action results, is unverified.

---

## 2. Archon (coleam00/Archon): how much of the Idea does it already implement?

### Takeaway
Archon is a mature, popular YAML-DAG engine for coding agents:
- 23.6k stars, MIT, v0.11.1 on 2026-09-25
- nodes run through in-process provider SDKs (Claude Agent SDK, Codex SDK, Pi, OpenCode, Copilot)
- deterministic `bash`/`script` nodes, `approval` gates and durable `wait:` nodes with external-event signals
- resume that skips completed nodes, cancel/abandon, `fan_out` runtime sub-runs, SQLite/Postgres state with a `workflow_events` table plus a per-run JSONL transcript
- a CLI with a bundled skill for Claude Code and Codex.

It deliberately freezes the workflow source per run ("A run does not change shape while it is running"). It has no recorded-output replay or time travel, no per-node "rerun this node and downstream" verb, no plan-approval contract, and no HPC support.

### Cited Findings
- Repo: 23,600 stars, 3,495 forks, MIT, created 2025-02-07, last push 2026-10-03, 325 open issues. Description: "The first open-source harness builder for AI coding. Make AI coding deterministic and repeatable." About 3.3k commits; top contributors Wirasm (2,600) and coleam00 (427). Releases: v0.11.1 (2026-09-25), v0.11.0 (2026-09-25), v0.10.1 (2026-08-30). — [GitHub API](https://api.github.com/repos/coleam00/Archon), [releases](https://api.github.com/repos/coleam00/Archon/releases), [contributors](https://api.github.com/repos/coleam00/Archon/contributors)
- Current Archon is the "0.x (2025–present)" ground-up rebuild ("Governed agentic automation engine"). v1–v6 were an unrelated RAG agent builder, archived on branch `archive/v1-task-management-rag`. — [what-archon-is-not.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/getting-started/what-archon-is-not.md), [README](https://github.com/coleam00/Archon/blob/main/README.md)

**Node model**
- Node types (exactly one per node): `command`, `prompt`, `bash` ("Shell script (no AI)"), `script` (bun/uv), `loop`, `loop_group`, `approval`, `wait`, `cancel`, `include`, `workflow` (a child sub-run). Per-node `provider:` and `model:`. — [authoring-workflows.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/guides/authoring-workflows.md)
- Providers: `claude`, `codex`, and community providers `opencode`, `pi`, `copilot`. The matrix covers session resume, immutable session fork, MCP, skills, structured output (enforced on claude/codex/opencode) and spend limits (claude only). — [provider-capabilities.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/provider-capabilities.md)
- Harness integration is through in-process SDKs, not CLI print mode. The dependencies are `@anthropic-ai/claude-agent-sdk` ^0.3.287, `@openai/codex-sdk` ^0.160.0, `@earendil-works/pi-coding-agent` ^1.0.0 and `@opencode-ai/sdk`. The changelog says "Provider SDKs updated to latest: Claude Agent SDK 0.3.287, Codex SDK 0.160.0, Pi 1.0.0". Compiled binaries need `CLAUDE_BIN_PATH`. — `packages/providers/package.json`; [CHANGELOG.md](https://github.com/coleam00/Archon/blob/main/CHANGELOG.md); [README](https://github.com/coleam00/Archon/blob/main/README.md)
- The design intent is a mix: "Mix deterministic nodes (bash scripts, tests, git ops) with AI nodes… The AI only runs where it adds value." — [README](https://github.com/coleam00/Archon/blob/main/README.md)

**Plan-as-contract**
- Workflows are YAML in `.archon/workflows/`. A bundled `archon-workflow-builder` workflow generates new workflow YAML. `validate workflows` exists, and `--dry-run` runs a side-effect-free simulation that emits one trace document. — [README](https://github.com/coleam00/Archon/blob/main/README.md); [cli.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/cli.md)
- The source is frozen per run: captured at `~/.archon/workspaces/<project>/workflow-source/runs/<run-id>/`, with a manifest digest checked on resume. "A run does not change shape while it is running… Start a new run to pick up the edits." — [cli.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/cli.md)
- I found no step for approving the whole plan before execution. Approval is a node type placed inside the DAG.

**Durability and long waits**
- `wait:` node: "records its condition in the workflow run, changes the run to `paused`, and returns the worker slot". It supports `duration_ms`, `until`, `event` (with a required `deadline_ms`) and `attention`. "Restarting Archon preserves either kind." Signals go through `POST /api/workflows/runs/<id>/signal`. A foreground or `--detach` process "stay[s] alive through the wait", and `archon serve`'s continuation scan resumes due waits whose owner is gone. Waits are not supported in container-isolated runs. — [authoring-workflows.md §Durable waits](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/guides/authoring-workflows.md)
- Resume is explicit: "Completed nodes are skipped; only failed and not-yet-run nodes are executed". `always_run: true` opts a node out of resume caching. Archon "does not auto-fail `running` rows on server startup". — same doc §"DAG Resume on Failure"
- No HPC, Slurm or cluster integration appears in the docs. A grep of the docs for durable-wait and poll patterns found only the `wait:` node and OAuth polling.

**Monitoring and control**
- CLI: `workflow status`, `runs`, `get`, `logs <run-id> [--follow]`, `wait <run-id>`, `resume`, `cancel` (terminates the owner's process tree), `abandon`, `approve [--comment]`, `reject [--reason]` (with `on_reject` rework), `cleanup`, `reset-sessions`, and `event emit`. Most verbs accept `--json` and `--detach`. — [cli.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/cli.md)
- Web UI: run list, run detail with event log, artifacts and workflow graph. Chat adapters for Slack, Telegram, Discord and GitHub. — [README](https://github.com/coleam00/Archon/blob/main/README.md)
- I found no documented CLI verb to rerun a single node and its downstream on a completed run. The mechanisms are resume, which re-runs failed or unrun nodes, and `always_run`.

**Dynamic DAG**
- `fan_out:` turns a `workflow:` node into N child runs over "a list produced at run time". `include:` is expanded at load time. The node set is otherwise fixed when the run starts.

**Audit and replay**
- The `remote_agent_workflow_events` table is a "Step-level workflow event log" in SQLite or Postgres, with a pg_notify trigger. Each run also writes a structured JSONL transcript (`transcriptPath`). `bash` node stdout is stored in `node_completed.data.node_output` (capped at 32 KiB). — [database.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/database.md); [cli.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/cli.md)
- Resume feeds the cached outputs of completed nodes downstream ("resume skips any node that completed successfully in the prior run and feeds its cached output to downstream consumers"). There is no rewind or fork-from-frame. — [authoring-workflows.md](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/guides/authoring-workflows.md)

**Interface for an outer agent**
- `archon skill install` installs the `archon-cli` skill into `.claude/skills/` and `.agents/skills/` (Codex). Users say "Use archon to fix issue #42". The chat agent has a `manage_run` tool, and an HTTP API exists. I found no MCP server that exposes Archon itself; `mcp:` configures MCP servers for nodes. — [cli.md §skill install](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/cli.md)

### Inferences
- Archon covers:
  - DAG + per-node agent/provider
  - deterministic nodes
  - approval gates
  - durable waits, which are close to what Slurm needs: a `wait: event` plus a signal from a job epilogue would work, though an owner process stays alive in CLI mode
  - resume with cached outputs
  - cancel
  - a skill for an outer agent.
- It misses the Idea's:
  - plan-approval contract
  - dynamic or amended graphs (by design it refuses mid-run shape changes)
  - replay or time travel
  - per-node rerun + downstream
  - a plan-file-plus-append-only-log storage model (it uses DB tables)
  - HPC support.
- Archon also uses SDK integration rather than headless CLI print mode.

### Gaps
- I did not verify whether the web UI offers "retry node" for completed runs. Only the CLI docs were checked.

---

## 3. orc (tjdals12/orc) and yak (lchase/yak): how much of the Idea do they already implement?

### Takeaway
Both are tiny, single-author, MIT, TypeScript YAML-DAG engines with 0 stars, created August 2026.

orc:
- spawns `claude -p` / `codex exec` / `grok -p` per agent node
- has `bash` and `approval` nodes, SQLite state with a frozen spec copy per run, and stop/cancel/resume
- ships a skill so an outer agent can write and run workflows.

yak:
- is architecturally closest to the Idea's storage model: an append-only `journal.jsonl` is "the only durable state — no database", and the frozen graph IR is saved as `workflow.json`
- has a file-based gate protocol, content-addressed caching, `replay --from <step>`, cancel and watch
- has only a Claude Agent SDK adapter, plus a mock adapter.

Neither offers dynamic graphs, long external waits/HPC, or MCP.

### Cited Findings

**orc**
- `tjdals12/orc`: 0 stars, MIT, created 2026-08-23, pushed 2026-10-02. Releases v1.3.1 (2026-10-02) and v1.3.0 (2026-09-18). npm `@tjdals12/orc`. Commits come from Seongmin (Lee) plus semantic-release-bot. — [GitHub API](https://api.github.com/repos/tjdals12/orc), [repo](https://github.com/tjdals12/orc)
- "every step picks its own agent and model… Each step starts in a fresh session… every run gets its own isolated git worktree". Providers are Claude Code (`claude` >=2.1.261), Codex (`codex` >=0.153.4) and Grok Build (`grok` >=1.0.13). Node `type: agent | bash | approval`. Artifacts (`produces`/`consumes`, `$ARTIFACT(x)`) are "the only way context travels from one node to the next". — [README.md](https://github.com/tjdals12/orc/blob/main/README.md)
- The runners build argv with `'-p'` for claude, `'exec'` for codex and `-p` for grok. — `src/workflow-run/agent-node/{claude,codex,grok}/runner.ts` in [repo](https://github.com/tjdals12/orc)
- State: `~/.orc/orc.db` ("projects, workflow run state, events and logs"). Each run has `spec/` with "Copies of the workflow file… A run — and a resume — works from these copies". — [README.md](https://github.com/tjdals12/orc/blob/main/README.md)
- Commands: `workflow runs|status|events|logs|stream [-f]|approvals|approve|reject [--reason]|stop|cancel|resume`, `--detach`. `on_reject` runs rework and re-asks. Approve "records the decision only — continue the run with resume". `resume` restarts "from unfinished nodes". The v1.3.0 changelog adds "keep runs alive after terminal exit". — [README.md](https://github.com/tjdals12/orc/blob/main/README.md), [CHANGELOG.md](https://github.com/tjdals12/orc/blob/main/CHANGELOG.md)
- Outer agent: `orc project add` installs a skill so "you can describe the workflow you want and have the agent write it for you", and the agent "picks the workflow that fits, starts it, watches until it finishes". — [README.md](https://github.com/tjdals12/orc/blob/main/README.md)

**yak**
- `lchase/yak`: 0 stars, MIT, created 2026-08-09, last push and release `yak-v0.5.0` on 2026-09-08 (with v0.4.1 and v0.4.0 on 2026-09-08). 78 commits by Lawrence Chase. npm `@lchase/yak`. — [GitHub API](https://api.github.com/repos/lchase/yak), [repo](https://github.com/lchase/yak)
- Thesis: "An agentic workflow engine is a **build system whose compilers are nondeterministic**." Its principles include "The graph is data" and "Everything on disk, everything greppable. No hidden state in a database". Its non-goals include distributed execution, a server and cron: "If you need any of these on day one, use Archon or Temporal instead". — [spec.md](https://github.com/lchase/yak/blob/main/spec.md)
- Step kinds: `command`, `transform`, `agent` (with Zod schema and a repair loop), `loop`, `map` (with worktree isolation per item), and `gate`. Adapters: `mock` and `claude-code` ("wraps `@anthropic-ai/claude-agent-sdk`"). — [README.md](https://github.com/lchase/yak/blob/main/README.md)
- Run directory: `workflow.json` ("frozen IR of the graph as executed"), `journal.jsonl` ("append-only event log — source of truth"), `artifacts/`, `pending/<step>.request.json|answer.json`, and `sessions/<step>.jsonl` (raw transcript, "for debugging only"). Events: `run.started`, `step.started` (with semanticKey/definitionKey), `step.completed` (with artifactHash, cached, stale), `step.failed`, `artifact.written`, `budget.consumed`, `gate.opened`, `gate.answered`, `run.suspended`, `run.finished`. — [spec.md §4, §13](https://github.com/lchase/yak/blob/main/spec.md)
- Resume replays the journal and reuses artifacts whose cache keys match; a mismatch re-runs the step "and everything downstream". `yak run --from <step-id>` / `yak replay <run-id> --from <step-id>` force re-execution from a point. A gate writes a request file, journals `run.suspended` and exits 78. Any frontend can write the answer file. Other commands: `yak cancel`, `yak watch`, `yak pending`, `yak status`, `yak graph` (Mermaid). — [spec.md](https://github.com/lchase/yak/blob/main/spec.md), [README.md](https://github.com/lchase/yak/blob/main/README.md)
- "Cycles in the static graph are rejected at load time… The graph is acyclic by construction". — [spec.md §4.3](https://github.com/lchase/yak/blob/main/spec.md)

### Inferences
- orc matches the Idea's "headless CLI per node + approval + resume + skill" exactly, but it is minimal: no dynamic graph, no waits, no replay, no MCP.
- yak matches the Idea's "plan file + append-only journal, replay reuses recorded outputs, file-based gates" storage philosophy almost exactly. It lacks multi-harness support, dynamic graph amendments, long external waits and an MCP/skill interface.
- Both are effectively proofs-of-concept with no adoption. They show the design space is crowded at the small end but leave the niche open. The open niche is the research/HPC durability, dynamic-amendment and multi-harness combination.

### Gaps
- I did not read orc's per-node rerun semantics in source. The README documents only run-level resume.
- I did not verify whether the yak journal stores agent outputs inline or only as artifact hashes. The spec says artifacts are on disk and referenced by hash.

---

## 4. GitHub Agentic Workflows (`gh aw`, now github/gh-aw): how much of the Idea does it already implement?

### Takeaway
gh-aw runs one coding agent (Copilot CLI by default; Claude Code, Codex, Gemini or Pi optional) per Markdown workflow inside a GitHub Actions job, with sandboxing and "safe outputs". Durability, logs and reruns are inherited from Actions. It has an MCP server (`gh aw mcp-server`) and agent-readable authoring guides. It is not a per-node-agent DAG engine:
- multi-step work is done through orchestrator/worker workflow dispatch
- agent jobs are minutes-bounded (agent job default 60 min; GitHub-hosted Actions jobs are capped at 360 min)
- it is tied to GitHub, with no HPC support.

### Cited Findings
- `githubnext/gh-aw` now redirects ("Moved Permanently") to `github/gh-aw`: 5,337 stars, 572 forks, MIT, created 2025-08-12, pushed 2026-10-03, 515 open issues, about 18.1k commits. Top contributors are Copilot (12,729) and github-actions[bot] (2,586), then dsyme (1,131) and pelikhan (809). Releases v0.90.3 (2026-10-03), v0.90.1 and v0.90.0 are all marked prerelease. — [GitHub API](https://api.github.com/repos/github/gh-aw), [releases](https://api.github.com/repos/github/gh-aw/releases), [contributors](https://api.github.com/repos/github/gh-aw/contributors)
- "define AI-powered repository automation in Markdown with YAML frontmatter… The `gh-aw` GitHub CLI extension compiles each agentic workflow into a standard GitHub Actions workflow" (`.lock.yml`). "Agent jobs are read-only and sandboxed by default, and configured GitHub writes are normally applied through validated `safe-outputs` jobs". — [README.md](https://github.com/github/gh-aw/blob/main/README.md)
- Engines: `copilot` (default), `claude`, `codex`, `gemini`, `pi`. Copilot SDK mode is optional. A custom engine command and harness script are supported. — [reference/engines.md](https://github.com/github/gh-aw/blob/main/docs/src/content/docs/reference/engines.md)
- Built-in jobs per workflow: `pre_activation`, `activation`, `agent`, `safe_outputs`, `conclusion`, `detection`, `unlock`. Custom deterministic `jobs:` are allowed. Timeouts: `agent` job 60 min, `agentic_execution` step 20 min, `detection` 10 min. The GitHub Actions default job `timeout-minutes` is 360. — [reference/steps-jobs.md](https://github.com/github/gh-aw/blob/main/docs/src/content/docs/reference/steps-jobs.md)
- Multi-agent pattern "OrchestratorOps": an orchestrator workflow uses `dispatch-workflow` (async, can "outlive the parent run") or `call-workflow` (compile-time fan-out) safe outputs to start worker workflows. — [patterns/orchestrator-ops.md](https://github.com/github/gh-aw/blob/main/docs/src/content/docs/patterns/orchestrator-ops.md)
- Outer-agent interface: `gh aw mcp-server` "exposes GitHub Agentic Workflows CLI commands as MCP tools", including `logs`, `audit` and `audit-diff`. The README has an "Agent quick links" block (create.md, install.md) for agents. — [reference/gh-aw-as-mcp-server.md](https://github.com/github/gh-aw/blob/main/docs/src/content/docs/reference/gh-aw-as-mcp-server.md), [README.md](https://github.com/github/gh-aw/blob/main/README.md)
- Audit: `gh aw audit <run-id>` produces a Markdown or JSON report, diffs runs, and parses agent and firewall logs. — [reference/audit.md](https://github.com/github/gh-aw/blob/main/docs/src/content/docs/reference/audit.md)

### Inferences
- gh-aw covers the Idea's harness-agnostic "agent does the work", the authoring-by-agent loop, MCP and audit.
- It misses:
  - per-node agents in a DAG (one agent job per workflow)
  - plan approval beyond code review of the `.md`/`.lock.yml` in a PR
  - long waits on HPC
  - dynamic graph amendments
  - replay of recorded agent outputs.
- It is adjacent prior art (an "agent in CI") rather than a direct competitor.

### Gaps
- I did not check whether gh-aw supports Actions `environment:` protection as a human approval gate for agent jobs in detail. The docs have "Environment Protection (`environment:`)" sections in frontmatter.md and safe-outputs.md, which suggests it does.

---

## 5. CatGo (Hello-QM/catgo-LRG): are nodes agents or fixed steps, and how do HPC, state, provenance and MCP work?

### Takeaway
CatGo's workflow nodes are **fixed scientific task types**: `geo_opt`, `freq`, `single_point`, `md`, `ts_search`, `gibbs_energy`, `slab_gen`, and so on. They are **not agents**. LLMs (CatBot via Claude Code, Codex or Gemini) sit *outside* the DAG: they author and modify workflows through MCP tools, and they diagnose failed HPC tasks.

Its HPC durability is strong and directly relevant to the Idea:
- a stateless periodic scanner over SQLite (`~/.catgo/catgo.db`) with a 14+ state task machine that includes SUBMITTED, QUEUED, RUNNING and COMPLETED_REMOTE
- SSH plus SLURM/PBS/LSF/SGE submit and polling, so a job waits without holding a process
- a PENDING_REVIEW gate before HPC spend
- retry that resets a task and all downstream tasks, and pause/resume/reset
- lightweight hash-based provenance.

### Cited Findings
- Upstream repo `Hello-QM/catgo-LRG`: 205 stars, 26 forks, AGPL-3.0, created 2026-04-29, pushed 2026-09-22. Releases v1.4.14 (2026-08-25), v1.4.9 and v1.4.8. **The local clone is older**: its HEAD is `c64d0e7` (2026-05-17), and its CHANGELOG stops at 1.0.0 (2026-05-12). — [GitHub API](https://api.github.com/repos/Hello-QM/catgo-LRG), [releases](https://api.github.com/repos/Hello-QM/catgo-LRG/releases); local `/homes/nessa/zhanghao/dev/Eleforge/context/compute_workflow/catgo-LRG/CHANGELOG.md`
- README: CatGo "combines an interactive 3D structure viewer, a natural-language AI assistant (**CatBot**), a visual DAG **workflow engine**, and **HPC integration**". The workflow is a "DAG editor for chained calculations (opt → SP → DOS / NEB / MD / slow-growth …)". HPC offers "SLURM / PBS / LSF / SGE adapters… queue-state polling, log tail… AI diagnosis on FAILED / REMOTE_ERROR tasks". The MCP tools are `catgo_structure`, `catgo_workflow`, `catgo_quickbuild`, `catgo_workflow_engine`, `catgo_diagnose` and others. — local `catgo-LRG/readme.md`; [GitHub readme](https://github.com/Hello-QM/catgo-LRG/blob/main/readme.md)
- Task types: HPC calculations (`geo_opt`, `single_point`, `freq`, `cell_opt`, `md`, `ts_search`), local analysis (`gibbs_energy`, `free_energy_diagram`, `dos_analysis`, `charge_analysis`) and structure building. The Python API is `wf.add_task(geo_opt, …)` followed by `wf.submit()`. "HPC tasks pause at `PENDING_REVIEW`… users can verify structures and parameters before spending HPC resources". Agents "MUST ask the user which HPC cluster to use and confirm job parameters". — local `catgo-LRG/server/catgo/workflow/SKILL.md`
- The task state machine is `WAITING, READY, GENERATING, UPLOADING, SUBMITTED, QUEUED, RUNNING, COMPLETED_REMOTE, COLLECTING, COMPLETED, FAILED, REMOTE_ERROR, PENDING_REVIEW, PAUSED, CANCELLED, SKIPPED, MAPPED` ("Template/controller — children were spawned"). — local `catgo-LRG/server/catgo/workflow/states.py`
- The scanner docstring: "WorkflowEngine — stateless periodic scanner. Each scan_cycle() reads DB, advances task states, and returns. No in-memory state between cycles. Crash and restart safely." The poller polls SUBMITTED/QUEUED/RUNNING jobs over asyncssh and maps the Slurm statuses COMPLETED/FAILED/TIMEOUT/OOM. — local `server/catgo/workflow/engine/scanner.py`, `engine/poller.py`
- SQLite at `~/.catgo/catgo.db` (`WorkflowDB`). — local `server/catgo/workflow/db.py`
- `retry_task`: "Reset a task and all downstream dependents to WAITING". MCP `catgo_workflow_engine` actions: `create, add_task, submit, status, list, get_dag, get_result, modify_params, retry, pause, resume, reset`. — local `server/catgo/workflow/service.py`, `server/catgo/workflow/mcp_tools.py`
- `add_task` adds "a task to an existing workflow", so the graph can be extended through MCP. I saw no approval or audit record for such amendments. — local `server/catgo/workflow/service.py`
- Provenance: "Lightweight provenance tracking — hash-based lineage for task results" (SHA-256 of output values). — local `server/catgo/workflow/provenance.py`
- AI diagnosis: "AI-assisted error diagnosis for failed HPC tasks… Automatically processed by the error handler (if confidence is high) [or] Returned via MCP tool for manual AI agent review". — local `server/catgo/workflow/engine/ai_diagnosis.py`
- MCP servers: `server/catgo/mcp_tools/server.py` (with tools such as `catgo_create_tool`, which creates and tests a new Python tool in a sandboxed subprocess, plus atomate2/quacc template import) and `server_claude_code.py` ("catgo-claude-code", configured in `~/.claude/mcp.json`). HTTP/SSE MCP routers also exist. — local `server/catgo/mcp_tools/`, `server/catgo/routers/mcp_http.py`, `mcp_sse.py`
- Architecture doc (dated 2026-03-13): a Rust kernel (`crates/catgo-graph`, SQLite-backed) plus a Python HPC shim, with routing "decided before execution starts". It may be stale relative to the code. — local `catgo-LRG/docs/catgo_unified_workflow_architecture.md`
- Paper: a search found the Digital Discovery article page `pubs.rsc.org/en/content/articlelanding/2026/dd/d5dd00524h`, but the page returned 403. I could not confirm that this DOI is the CatGo paper. The repo cites Zenodo DOI 10.5281/zenodo.19709425 and a `citation.cff` (author Wanlu Li, UCSD). — [RSC search hit](https://pubs.rsc.org/en/content/articlelanding/2026/dd/d5dd00524h); local `catgo-LRG/citation.cff`

### Inferences
- CatGo is the closest prior art for the Idea's **HPC durability**: DB-backed job states, polling without a held process, a human gate before HPC spend, and retry with downstream reset. It is also prior art for an outer agent driving a workflow engine through MCP.
- It is the *opposite* of "every node is an agent". Nodes are a fixed scientific catalog (VASP/CP2K/ORCA/LAMMPS/MLP), with agents only at authoring and diagnosis time. It has no plan-approval contract other than the per-HPC-task PENDING_REVIEW, no append-only event log or replay, and no recorded agent decisions.
- AGPL-3.0 matters if code is reused.

### Gaps
- Upstream changes after 2026-05-17 (v1.4.x) were not inspected. Newer versions may have added agent nodes or event logs. The local clone is about 4 months old relative to upstream.
- The DOI of the Digital Discovery 2026 paper is unconfirmed.

---

## 6. DREAMS (arXiv 2507.14267): is there a code release, and how do its per-role agents and provenance work?

### Takeaway
DREAMS is a hierarchical LangGraph multi-agent system:
- a planning supervisor plus DFT, HPC and convergence worker agents, and (in v2) report-judge or safety-guard agents
- it runs Quantum ESPRESSO via pysqa on Slurm.

Code is public at `BattModels/material_agent` (29 stars, no license detected). In the public code, the HPC tool **blocks in a polling loop** while jobs run, and LangGraph state uses an in-memory `MemorySaver`, so there is no durability across process death. The v2 paper (2026-08-11) adds an append-only provenance registry and a provenance DAG, but I could not find that code in the public `main` branch. It is an autonomous agent system, not a user-approved DAG.

### Cited Findings
- arXiv 2507.14267: v1 2025-07-18, v2 2026-08-11. Authors Ziqi Wang, Hongshuo Huang, Hancheng Zhao, Changwen Xu, Shang Zhu, Jan Janssen, Venkatasubramanian Viswanathan. — [arXiv abs](https://arxiv.org/abs/2507.14267)
- v1: "implemented as a hierarchical multi-agent system using the Claude 3.7 Sonnet… facilitated by the LangGraph framework". "For calculation submission, the LLM agent employs the Python Simple Queuing System Adapter (pysqa)… with SLURM". "Data and code is available on: GitHub (github.com/BattModels/material_agent)". — [arXiv v1 HTML](https://arxiv.org/html/2507.14267v1)
- v2 roles: a planning supervisor that "generates and updates multi-step plans and selects the agent responsible for each task", a DFT agent, an HPC agent ("resource selection, job submission, job monitoring, and file retrieval"), a convergence agent, and a Report Judge. "The supervisor dispatches one worker step at a time in a synchronous, sequential control loop." The workers use Claude Sonnet 4.5 and the safety guard uses Claude Opus 4.8, as stated in the fetched text. — [arXiv v2 HTML](https://arxiv.org/html/2507.14267v2)
- v2 provenance: "Because each tool call identifies its input sources, the framework constructs a directed acyclic provenance graph during execution". Tool outputs are "stored in an append-only provenance registry under an immutable result identifier" with "parameter-specific justifications, parameter-specific sources". There is a shared "canvas" memory. Users "can inspect the canvas at any time", and there is no mandated plan sign-off. — [arXiv v2 HTML](https://arxiv.org/html/2507.14267v2)
- Repo `BattModels/material_agent`: 29 stars, 6 forks, no license, created 2024-08-28, pushed 2026-08-31. `main`'s last commit is 2026-01-26 ("Add executorlib and h5py to environment.yml"). There are many feature branches (DFTMD, multiOER, multiSoftware, oer_boss_log, …). — [GitHub API](https://api.github.com/repos/BattModels/material_agent), [repo](https://github.com/BattModels/material_agent)
- Code: `src/tools.py` imports `from pysqa import QueueAdapter`. `submit_and_monitor_job` and `submit_single_job` call `qa.submit_job(...)`, then loop on `qa.get_status_of_job(...)` until the jobs finish, so the agent's tool call blocks. `src/graph.py` and `src/planNexeHighPlan.py` use `langgraph`'s `StateGraph` and `MemorySaver` (in-memory checkpointer). `src/myCANVAS.py` implements the shared canvas (read/write keys). A grep of `src/` found no `provenance` code. — [src/tools.py](https://github.com/BattModels/material_agent/blob/main/src/tools.py), [src/graph.py](https://github.com/BattModels/material_agent/blob/main/src/graph.py), [src/myCANVAS.py](https://github.com/BattModels/material_agent/blob/main/src/myCANVAS.py)

### Inferences
- DREAMS shows that scientific value can come from role agents plus Slurm, and from parameter-level provenance with justifications. That last point is a strong precedent for the Idea's "agent decisions recorded".
- DREAMS covers none of the Idea's workflow-runtime properties: no durable state, waits that hold the process, no user-approved plan, no cancel/resume/rerun per node, and no outer-harness CLI or MCP.

### Gaps
- Whether the v2 provenance-registry and safety-guard code is public (possibly on an unexamined branch) is unknown.
- The "Claude Opus 4.8" model string comes from the fetched v2 text and was not independently verified.

---

## 7. Cross-project verdict: which parts of the Idea are already covered, and which are novel?

### Takeaway
No single project combines all of the following:
- every node is a headless coding-agent harness
- a user- and planner-approved plan file as a contract
- durable multi-day waits on Slurm without a held process
- dynamic graph amendments that are recorded and approved
- a file-based plan plus an append-only log, with replay that reuses recorded agent outputs
- a CLI/MCP/skill surface for an outer agent.

Smithers 0.x and 1.0 together cover the most. Archon covers the most at production scale. yak matches the storage philosophy, CatGo the HPC durability, and DREAMS the scientific provenance of agent decisions. The defensible novelty is in combining:
1. HPC-aware "park on external job" agent nodes
2. explicit, recorded and approved DAG amendments as first-class plan diffs
3. a research-oriented, lightweight, file-first contract.

The building blocks individually are not novel.

### Cited Findings (summary matrix; sources as cited in sections 1–6)

| Capability in the Idea | Smithers 0.x / 1.0 | Archon | orc | yak | gh-aw | CatGo | DREAMS |
|---|---|---|---|---|---|---|---|
| Node = headless agent harness | Yes (CLI child process; 0.x); 1.0 core agent in-process | SDKs in-process (Claude Agent SDK, Codex SDK, Pi) | Yes (`claude -p`, `codex exec`, `grok -p`) | Claude Agent SDK only | One agent job per workflow (Copilot/Claude/Codex/Gemini/Pi) | No (fixed sci nodes) | LangGraph role agents (API) |
| Every node must be agent | No (compute/static allowed) | No (bash/script) | No (bash) | No | No | No | n/a |
| Plan drafted by agent, approved before run | Agent-authored (create-workflow); digest-bound plan approval in 1.0 | Agent builder workflow; no plan approval | Skill writes YAML; no plan approval | No | PR review of .md/.lock | Per-HPC-task PENDING_REVIEW | No (autonomous) |
| Durable across restart | Yes (SQLite) | Yes (SQLite/Postgres) | Yes (SQLite) | Yes (journal) | Via Actions | Yes (stateless scanner + SQLite) | No (MemorySaver) |
| Wait hours–days without holding process | Yes (timer/event/approval waits) | `wait:` nodes (owner may stay alive in CLI mode; server rescans) | No | Gates only | No (job timeouts) | Yes (Slurm polling) | No (blocking poll) |
| HPC/Slurm | No | No | No | No | No | Yes | Yes (pysqa) |
| Cancel / resume / rerun node + downstream | Yes (`retry-task` default resets dependents) | Cancel/resume; no per-node rerun verb | Stop/cancel/resume | Cancel/resume/`--from` | Actions rerun | Yes (`retry` resets downstream) | No |
| Dynamic DAG during run | Yes (re-render per frame; hot reload) | `fan_out` only; shape frozen | No | No | Dispatch workers | `add_task` via MCP | Planner re-plans (not a DAG contract) |
| Amendments recorded/approved | Frame GraphSnapshots recorded; not approved | n/a | n/a | n/a | n/a | No | Plan updates in canvas |
| Append-only event log | SQLite events + NDJSON export; 1.0 append-only journal | DB events table + JSONL transcript | DB events | `journal.jsonl` (sole state) | Actions logs + `gh aw audit` | No | v2 paper: provenance registry |
| Replay reuses recorded outputs | Yes (fork/replay/rewind) | Resume reuses cached outputs | Resume | Yes (content-addressed) | No | No | No |
| Outer-agent interface | CLI + MCP + skill | CLI + skill (+HTTP) | CLI + skill | CLI | CLI + MCP | MCP | None |
| Maturity (2026-10-02) | 428★, MIT, v0.35.0 2026-08-17; 1.0-rc on main; ~1 maintainer | 23.6k★, MIT, v0.11.1 2026-09-25; active team | 0★, MIT, v1.3.1 2026-10-02 | 0★, MIT, v0.5.0 2026-09-08 | 5.3k★, MIT, v0.90.3 (pre) 2026-10-03 | 205★, AGPL-3.0, v1.4.14 2026-08-25 | 29★, no license |

### Inferences
- The idea of "a durable agent-workflow CLI driven by an outer coding agent via skill/MCP" is **already well covered**: Smithers 0.x, Archon, orc and gh-aw all ship an agent-facing CLI and skill or MCP. It should not be pitched as novel.
- Each of the following is covered somewhere: "replay reuses recorded agent outputs" (Smithers, yak), "approval gates" (all), and "every node runs a harness" (Smithers, orc).
- What remains uncovered in combination:
  - (1) Slurm/HPC-aware agent nodes that submit a job, durably park for hours to days, and are re-woken by a poller or signal. Only CatGo and DREAMS touch HPC, and neither has agent nodes with durable parking. Smithers 1.0's "park on job" design is the closest conceptual match but targets cloud sandboxes.
  - (2) First-class **amendment records**: a DAG diff proposed by an agent mid-run, approved by the user, and appended to the plan log. Smithers 1.0's append-only plan-store with digest-bound approval is the nearest primitive.
  - (3) A research-science framing with per-decision provenance, as in DREAMS v2's parameter justifications, joined to a durable runtime.
- Recommended positioning: build on or interoperate with the Smithers 1.0 Control/plan-store concepts, or with Archon's `wait:` signal API, rather than competing head-on on generic coding workflows. Differentiate on HPC-parked agent nodes, approved amendments and a scientific audit trail.

### Gaps
- Star counts and activity are point-in-time API reads, 2026-10-02/03 UTC.
- I did not exhaustively survey other near neighbors surfaced by search: Rayspec, caw (aigengame/cli-agentic-workflow), Orchestra, flowai-workflow, workhorse-agent, Yield (operatorstack/yield), TanStack Workflow. Yield in particular advertises "deterministic re-execution… feeding recorded responses back in order" with an append-only `.yield/runs/<id>.jsonl`, which is relevant to the replay requirement. — [Yield on pkg.go.dev](https://pkg.go.dev/github.com/operatorstack/yield@v0.1.29), [caw](https://awesome.ecosyste.ms/projects/github.com%2Faigengame%2Fcli-agentic-workflow), [Rayspec](https://pypi.org/project/rayspec/)
