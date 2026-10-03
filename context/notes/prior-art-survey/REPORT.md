# Existing tools cover the parts, not the HPC contract

Yes, close prior art exists, and the closest project is **Smithers** (smithersai/smithers, MIT). Its 0.x line already runs Claude Code, Codex, Pi and other CLI agents as durable DAG tasks, with SQLite state, waits that hold no process, approvals, per-node retry with downstream reset, an NDJSON event log, rewind/fork/replay that reuses persisted outputs, and a CLI + MCP server + installed skill for an outer agent. Its 1.0 release candidate adds a digest-bound "plan → approve → run" contract. On a feature checklist it covers roughly 70–80% of the idea. Archon (23.6k stars) gives the same shape at production scale, but freezes the graph per run and has no replay. orc and yak are tiny proofs of concept: orc runs `claude -p`/`codex exec` per node, and yak keeps an append-only journal as its only state. Claude Code's own dynamic workflows make every worker an agent, but they are Claude-only and resume only within a session. On the science side, CatGo has the right HPC durability (a stateless Slurm-polling scanner and a review gate before HPC spend), but its nodes are fixed calculation types. DREAMS has role agents on Slurm with parameter-level provenance, but no durable runtime. No surveyed project combines four things: (1) agent-only nodes run by interchangeable external harnesses that can **park for days on a Slurm job without holding a process**; (2) **mid-run DAG amendments recorded as approved plan diffs**; (3) a file-first plan + append-only log; (4) scientific provenance of agent decisions. That combination is the real, narrow gap. Pitching the CLI + skill/MCP surface, approval gates or replay as novel would be wrong, because all of them already exist.

## Smithers covers about three-quarters; the remainder is scattered across six projects

The table scores the most relevant projects against the idea's requirements. "Smithers 1.0" refers to the release candidate on `main`, which is not on npm. All facts are as of 2026-10-02 and are sourced in the sections that follow.

| Project | Every node an agent? | External harness nodes | Plan approval before run | Durable, long waits without a held process | HPC/Slurm | Dynamic DAG with recorded/approved amendments | Event log; replay reuses agent outputs | Outer-agent interface | Maturity / license |
|---|---|---|---|---|---|---|---|---|---|
| **Smithers 0.x** | No (agent, compute and static task modes) | Yes: child-process `ClaudeCodeAgent`, `CodexAgent`, `PiAgent`, Cursor, Gemini… | Agent-authored workflow (`create-workflow`), `graph` preview; no plan sign-off | Yes: SQLite; timer/event/approval waits exit the process | No | Graph re-derived every frame (grows at runtime); snapshots recorded but not approved | NDJSON events; rewind/fork/replay reuse persisted outputs | CLI (agent-oriented output) + MCP server + skill | 428★, MIT, v0.35.0 (2026-08-17), effectively one maintainer |
| **Smithers 1.0-rc** | Core agent is an in-process "cell-loop" agent | Only for build targets (`claude -p`, `codex exec`) | **Yes: digest-bound PlanCard** | Yes; "park on job" only at design stage | No | Append-only plan store with diffs | Append-only journal; fork reuses completed results | `Control` service (MCP host left to the user) | rc, not on npm; 0.x state not portable |
| **Archon** | No (`bash`, `script` nodes) | In-process SDKs (Claude Agent SDK, Codex SDK, Pi, OpenCode) | Builder workflow; approval only as nodes | Yes: `wait:` nodes plus external signals; SQLite/Postgres | No | **No**: "A run does not change shape"; `fan_out` only | Events table + JSONL transcript; resume reuses cached outputs, no time travel | CLI + skill + HTTP | 23.6k★, MIT, v0.11.1 (2026-09-25) |
| **orc** | No (`bash` nodes) | Yes: `claude -p`, `codex exec`, `grok -p` | Skill writes the YAML; no plan sign-off | SQLite, resume from unfinished nodes; no external waits | No | No | DB events; run-level resume | CLI + skill | 0★, MIT, v1.3.1 (2026-10-02) |
| **yak** | No (`command`, `transform`) | Claude Agent SDK only | No (file-based gates) | Journal-backed resume; gates only | No | No (acyclic, frozen IR) | **`journal.jsonl` is the sole state**; content-addressed reuse, `replay --from` | CLI | 0★, MIT, v0.5.0 (2026-09-08) |
| **Claude Code workflows** | Yes (every worker is a Claude subagent) | No (Claude only) | Per-run phase preview; never shown in `claude -p` | Same session only; journal prefix-replay | No | JS control flow; no mid-run edits | `journal.jsonl` of return values | `claude -p`/SDK launch; TUI monitoring | Research preview (launched 2026-05-28), proprietary |
| **gh-aw** | One agent job per workflow | Copilot, Claude, Codex, Gemini, Pi | PR review of the `.md` workflow | Via GitHub Actions; agent job default 60 min | No | Orchestrator dispatches workers | Actions logs + `gh aw audit` | CLI + MCP server | 5.3k★, MIT, v0.90.3 (prerelease) |
| **CatGo** | **No: fixed scientific task types** | Agents only author workflows and diagnose failures, via MCP | Per-task `PENDING_REVIEW` before HPC spend | **Yes: stateless SQLite scanner** | **Yes: SLURM/PBS/LSF/SGE over SSH** | `add_task` via MCP, no audit record | Hash provenance; no event log | MCP | 205★, **AGPL-3.0**, v1.4.14 (2026-08-25) |
| **DREAMS** | Role agents (supervisor, DFT, HPC, convergence) | LangGraph API agents | No (autonomous) | **No**: blocking poll; in-memory `MemorySaver` | Yes (pysqa) | Supervisor re-plans | v2 paper: append-only provenance registry (code not public) | None | 29★, no license |
| **jobflow / FireWorks / AiiDA** | No (fixed functions/CalcJobs) | No | aiida-agents: approval per submission | Yes (daemons, DB) | Yes | **Yes: `Response(replace/addition/detour)`, `FWAction`**; not approved | Strong data provenance; no LLM rationale | Python/CLI; thin MCP front-ends | Mature ecosystems |
| **MS Agent Framework** | Executors; agents optional | Copilot CLI/SDK, Claude Agent SDK (in-process) | YAML declarative workflows (1.0) | Superstep checkpoints (file/Cosmos) | No | No (topology must match to rehydrate) | Entry checkpoints "replayable" | SDKs | 13.9k★, MIT |
| **Temporal / DBOS + agent SDKs** | Code workflows; LLM calls as steps | No (in-process agent loops) | Only as runtime HITL gates | Yes (hours–weeks) | No | Code-defined | **Recorded LLM results reused on replay**; DBOS fork/rewind | CLI; DBOS MCP for monitoring | Mature; agent layers in preview |

The table shows a pattern. Each requirement is met somewhere, usually by a project that misses most of the others. Smithers is the only project with green cells in nearly every column, and its holes are exactly the science-specific ones: HPC/Slurm and *approved* amendments. That is why the rest of this report treats Smithers as the benchmark and asks what the idea adds beyond it.

## Five categories converge on the same building blocks

### Harness orchestration: vendors make every worker an agent, but only their own

Anthropic's mechanisms are the closest vendor match to "every node is an agent". A Claude Code dynamic workflow is "a JavaScript script that orchestrates many subagents at once". The script decides what runs next. It has no shell or filesystem access, so "Agents read, write, and run commands" ([Claude Code workflows](https://code.claude.com/docs/en/workflows)). The workers are always Claude. The docs say that "In every approach the workers are Claude sessions. To involve a different tool, expose it to Claude as an MCP server" ([Claude Code agents](https://code.claude.com/docs/en/agents)).

Durability is limited:

- **Resume is prefix replay, scoped to a session.** Completed agents return saved results. "The first agent whose prompt differs … runs again, and so does every agent after it", and "In a session you start fresh, Claude has no earlier run to relaunch" ([workflows docs](https://code.claude.com/docs/en/workflows)).
- **There is a pre-run approval, but not a contract.** The CLI shows the planned phases with "Yes, run it / View raw script / No". In `claude -p` and the SDK, "Claude Code never shows this prompt". There is "No mid-run user input" ([workflows docs](https://code.claude.com/docs/en/workflows)).
- **Agent view runs sessions under a supervisor that survives a closed terminal, but "Shutting down still stops running sessions"** ([agent view](https://code.claude.com/docs/en/agent-view)).

OpenAI's Agent Builder mixed agent and fixed nodes and was deprecated on 2026-06-03, with shutdown on 2026-11-30 ([OpenAI community notice](https://community.openai.com/t/deprecation-notice-agent-builder/1382650)).

The third-party session managers each run one agent per task in a git worktree, and none of them is a DAG engine. That group includes Claude Squad, ccmanager, Conductor, Crystal (deprecated), Vibe Kanban (sunsetting) and Sculptor ([claude-squad](https://github.com/smtg-ai/claude-squad); [vibe-kanban](https://github.com/BloopAI/vibe-kanban)). Gas Town is the outlier. It spawns Claude, Codex or Gemini "polecats" per task in tmux sessions over a git-backed beads ledger, with watchdog agents for stall detection ([Gas Town](https://github.com/steveyegge/gastown)). It is coding- and merge-queue-oriented, and its author calls it "100% vibe coded" ([Wikipedia: Steve Yegge](https://en.wikipedia.org/wiki/Steve_Yegge)).

### Coding-agent DAG engines: the direct competitors are young or code-shaped

Four projects put a coding agent inside a DAG node: Smithers, Archon, orc and yak. All four also allow non-agent nodes. Archon states the design principle that the idea rejects: "Mix deterministic nodes (bash scripts, tests, git ops) with AI nodes… The AI only runs where it adds value" ([Archon README](https://github.com/coleam00/Archon/blob/main/README.md)). GitHub Agentic Workflows sits adjacent. It compiles Markdown workflows into Actions jobs that run Copilot, Claude, Codex, Gemini or Pi, with an MCP server and `gh aw audit`. It runs one agent job per workflow, and the agent job defaults to 60 minutes ([gh-aw README](https://github.com/github/gh-aw/blob/main/README.md); [steps-jobs reference](https://github.com/github/gh-aw/blob/main/docs/src/content/docs/reference/steps-jobs.md)). The section on closest prior art covers these engines in detail.

### Multi-agent graph frameworks: nodes are functions, and checkpoints fall between in-process steps

LangGraph, CrewAI, ADK, LlamaIndex, Mastra, pydantic-graph, Haystack and Agno all treat a node as a typed function. An LLM agent is one kind of node you can pick, not the base unit. Two frameworks wrap external coding agents:

- **Microsoft Agent Framework** treats GitHub Copilot (CLI/SDK) and the Claude Agent SDK as agent services usable inside graph workflows ([MAF agent services](https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/agent-services/)). It ships YAML declarative workflows that reached 1.0 in July 2026 ([MS Learn](https://learn.microsoft.com/agent-framework/workflows/declarative)). It requires that "A rehydrated workflow must preserve the topology", which rules out mid-run graph edits ([MAF checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)).
- **OpenHands Agent Canvas** can "Run OpenHands, Claude Code, Codex, Gemini, or any ACP-compatible agent" ([OpenHands](https://github.com/OpenHands/OpenHands)).

Durability across this whole category assumes a node finishes within one process lifetime. LlamaIndex states the semantics plainly: an in-flight step "is rewound and runs again from the top" ([LlamaIndex durable workflows](https://developers.llamaindex.ai/python/llamaagents/workflows/durable_workflows)). For a node that is a three-day Slurm job, that means duplicate submission unless the node itself stores the job ID. None of these frameworks has a planner-negotiated, human-approved plan artifact. CrewAI's `planning=True`, for example, auto-generates a plan with no approval step ([CrewAI planning](https://docs.crewai.com/en/concepts/planning)).

### Durable execution engines: they solved replay, at the wrong granularity

Temporal, Restate, DBOS, Inngest, Hatchet, Vercel Workflow and Airflow's Common AI provider all settle on the same pattern: deterministic orchestration code, with every LLM or tool call journaled as a step whose output is reused on replay. In Temporal's OpenAI Agents SDK integration, model calls "retry durably and are not repeated during Workflow replay" ([Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents)). Airflow's `@task.agent` with `durable=True` caches "each model response and tool result", so that "on retry, cached steps replay instantly" ([Airflow blog](https://airflow.apache.org/blog/common-ai-provider/)).

DBOS has the richest per-step control. `fork_workflow` copies a workflow's steps up to a chosen step and runs from there under a new ID. `rewind_workflow` "discards the workflow's recorded steps from the selected step onward" under the same ID ([DBOS workflow management](https://docs.dbos.dev/python/tutorials/workflow-management)).

The limitation is granularity. These engines journal individual model calls inside an in-process agent loop. An external `claude -p` or `codex exec` subprocess owns its own loop, so the activity boundary becomes the whole subprocess. Nextflow's nf-agent is the nearest science-shaped analogue: an agent becomes "an ordinary Nextflow task" with resume and lineage. It calls an OpenAI-compatible API from the driver JVM, though, and its docs say nothing about Slurm ([nf-agent](https://registry.nextflow.io/plugins/nf-agent)). None of these engines has a Slurm executor, and the lightest ones (the DBOS library on SQLite, the Temporal single-binary dev server) still assume a long-lived process. Many HPC centers discourage that on login nodes.

### Agent task and plan CLIs: the contract UX is commoditized, the runtime is not

Spec-driven kits have very large followings: GitHub Spec Kit (~140k stars), OpenSpec (~71k), BMAD (~54k), and Superpowers, which the API reports at 294k stars (worth a sanity check). They all make "review the plan before any code is written" standard practice ([OpenSpec](https://github.com/Fission-AI/OpenSpec); [Spec Kit](https://github.com/github/spec-kit)). Kiro goes furthest toward execution. It "analyzes task dependencies and executes independent tasks concurrently across 'waves'", but only inside its own IDE agent ([Kiro specs](https://kiro.dev/docs/specs/)).

Ledgers prove that agents can maintain a dependency graph through a CLI or MCP. Beads, for example, describes itself as "a ledger and coordination system—it does not execute tasks" ([beads](https://github.com/steveyegge/beads)). None of them schedules or runs nodes.

Two conventions are the best precedents for recording amendments:

- **OpenAI's ExecPlans** require a living plan with a Decision Log ("Decision: … / Rationale: … / Date/Author") ([OpenAI Cookbook](https://developers.openai.com/cookbook/articles/codex_exec_plans)).
- **Anthropic's long-running harness** allows the agent to edit the feature list "only by changing the status of a passes field" ([Anthropic engineering](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)).

Both are prose conventions, not typed events with approval state.

### Scientific workflow and agent systems: durable engines without agents, agents without durable engines

Computational chemistry splits cleanly in two.

**Mature engines** (AiiDA, jobflow/jobflow-remote, FireWorks, dflow, Parsl) already provide HPC scheduling, resume, provenance and even runtime graph growth. jobflow's `Response` can `replace` a job with a new flow, `addition` new jobs, or `detour`, so the job count is not fixed in advance ([jobflow dynamic flows](https://materialsproject.github.io/jobflow/tutorials/5-dynamic-flows.html)). Their LLM layers are thin front-ends. aiida-agents, for example, is approval-gated ("nothing is written without your say-so"), serves read-only MCP tools, and is an alpha with about 5 stars ([aiida-agents](https://github.com/aiidateam/aiida-agents)).

**LLM-agent systems** use agents as planners over fixed tools, rarely with a durable, approved DAG. These include VASPilot (CrewAI + MCP + Slurm auto-restart), El Agente, ChemGraph, MDCrow and Masgent ([VASPilot](https://arxiv.org/pdf/2508.07035); [El Agente Q](https://arxiv.org/abs/2505.02484v2); [ChemGraph](https://arxiv.org/pdf/2506.06363)).

Three recent papers sit closest to the idea:

- **AutoDFT** gives empirical support for agent-per-node. It "embed[s] LLM reasoning into every stage", inserts intermediate steps as results come in, and reports 94.1% task success on the 34-task VASPBench ([AutoDFT](https://arxiv.org/abs/2605.26179)).
- **OpenClaw with domain skills** pairs a general-purpose agent with an "Agent Taskboard Manifest" (stages, dependencies, validation criteria) and DPDispatcher for Slurm/PBS/LSF. It is the closest "general coding-style agent + explicit dependency manifest + real HPC" system, but it does not describe persistence or plan approval ([arXiv 2603.25522](https://arxiv.org/html/2603.25522)).
- **Diamond Agent** decouples agent turns from batch jobs with "event-driven continuation". Persistent services "resume the agent only when a result or decision-relevant event is available". This cut overhead from a 42 s to a 4 s median across 83 jobs on four supercomputers ([Diamond Agent](https://arxiv.org/abs/2609.06181)).

Anthropic's own guidance for science is the baseline the idea improves on: a single Claude Code session in tmux on an HPC system, with CLAUDE.md as the plan, CHANGELOG.md as memory, and a Ralph loop ([Anthropic: long-running Claude for scientific computing](https://www.anthropic.com/research/long-running-Claude)).

## The closest prior art, project by project

### Smithers: the benchmark the idea must beat

Smithers 0.x workflows are TSX trees of `<Task>`, `<Sequence>`, `<Parallel>`, `<Branch>` and `<Loop>`, with approvals, signals, timers and sandboxes. Each task's Zod-validated output is persisted to SQLite ([smthrs on npm](https://www.npmjs.com/package/smthrs)). The bundled docs (in the [0.35.0 tarball](https://registry.npmjs.org/smthrs/-/smthrs-0.35.0.tgz), `docs/llms-full.txt`) document the following.

**Agent nodes.** CLI agents "run as a child process": `ClaudeCodeAgent`, `CodexAgent`, `OpenCodeAgent` and others "spawn the vendor binary via `node:child_process`". For harnesses without native structured output, Smithers injects JSON instructions, validates the reply, and retries on failure.

**Durability.** "Every completed step is persisted to SQLite the moment it finishes", and "a completed task is never re-executed". `up` exits on `waiting-approval/event/timer` "rather than burning a process". A `<Timer duration="7d">` "holds no worker and no CPU". `supervise` auto-resumes runs whose owner died.

**Control.** `retry-task` resets a node and, by default, its dependents. `steer` delivers a durable message to a node's next agent step. `hijack` takes over a live agent session.

**Graph growth.** "The plan is a derived value, recomputed on every state change", so `{tickets.map(...)}` grows the graph at runtime, and every frame commit produces a `GraphSnapshot`.

**Audit and replay.** `rewind`, `fork --frame N --reset-node X` and `replay` reuse the parent's finished outputs. "A plain `fork` without `--run` does not re-execute anything". An effect journal tracks intended/succeeded/unknown side effects, with compensation handlers.

**Outer-agent surface.** The surface is a CLI that emits agent-oriented JSON envelopes with "Next steps". Exit code 3 means "parked waiting". There is also an MCP server (`run_workflow`, `watch_run`, `resolve_approval`, `rewind_run`…) and an installed skill.

The 1.0 rewrite on `main` adds the piece most relevant to the idea's contract. `Control.plan` returns a `PlanCard`: the flow, the input summary, the keyed node graph, and "a digest over all of it". The first `run` parks until approval, and "an approval taken on this envelope cannot authorize a wider one later" ([control quickstart](https://github.com/smithersai/smithers/blob/main/packages/smithers/control/docs/quickstart.md)). The plan store is append-only, enforced in SQL ([plan-store README](https://github.com/smithersai/smithers/blob/main/packages/smithers/flows/plan-store/README.md)). A design doc approved on 2026-10-02 compares "attach inside one action" with "park on job" (Start/Poll/Collect, where "A suspended run holds no lease"). That is the exact primitive a Slurm node needs, but the target is "10-60 min coding agents in microVMs or Cloud workspaces" ([durable-external-work.md](https://github.com/smithersai/smithers/blob/main/docs/design/durable-external-work.md)).

Four facts temper Smithers as a foundation:

- **The 1.0 core agent is an in-process "cell-loop" agent, not `claude -p`.** A foreign-CLI agent is described only as "another implementation of `Agent.Service`" ([agent README](https://github.com/smithersai/smithers/blob/main/packages/smithers/agent/README.md)).
- **1.0 "never loads, resumes, or migrates a 0.x run database"** ([replacements.md](https://github.com/smithersai/smithers/blob/main/apps/docs/smthrs/src/content/docs/replacements.md)).
- **The README now pitches a hosted "Smithers maintains your codebase" product** ([README](https://github.com/smithersai/smithers/blob/main/README.md)).
- **One person wrote about 9,734 of roughly 15.8k commits** ([contributors API](https://api.github.com/repos/smithersai/smithers/contributors)).

A search of the 1.1 MB of 0.x docs found no mention of Slurm, HPC, sbatch or pysqa.

### Archon: production scale, frozen graphs

Archon (23.6k stars, MIT, v0.11.1 on 2026-09-25) is a YAML DAG engine with these node types: `prompt`, `command`, `bash`, `script`, `loop`, `approval`, `wait`, `cancel` and child `workflow`. Each node chooses its provider: Claude, Codex, Pi, OpenCode or Copilot, all called through in-process SDKs ([authoring-workflows](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/guides/authoring-workflows.md); [provider-capabilities](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/provider-capabilities.md)).

Its `wait:` node is the closest existing building block for Slurm. It "records its condition in the workflow run, changes the run to `paused`, and returns the worker slot", and it can be woken by a `POST …/signal`. A Slurm epilogue could, in principle, send that signal. Resume skips completed nodes and feeds their cached outputs downstream ([authoring-workflows](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/guides/authoring-workflows.md)).

Archon deliberately rejects the idea's dynamic contract: "A run does not change shape while it is running… Start a new run to pick up the edits." It has no time travel, no documented "rerun this node and downstream" verb, no plan-level approval, and no MCP server exposing itself ([CLI reference](https://github.com/coleam00/Archon/blob/main/packages/docs-web/src/content/docs/reference/cli.md)).

### orc and yak: the idea in miniature, twice

orc (0 stars, MIT, v1.3.1 released on 2026-10-02) is almost the idea's execution model verbatim. Each node picks `claude`, `codex` or `grok`, the runners build argv with `-p`/`exec`, each step starts in a fresh session in its own worktree, and artifacts are "the only way context travels from one node to the next". It has approval nodes with `on_reject` rework, SQLite state with a frozen spec copy per run, stop/cancel/resume, and a skill so an outer agent "picks the workflow that fits, starts it, watches until it finishes" ([orc](https://github.com/tjdals12/orc)).

yak (0 stars, MIT, v0.5.0) is almost the idea's storage model verbatim. Its thesis is that "An agentic workflow engine is a build system whose compilers are nondeterministic". Each run directory holds:

- `workflow.json` (the frozen graph IR)
- `journal.jsonl` ("append-only event log — source of truth")
- `artifacts/`
- `pending/<step>.request.json|answer.json` file-based gates, with exit code 78 on suspend.

Resume reuses artifacts whose cache keys match and reruns a mismatched step "and everything downstream" ([yak spec](https://github.com/lchase/yak/blob/main/spec.md)). yak lists distributed execution and servers as non-goals: "use Archon or Temporal instead".

Neither project has dynamic graphs, external long waits, HPC support or MCP. Together they show that the small end of this design space is crowded, but nobody there has adoption.

### CatGo: the HPC half, without agent nodes

CatGo (205 stars, **AGPL-3.0**, v1.4.14) combines a 3D structure viewer, a visual DAG editor, SLURM/PBS/LSF/SGE adapters and MCP tools for Claude Code and Codex ([CatGo README](https://github.com/Hello-QM/catgo-LRG/blob/main/readme.md)). Its workflow engine is "a stateless periodic scanner. Each scan_cycle() reads DB, advances task states, and returns. No in-memory state between cycles. Crash and restart safely." It drives a 17-state task machine (READY → UPLOADING → SUBMITTED → QUEUED → RUNNING → COMPLETED_REMOTE → COLLECTING → COMPLETED, plus PENDING_REVIEW, PAUSED, MAPPED and others). HPC tasks "pause at `PENDING_REVIEW`" so "users can verify structures and parameters before spending HPC resources". `retry_task` resets "a task and all downstream dependents". These details come from the local clone's `server/catgo/workflow/` (see [repo](https://github.com/Hello-QM/catgo-LRG)).

Its nodes, however, are a fixed catalog (`geo_opt`, `freq`, `md`, `ts_search`, `gibbs_energy`…). LLMs stay outside the DAG, where they author workflows and diagnose failed jobs. `add_task` via MCP extends a live workflow with no approval or audit record. One caveat: the inspected clone dates from 2026-05-17, while upstream is about four months newer.

### DREAMS: agent decisions as provenance, without a durable runtime

DREAMS is a LangGraph hierarchy: a planning supervisor plus DFT, HPC and convergence agents, running Quantum ESPRESSO through pysqa on Slurm ([arXiv v1](https://arxiv.org/html/2507.14267v1)). The v2 paper (2026-08-11) adds what the idea calls "agent decisions recorded". Tool outputs are "stored in an append-only provenance registry under an immutable result identifier" with "parameter-specific justifications", and a provenance DAG is assembled during execution ([arXiv v2](https://arxiv.org/html/2507.14267v2)).

The public code shows the runtime gap. The pysqa tool loops on `get_status_of_job` until jobs finish, so the agent's tool call blocks. LangGraph state uses an in-memory `MemorySaver`, and no provenance code is on `main` ([src/tools.py](https://github.com/BattModels/material_agent/blob/main/src/tools.py); [src/graph.py](https://github.com/BattModels/material_agent/blob/main/src/graph.py)). The repo has no license.

### jobflow, FireWorks and AiiDA: the substrate, not the competitor

These engines already persist, schedule and grow graphs at runtime, and AiiDA records full data and calculation provenance ([AiiDA](https://www.aiida.net); [FireWorks](https://materialsproject.github.io/fireworks/); [jobflow-remote](https://github.com/Matgenix/jobflow-remote)). What they lack is exactly what the idea adds: agent executors, plan approval, and LLM rationale in the record.

jobflow's `replace/addition/detour` vocabulary is the most mature existing grammar for "the DAG grows during execution". An agent-per-node tool could adopt that vocabulary for amendments, or could even submit through jobflow-remote or AiiDA rather than reimplement HPC transport.

## The unfilled gap is a four-part conjunction, not any single feature

Several elements of the idea are already prior art and should not be claimed as novel. A durable agent-workflow CLI driven by an outer coding agent through a skill or MCP already ships in Smithers, Archon, orc and gh-aw. Approval gates are universal. Per-node retry with downstream reset exists in Smithers and CatGo. Replay that reuses recorded agent outputs exists in Smithers and yak, and the durable engines reuse recorded LLM calls. Agent-per-node headless harness execution exists in Smithers 0.x and orc.

What no project combines is the following:

1. **Agent-only nodes run by interchangeable external harnesses that park durably on HPC work.** A node submits a Slurm job, records the job ID and the harness session ID, exits, and is woken hours or days later by a stateless poller or a callback, without a held process. CatGo has the scanner but no agent nodes. DREAMS has Slurm agents but blocks. Smithers 1.0 has "park on job" only as a design aimed at cloud sandboxes. Diamond Agent proves the wake-on-event pattern for a single agent, not for a DAG.
2. **Amendments as first-class, approved plan diffs.** In this model an agent proposes a DAG diff mid-run, the user approves it, and the diff is appended to the plan log, so replay re-applies the same mutation. Smithers records unapproved re-render snapshots, Archon forbids mid-run shape changes, CatGo's `add_task` and jobflow's `Response` are unaudited, and ExecPlans' Decision Log is prose. The nearest primitive is Smithers 1.0's append-only plan store with digest-bound approval, which is unreleased and not tied to amendments.
3. **A file-first contract.** This means a human-readable plan file plus a JSONL event log as the sole source of truth, suitable for an HPC home or scratch filesystem with no daemon or database. Only yak does this, and it is a 0-star, Claude-SDK-only proof of concept.
4. **Scientific provenance of agent decisions joined to a durable runtime.** DREAMS v2 shows parameter-level justifications on paper. AiiDA shows data lineage without rationale. Nobody records both, per node, in a replayable log.

The defensible novelty is therefore narrow and specific: an **HPC-native, file-first, amendment-audited** agent-per-node runtime for research. It is not "an agent workflow CLI". Plan negotiation is only a partial differentiator: the spec kits already commoditize the UX, and Smithers 1.0 has the digest-bound mechanism. The difference is binding that approval to a DAG that keeps evolving under audit.

## "Every node is an agent" turns replay into audit, not re-execution

Choosing agent-only nodes with no fixed tool list has consequences for reproducibility, and the prior art already marks out the options.

**Record-and-reuse is the only workable replay model, and it gives audit, not reproducibility.** Every durable engine replays by reusing recorded nondeterministic results rather than re-calling the model ([Restate](https://docs.restate.dev/ai/patterns/durable-agents.md); [Temporal](https://docs.temporal.io/develop/python/integrations/openai-agents)). LangGraph shows the alternative: "Replay re-executes nodes… LLM calls, API requests, and interrupts fire again and may return different results" ([LangGraph time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)). For an agent-per-node DAG, replay therefore means "reconstruct exactly what happened from the log", while *re-execution* of any node gives a new, different sample. The tool should keep these as separate, explicitly named operations (replay/inspect vs. rerun/fork), as Smithers does with "none of them re-executes anything" for time-travel verbs ([time-travel README](https://github.com/smithersai/smithers/blob/main/packages/smithers/flows/time-travel/README.md)).

**The journal unit is the whole harness subprocess.** Because `claude -p` or `codex exec` owns its own loop, the engine cannot replay partway through a node. Partial recovery inside a node depends on the harness's own session resume, such as `codex exec resume` or Claude Agent SDK session `resume`/`fork` ([Codex non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode); [Agent SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions)). The node record must therefore capture the session ID, the transcript and the harness version. In a DAG, invalidation should follow dependency edges, as yak does, rather than Claude Code's call-order prefix rule. Under that rule a mid-fan-out failure reruns "every agent that started after it", which is wasteful when sibling nodes are multi-day Slurm jobs ([workflows docs](https://code.claude.com/docs/en/workflows)).

**No fixed tool list weakens structural provenance.** In AiiDA a calculation's inputs and outputs are typed graph nodes by construction. An agent with a shell can compute anything, so the only provenance is what the runtime chooses to capture. That means content hashes of every produced file, the generated input decks and job scripts, Slurm job IDs and accounting, the model ID, the harness version, the prompt and the transcript. yak's `semanticKey/definitionKey/artifactHash` events and CatGo's SHA-256 lineage are the models to copy ([yak spec](https://github.com/lchase/yak/blob/main/spec.md)).

**Harness churn sets an expiry date on re-execution.** orc pins `claude >= 2.1.261` and `codex >= 0.153.4`, and Archon tracks fast-moving SDK versions ([orc](https://github.com/tjdals12/orc); [Archon changelog](https://github.com/coleam00/Archon/blob/main/CHANGELOG.md)). A run recorded today will not be re-executable bit-for-bit next year. That makes the recorded artifacts, not the prompts, the scientific record.

The strongest mitigation follows from this. An agent node should *emit deterministic artifacts* (input files, scripts, sbatch files) that can be rerun without the agent, so the agent's role in the record is the *decision* (with rationale), and the computation itself stays reproducible in the classical sense.

**The steelman for mixed nodes is real.** Archon, Smithers, orc and yak all allow cheap deterministic nodes, for cost, latency and variance reasons. The idea can stay pure and still capture most of that benefit if an agent node that produced a script can be "pinned" so later reruns execute the recorded script. AutoDFT's results argue for reasoning at every stage ([AutoDFT](https://arxiv.org/abs/2605.26179)). LQCDMaster's report, seen only as a search snippet, that general coding agents without domain skills failed some lattice-QCD tasks argues for pairing agent nodes with domain skills and validation criteria ([arXiv 2607.15001](https://arxiv.org/pdf/2607.15001)).

## Risks and the design ideas worth borrowing

### Risks

**Competition and dependency.** Smithers is the main competitive risk. It is shipping fast, already has headless agents + durability + MCP + skill, and has designed "park on job". Any HPC adapter it adds would close much of the gap. It is also a risky foundation: single maintainer, an incompatible rewrite, and a pivot toward a hosted product. Anthropic's workflows are a second vector, since they launched on 2026-05-28 and press coverage already frames them for jobs that "typically take hours or days" ([reworked.co](https://www.reworked.co/digital-workplace/anthropic-announces-dynamic-workflows-in-claude-code/)).

**Operational.** HPC centers often disallow long-lived daemons on login nodes, which argues for a stateless, periodically invoked scanner over any server. Headless harnesses also lose some interactive conveniences. Claude Code workflows auto-continue after usage limits only in interactive sessions, "not `claude -p` or the SDK" ([workflows docs](https://code.claude.com/docs/en/workflows)). Multi-day runs therefore need explicit handling of rate limits and quotas as a parked state.

**Correctness.** At-least-once re-execution of in-flight nodes can double-submit Slurm jobs unless the job ID is journaled before the node waits, which is what Smithers' intended/succeeded/unknown effect journal addresses. An agent with shell access on a cluster also holds real credentials and allocation budget. gh-aw's read-only-by-default agents with validated "safe outputs", CatGo's PENDING_REVIEW before HPC spend, and aiida-agents' approval of every write are the relevant guardrail patterns ([gh-aw](https://github.com/github/gh-aw/blob/main/README.md)).

**Licensing.** CatGo is AGPL-3.0, so reusing its code would carry copyleft obligations, and DREAMS' repository has no license.

### Design ideas to borrow

| Borrow | From | How it maps to the idea |
|---|---|---|
| Deterministic orchestrator + recorded nondeterministic steps; replay never re-calls the model | Temporal, Restate, Airflow `durable=True` ([Temporal](https://docs.temporal.io/develop/python/integrations/openai-agents)) | Plan file = deterministic workflow. Each agent node's final output, artifacts and decisions = recorded activity result. |
| `fork` (new run from step N) and `rewind` (same run, discard suffix) | DBOS ([workflow management](https://docs.dbos.dev/python/tutorials/workflow-management)) | `rerun <node>` = rewind the log suffix from the node and its downstream. `fork` = try an alternative branch without losing the original. |
| Digest-bound plan approval; approval cannot authorize a wider envelope | Smithers 1.0 PlanCard ([control API](https://github.com/smithersai/smithers/blob/main/packages/smithers/control/docs/api.md)) | Hash the plan file and record the approval against the hash. Each amendment gets a new digest and its own approval event. |
| Append-only plan store enforced at the storage layer | Smithers 1.0 plan-store ([README](https://github.com/smithersai/smithers/blob/main/packages/smithers/flows/plan-store/README.md)) | Amendments only add or supersede nodes, never rewrite them, which gives deterministic replay of graph growth. |
| Stateless scanner over persisted job states; crash-safe | CatGo `scan_cycle()` | One `tick` command advances SUBMITTED/QUEUED/RUNNING nodes via `squeue`/`sacct`. It can be run by cron or by the outer harness, so no daemon is needed. |
| "Park on job" (Start/Poll/Collect; no lease while suspended) | Smithers 1.0 design doc ([durable-external-work](https://github.com/smithersai/smithers/blob/main/docs/design/durable-external-work.md)) | Split an HPC agent node into an agent turn (prepare + submit), a parked state, and an agent turn (collect + interpret). |
| Event-driven agent continuation | Diamond Agent ([arXiv 2609.06181](https://arxiv.org/abs/2609.06181)) | Re-invoke the harness only when a job finishes or fails, resuming the recorded session ID. |
| Journal as sole state; file-based gates; content-addressed cache keys | yak ([spec](https://github.com/lchase/yak/blob/main/spec.md)) | `run/<id>/{plan, events.jsonl, artifacts/, pending/}`. Any frontend answers a gate by writing a file. |
| Frozen source per run + manifest digest checked on resume; explicit override flag | Archon, Smithers (`RESUME_METADATA_MISMATCH`, `--accept-workflow-change`) | Guard against silent drift between the approved plan and the executing one. |
| Agent-oriented CLI envelopes with "next steps"; distinct exit code for "parked" | Smithers 0.x | Lets Codex or Claude Code drive the CLI reliably through a skill without parsing prose. |
| Dynamic-growth vocabulary `replace / addition / detour` | jobflow ([docs](https://materialsproject.github.io/jobflow/tutorials/5-dynamic-flows.html)) | Typed amendment kinds that an agent may *propose* and the user approves. |
| Decision Log entries (Decision / Rationale / Author) and "only flip status" rules | OpenAI ExecPlans, Anthropic harness | Typed `decision_recorded` events, and a schema that limits what a node agent may change in the plan. |
| Parameter-level justifications with immutable result IDs | DREAMS v2 ([arXiv](https://arxiv.org/html/2507.14267v2)) | Scientific audit: why this ENCUT, this k-mesh, this functional. |
| ACP / multi-harness detection | OpenHands Agent Canvas, Smithers `harness-detect` | A thin adapter layer so nodes are not tied to one vendor's CLI flags. |

## Conclusion

The prior-art survey changes the question from "does this exist?" to "which slice is worth owning?" The generic slice is a durable, approvable, replayable agent DAG driven by an outer coding agent. Smithers already occupies it, Archon occupies it at scale, and vendor features are moving into it. Competing there means racing a fast single maintainer and Anthropic at the same time. The science slice is the one nobody serves. It consists of agent nodes that park on Slurm for days without a daemon, amendments that are approved and replayable, and a log that records *why* a parameter was chosen next to *what* was computed. The pieces to build it are proven elsewhere: CatGo's scanner, Smithers' digest approval and park-on-job design, yak's journal, DBOS's rewind, and jobflow's amendment grammar. That suggests a thin integration layer rather than a new engine, and it may mean submitting through an existing HPC substrate such as jobflow-remote, AiiDA or DPDispatcher rather than reimplementing transport.

The "every node is an agent" principle is the idea's most distinctive and most fragile choice. It is defensible only if the tool redefines what reproducibility means for such runs. The log must make every agent decision auditable, and every agent must leave behind deterministic artifacts that can be rerun without it. If the runtime enforces that, agent-per-node becomes an advantage for scientific transparency. If it does not, the runs become unrepeatable transcripts. The design work that matters most is therefore not in scheduling but in the event schema: what a node must record before it is allowed to complete.
