# Multi-agent graph/workflow frameworks as prior art for an agent-per-node, durable, plan-as-contract workflow CLI

Scope: LangGraph (+ Agent Server, ex-LangGraph Platform), CrewAI, AutoGen/AG2, Microsoft Agent Framework (MAF), Google ADK, LlamaIndex Workflows, Mastra, Pydantic AI / pydantic-graph, Haystack, smolagents, DSPy, Agno, OpenHands, MetaGPT/ChatDev. Research date 2026-10-02. GitHub numbers were pulled from the GitHub REST API (`api.github.com/repos/...`) on 2026-10-02 unless noted.

The idea being evaluated (for reference): every DAG node is run by an autonomous headless coding-agent harness (`claude -p`, `codex exec`, pi, OpenHands); the DAG is a contract that the user and a planner agent agree on before execution; runs are durable over hours to days on HPC/Slurm; runs can be monitored and cancelled, resumed or rerun per node; the DAG can change during a run; state is a plan file plus an append-only event log; an outer harness drives it through a CLI, a skill or MCP.

## Q1. Is every node an autonomous agent with its own tool loop? Can a node be an external CLI agent (claude -p / codex exec)?

### Takeaway
In every framework surveyed, a node is a typed function or "executor" by default. An LLM agent with its own tool loop is one kind of node you can choose, not the base unit. Only two projects ship built-in wrappers that put an external coding-agent harness inside a node: Microsoft Agent Framework (GitHub Copilot CLI/SDK and Claude Agent SDK agents usable as workflow agents) and OpenHands Agent Canvas (runs Claude Code, Codex, Gemini or any ACP agent). None of them uses shell-invoked `claude -p` / `codex exec` as its node model.

### Cited Findings
- **LangGraph**: nodes are functions that read and write a shared state. The engine uses a Pregel-style message-passing model with "super-steps"; parallel nodes share a super-step. — [LangGraph nodes/edges (mintlify mirror of docs)](https://www.mintlify.com/langchain-ai/langgraph/concepts/nodes-edges). The usual pattern is that LangGraph defines the graph and checkpointing while Claude is called "inside specific nodes where LLM reasoning is required" — [mager.co guide (secondary)](https://mager.co/blog/2026-03-07-langgraph-claude-agent-sdk-ultimate-guide/). LangSmith Agent Server can host "agents built with other frameworks—such as Strands, Claude Agent SDK, and more" using wrapper packages — [LangSmith Agent Server docs](https://docs.langchain.com/langsmith/agent-server).
- **CrewAI Flows**: steps are Python methods marked with `@start`, `@listen` and `@router`. Crews (multi-agent teams) are embedded by calling `CrewName().crew().kickoff()` inside a `@listen` method. Flows support "chaining multiple crews together, where the output of one crew is used by another." — [CrewAI Flows docs](https://docs.crewai.com/en/concepts/flows)
- **Microsoft Agent Framework**: workflows are graphs of "executors" with edges, run in supersteps. Agents are wrapped by the built-in `AgentExecutor`, whose checkpoint state includes `agent_session` and `full_conversation`. — [MAF checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints). MAF lists these as "agent services": **GitHub Copilot** (C#/Python/Go), which owns the "coding-agent runtime, sessions, permissions, built-in shell/file/URL capabilities, and MCP connections"; **Anthropic Claude** (Python only), which owns the "Claude Agent SDK runtime, sessions, permissions, built-in tools, and MCP connections"; plus Foundry, Copilot Studio and A2A — [MAF Agent services](https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/agent-services/). The GitHub Copilot agent is "backed by the GitHub Copilot CLI and SDK, where Copilot owns the agent loop" and Agent Framework adds tools, middleware and HITL approval. It was reported stable for .NET and Python in Aug 2026 — [GitHub Docs: MAF integration](https://docs.github.com/en/copilot/how-tos/copilot-sdk/integrations/microsoft-agent-framework); [MAF devblog Jan 27 2026](https://devblogs.microsoft.com/agent-framework/build-ai-agents-with-github-copilot-sdk-and-microsoft-agent-framework/).
- **Google ADK**: the 1.x "template" workflow agents (Sequential/Parallel/Loop) are deterministic and run sub-agents "without consulting an AI model for assistance with the orchestration". They have been "superseded by ... graph-based workflows and dynamic workflows" in ADK 2.0 — [ADK workflow agents](https://adk.dev/agents/workflow-agents/). In ADK 2.x, nodes can be function nodes ("wraps a plain function with the metadata required to run within a workflow"), LLM agent nodes, or orchestrator nodes — [ADK dynamic workflows](https://adk.dev/graphs/dynamic/). Remote agents can be reached through A2A (ADK 2.0 overview) — [ADK 2.0](https://adk.dev/2.0/).
- **LlamaIndex Workflows**: "event-driven, step-based". Steps are async Python functions that receive typed events and emit new ones — [LlamaIndex Workflows](https://developers.llamaindex.ai/python/llamaagents/workflows/).
- **Mastra**: `createStep()` units with input/output schemas, composed by `createWorkflow().then()...commit()`. Agents can be used as steps and can stream into steps — [Mastra workflows overview](https://mastra.ai/docs/workflows/overview).
- **pydantic-graph**: nodes are dataclasses that subclass `BaseNode` with a `run()` method. The return type annotations define the outgoing edges — [pydantic-graph docs](https://pydantic.dev/docs/ai/graph/graph/).
- **Haystack**: a Pipeline is a graph of components, and an `Agent` is one component type. Breakpoints "work for any regular component as well as an Agent component." — [Haystack pipeline breakpoints](https://docs.haystack.deepset.ai/docs/pipeline-breakpoints)
- **Agno**: a workflow step's executor can be an Agent ("Individual AI executor with specific tools and instructions"), a Team, a Python function, or a nested Workflow. "Workflow control flow is repeatable. Agent and team outputs can still vary between runs." — [Agno workflows](https://docs.agno.com/workflows/overview)
- **smolagents**: there is no graph. Multi-agent means a manager `CodeAgent` with `managed_agents=[...]`, where each managed agent needs a `name` and `description` "to make this agent callable by its manager agent." — [smolagents multi-agent](https://huggingface.co/docs/smolagents/main/en/examples/multiagents)
- **OpenHands SDK**: a single agent runs a reasoning-action loop. Sub-agent delegation is a tool, and sub-agents are "independent conversations that inherit the parent's model configuration and workspace". The current implementation is "blocking parallel execution" — [OpenHands SDK paper, arXiv 2511.03690](https://arxiv.org/html/2511.03690v1). **OpenHands Agent Canvas** (README of the main repo) can "Run OpenHands, Claude Code, Codex, Gemini, or any ACP-compatible agent across local, remote, and cloud backends" — [OpenHands repo](https://github.com/OpenHands/OpenHands); [Agent Canvas docs](https://docs.openhands.dev/openhands/usage/agent-canvas).
- **ChatDev 2.0 (DevAll)**: workflows are YAML files (`yaml_instance/`) whose nodes are agents, Python functions or human-in-the-loop steps, arranged as a DAG — [ChatDev repo README](https://github.com/OpenBMB/ChatDev).
- **MetaGPT**: "Code = SOP(Team)". Fixed roles (product manager, architect, project manager, engineer) run a standard operating procedure — [MetaGPT README](https://github.com/FoundationAgents/MetaGPT).

### Inferences
- No framework treats "node = headless CLI agent process" as its basic unit. MAF comes closest structurally: Copilot CLI and Claude Agent SDK agents are first-class agent types that can sit in graph workflows as `AgentExecutor`s. They run in-process through SDKs, though, not as `claude -p` / `codex exec` subprocesses, and there is no Codex or pi wrapper.
- OpenHands Agent Canvas (ACP-based, multi-harness) is the closest on "pluggable external coding agents". It is aimed at automations and conversations, not a contract DAG. ACP (Agent Client Protocol) could be a practical integration layer for a node runner.
- In LangGraph, LlamaIndex, Mastra, pydantic-graph, Haystack and Agno, wrapping `claude -p` as a node is a user-written function node. The framework would know nothing about agent sessions, cost or transcripts.

### Gaps
- No primary source found for a built-in Codex CLI (`codex exec`) or pi node in any framework.
- I could not fetch a primary MAF page for the Anthropic Claude agent (a guessed URL returned 404). Its existence rests on the MAF agent-services table.

## Q2. Plan-as-contract: is there a declarative plan artifact that a human reviews and approves before execution? HITL gates?

### Takeaway
Declarative workflow files exist in MAF (YAML declarative workflows, 1.0 in Jul 2026), ChatDev 2.0 (YAML DAG and a visual canvas) and CrewAI (YAML crew configs plus flow plotting). None of them has an explicit "planner agent drafts a DAG, the human approves it, then it runs" contract step. CrewAI's `planning=True` auto-generates a plan with no human approval. Runtime HITL gates are common: LangGraph `interrupt()`, MAF request/response, ADK `RequestInput`, Mastra `suspend()`, CrewAI `@human_feedback`, Agno step-level HITL, LlamaIndex HITL events.

### Cited Findings
- **MAF declarative workflows**: YAML definitions are converted "into executable workflow graphs". The action types include `InvokeAzureAgent`, `If`, `ConditionGroup`, `Foreach`, and HITL actions `Question` and `RequestExternalInput`. They reached 1.0 in both SDKs in July 2026 (Python `agent-framework-declarative` 1.0.0) — [MS Learn declarative workflows](https://learn.microsoft.com/agent-framework/workflows/declarative); [startdebugging.net (secondary), Jul 2026](https://startdebugging.net/2026/07/agent-framework-declarative-workflows-1-0-yaml-orchestration/). A secondary source frames it as "the workflow is a document rather than a call graph", reviewable by non-developers — [startdebugging.net Aug 2026](https://startdebugging.net/2026/08/agent-framework-declarative-yaml-vs-code-first-orchestration/). MAF also lists a "Planning and todos" capability ("Track plans, operational todos, dependencies, and completion") and a "Harness Agent" — [MAF agent capabilities](https://learn.microsoft.com/en-us/agent-framework/agents/).
- **CrewAI planning**: "Before each Crew iteration, all Crew information is sent to an AgentPlanner that will plan the tasks step by step, and this plan will be added to each task description." The default model is `gpt-4o-mini`. The docs mention no human approval step — [CrewAI planning](https://docs.crewai.com/en/concepts/planning). Flows can be plotted to interactive HTML (`plot()`, `crewai flow plot`), and `@human_feedback` "enables human-in-the-loop workflows by pausing flow execution to collect feedback" — [CrewAI Flows](https://docs.crewai.com/en/concepts/flows).
- **LangGraph**: "Interrupts allow you to pause graph execution at specific points and wait for external input". This needs a checkpointer plus a `thread_id` ("your persistent cursor"), and execution resumes with `Command(resume=...)`. On resume, "the node restarts from the beginning of the node where the interrupt was called". Static `interrupt_before` / `interrupt_after` exist but are "not recommended for human-in-the-loop workflows" — [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts).
- **ADK 2.x**: "You build human input into workflows by yielding a RequestInput from a node, which pauses the workflow and waits for user input" — [ADK dynamic workflows](https://adk.dev/graphs/dynamic/).
- **Mastra**: `suspend()` / `resume()` with `resumeData`. Runs can be resumed "from anywhere—HTTP endpoints, event handlers, or timers" (fetched summary of) — [Mastra suspend & resume](https://mastra.ai/docs/workflows/suspend-and-resume).
- **Agno**: step-level HITL lets you "pause execution to collect confirmation or user input before proceeding" — [Agno changelog: step-level HITL](https://www.agno.com/changelog/enforce-human-checkpoints-wherever-you-need-them-with-step-level-hitl).
- **ChatDev 2.0**: YAML workflows, a visual drag-and-drop canvas and a Python SDK (`chatdev` on PyPI). Human-in-the-loop is available as a node type — [ChatDev README](https://github.com/OpenBMB/ChatDev).
- **pydantic-graph**: graphs render as Mermaid `stateDiagram-v2` through `graph.render()` — [pydantic-graph](https://pydantic.dev/docs/ai/graph/graph/).
- **LlamaIndex**: "Pre-execution graph validation ensures type safety and completeness" (fetched summary of) — [LlamaIndex Workflows](https://developers.llamaindex.ai/python/llamaagents/workflows/).

### Inferences
- The "contract" part of the idea (a planner agent and the user iterate on a DAG artifact, approve it, then execute it) does not exist as a built-in in any surveyed framework. The closest building blocks are MAF declarative YAML, ChatDev YAML and CrewAI YAML configs. All of them are written by a developer, not negotiated with a planner agent.
- Runtime HITL gates are commodity features. A new tool would not stand out by having them.

### Gaps
- I did not verify whether any framework stores a plan approval as a signed or versioned artifact.

## Q3. Durability: checkpointing, crash resume, long-running (hours–days), HPC/Slurm?

### Takeaway
Checkpoint and resume is mature in LangGraph (three durability modes; Postgres, SQLite and Mongo backends), MAF (a checkpoint per superstep; in-memory, file or Cosmos storage), ADK 2.x (deterministic node IDs, completed nodes skipped on resume), Mastra (storage snapshots, `restart()`, Inngest runner), LlamaIndex (Context snapshots, or a DBOS runtime), Pydantic AI (Temporal, DBOS, Prefect, Restate), CrewAI (`@persist`, SQLite) and Haystack (JSON snapshots at breakpoints and on error). The usual pattern is at-least-once re-execution of the in-flight step. None has HPC/Slurm awareness: no job-ID tracking or scheduler polling.

### Cited Findings
- **LangGraph durability modes**: `"exit"` persists only when the run exits (no crash recovery mid-run), `"async"` persists while the next step runs (small risk), and `"sync"` persists before the next step. Side effects should be wrapped in tasks. On resume, "the workflow's resumption will re-run the task" — [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution). Checkpointers include `InMemorySaver` (dev), `SqliteSaver` and `PostgresSaver`. Stores hold cross-thread long-term memory. `thread_id` should be under 255 characters for Postgres — [same page / persistence](https://docs.langchain.com/oss/python/langgraph/durable-execution).
- **LangSmith Agent Server** (formerly LangGraph Platform/Server): "a client sends a request to an API server, which creates a pending run in the durable task queue". Queue workers execute it. Postgres is the default store, with Mongo or custom checkpointers possible. Features include cron jobs and SSE streaming, with split API and worker deployment — [Agent Server](https://docs.langchain.com/langsmith/agent-server).
- **MAF**: checkpoints are created "at the end of each superstep". A checkpoint captures executor states, pending messages, pending requests/responses and shared state. Storage options are `InMemoryCheckpointStorage`, `FileCheckpointStorage` (local disk) and `CosmosCheckpointStorage`. Stated use cases include "Long-running workflows where you want to pause and resume execution at a later time". Rehydration requires the same topology and executor IDs. Python serialization uses pickle with a restricted unpickler. From Python 1.13.0, entry checkpoints "make the complete workflow run replayable" — [MAF checkpoints (updated 2026-09-21)](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints). MAF also has "Background responses" ("Continue, poll, and reconnect to long-running responses") and "Background agents" — [MAF capabilities](https://learn.microsoft.com/en-us/agent-framework/agents/).
- **ADK 2.x**: "Successful sub-nodes are automatically skipped when resuming the workflow, making complex logic durable and resumable by default." "ADK workflows use deterministic IDs for each scheduled node". `rerunOnResume` selects re-entry or handoff semantics — [ADK dynamic workflows](https://adk.dev/graphs/dynamic/).
- **LlamaIndex**: snapshots are made at step boundaries with `Context.to_dict()` / `from_dict()`. They capture in-flight events, the state store and fan-in buffers. "Completed steps don't re-run", while an in-flight step "is rewound and runs again from the top" (at-least-once). There is "no built-in automatic checkpointing" (you write a checkpoint loop yourself). Alternatively, "the DBOS runtime journals step transitions to a database" — [LlamaIndex durable workflows](https://developers.llamaindex.ai/python/llamaagents/workflows/durable_workflows).
- **Mastra**: "Suspension saves the current execution state as a snapshot" in the configured storage, which persists across restarts and deployments — [Mastra suspend/resume](https://mastra.ai/docs/workflows/suspend-and-resume). `restart()` / `resumeStream()` "recover interrupted runs from the last active step". Workflow runners such as Inngest are used for production — [Mastra workflows](https://mastra.ai/docs/workflows/overview).
- **Pydantic AI**: durable agents "preserve their progress across transient API failures and application errors or restarts, and handle long-running, asynchronous, and human-in-the-loop workflows". Four backends are supported: Temporal, DBOS, Prefect and Restate — [Pydantic AI durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/).
- **CrewAI**: `@persist` uses SQLite by default. `kickoff(inputs={"id": uuid})` resumes and `restore_from_state_id` forks — [CrewAI Flows](https://docs.crewai.com/en/concepts/flows).
- **Haystack**: snapshots are JSON (inputs, visit counts, intermediate outputs). File saving is off by default (`HAYSTACK_PIPELINE_SNAPSHOT_SAVE_ENABLED`). Pipelines automatically snapshot the last valid state before failures, and runs resume through `load_pipeline_snapshot()` then `pipeline.run()` — [Haystack breakpoints](https://docs.haystack.deepset.ai/docs/pipeline-breakpoints).
- **OpenHands SDK**: "event-sourced state model with an immutable event log that supports deterministic replay, selective persistence, and recovery to the last processed event" — [arXiv 2511.03690](https://arxiv.org/html/2511.03690v1).

### Inferences
- Durability in these frameworks assumes a node finishes within one process lifetime: checkpoints fall between steps. A node that is a 3-day Slurm job, or a multi-hour `claude -p` session, would be re-run from the top after a crash unless the node stores external handles (job IDs, session IDs) itself. This is the clearest gap the proposed tool would fill: node-level durable handles plus reconnecting to running external processes.
- The heavier durability options (Temporal, DBOS, Inngest, Agent Server with Postgres, Cosmos) need services that are often unavailable or awkward on HPC login nodes. MAF's `FileCheckpointStorage` and Haystack JSON snapshots are the file-based options closest to a "plan file + log" design.

### Gaps
- No primary source found for HPC/Slurm/PBS integration in any of these frameworks. As far as these searches show, none has it.
- Not verified: the maximum practical run length, or what happens when a single node runs for days, in any framework.

## Q4. Dynamic graph: can nodes or edges be added at runtime?

### Takeaway
Most frameworks compile a static topology and get dynamism through routing, fan-out or code-defined orchestration. Examples: LangGraph `Send` / `Command(goto)`, ADK 2.x dynamic workflows, LlamaIndex event emission, Mastra `.foreach` / branches, Agno Router/Loop, smolagents' fully agent-driven delegation. MAF explicitly requires the same topology to rehydrate checkpoints. No framework offers a first-class "edit the persisted DAG mid-run, then continue" operation.

### Cited Findings
- **LangGraph**: `Send` dispatches work "to a node with custom state", and returning a list of `Send` runs the target once per item in parallel (map-reduce). `Command` combines a state update with routing (`goto`) — [Galileo docs on Command/Send](https://v2docs.galileo.ai/sdk-api/third-party-integrations/langchain/command-and-send); [LangGraph nodes/edges](https://www.mintlify.com/langchain-ai/langgraph/concepts/nodes-edges). Time-travel forks with `update_state` create "a new checkpoint that branches from the specified point" — [LangGraph time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel).
- **ADK 2.x dynamic workflows**: "create workflows with simple decorators ... invoke workflow nodes as functions, and build complex routing logic". Execution paths are decided at runtime with normal loops and conditionals — [ADK dynamic workflows](https://adk.dev/graphs/dynamic/).
- **LlamaIndex**: "Branches are ordinary `if` statements that return different event types. Loops are steps that return an event handled by an earlier step." — [LlamaIndex Workflows](https://developers.llamaindex.ai/python/llamaagents/workflows/)
- **MAF**: "A rehydrated workflow must preserve the topology and executor identities of the workflow that created the checkpoint." — [MAF checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
- **Agno**: Parallel, Loop, Condition and Router containers are available — [Agno workflows](https://docs.agno.com/workflows/overview).
- **ChatDev 2.0**: the README describes DAG topologies scaling to "more than a thousand agents". The fetched summary did not describe runtime mutation — [ChatDev README](https://github.com/OpenBMB/ChatDev). ChatDev's NeurIPS 2025 paper "Multi-Agent Collaboration via Evolving Orchestration" (per the README) studies learned or dynamic orchestration.

### Inferences
- "Dynamic" in these frameworks means data-dependent paths through a fixed node set, or agent-driven delegation. Few support mutating a reviewed, persisted plan artifact under audit. The proposed tool's "DAG grows during execution, with each change recorded as an event" is not covered.

### Gaps
- Not verified whether the Mastra or CrewAI Flow topology can be changed between resume calls.

## Q5. Audit: event log, replay, time travel; are agent decisions recorded?

### Takeaway
Checkpoint history and time travel are strong in LangGraph and Mastra. MAF since Python 1.13 says it makes complete runs "replayable". OpenHands SDK is the only one built on an immutable, event-sourced log (per agent conversation, not per workflow). Most others rely on tracing and observability (OTel, LangSmith, Studio) instead of an append-only, human-readable event log.

### Cited Findings
- **LangGraph**: `get_state_history` lists past checkpoints, newest first. "Replay re-executes nodes—it doesn't just read from cache. LLM calls, API requests, and interrupts fire again and may return different results." — [LangGraph time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- **Mastra**: `run.timeTravel()` re-executes from any step using a stored snapshot or a provided context, and requires storage. Studio has a graph view, live status and time-travel debugging — [Mastra time travel](https://mastra.ai/docs/workflows/time-travel); [Mastra workflows](https://mastra.ai/docs/workflows/overview).
- **MAF**: entry checkpoints "make the complete workflow run replayable". Stated checkpoint use cases include "periodic state saving for auditing or compliance purposes" — [MAF checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints). Observability exports "traces, metrics, and logs" — [MAF capabilities](https://learn.microsoft.com/en-us/agent-framework/agents/).
- **OpenHands SDK**: an immutable event log with deterministic replay — [arXiv 2511.03690](https://arxiv.org/html/2511.03690v1).
- **CrewAI**: `restore_from_state_id` forks from a persisted state snapshot — [CrewAI Flows](https://docs.crewai.com/en/concepts/flows).
- **Haystack**: JSON snapshots can be inspected and edited before resuming — [Haystack breakpoints](https://docs.haystack.deepset.ai/docs/pipeline-breakpoints).

### Inferences
- An append-only log of workflow-level events that also records agent decisions (why a node was added, an approval, a cancel, a rerun) is not standard anywhere. LangGraph and MAF checkpoints are state snapshots, not semantic decision logs. OpenHands is event-sourced but only within one agent conversation. The proposed design overlaps with OpenHands' event-sourcing idea, applied one level up.

### Gaps
- I did not verify the on-disk format of Mastra snapshots or LangGraph checkpoints (both binary or serialized blobs in a DB), so they cannot be confirmed as human-auditable.

## Q6. Interface: SDK, CLI, server, MCP

### Takeaway
All are SDK-first: Python (most), TypeScript (Mastra, LangGraph.js), .NET and Go (MAF, ADK Go). CLIs exist mainly for scaffolding and serving (`langgraph dev/up`, `crewai run` / `crewai flow plot`, `llamactl`, `agent-canvas`). None is an "outer agent drives the workflow engine through a CLI + skill / MCP" tool by design.

### Cited Findings
- **CrewAI**: `crewai run` (replaces the deprecated `crewai flow kickoff`) and `crewai flow plot` — [CrewAI Flows](https://docs.crewai.com/en/concepts/flows).
- **LlamaIndex**: `llamactl` serves and deploys workflows, and llama_deploy runs workflows as services — [LlamaIndex Workflows](https://developers.llamaindex.ai/python/llamaagents/workflows/).
- **LangGraph**: Agent Server exposes REST + SSE for assistants, threads, runs and crons — [Agent Server](https://docs.langchain.com/langsmith/agent-server). The GitHub latest release tag is `cli==0.4.32.dev0` (2026-09-23), showing that the `langgraph-cli` package is maintained in the repo — [GitHub API](https://api.github.com/repos/langchain-ai/langgraph/releases/latest).
- **OpenHands**: `npm install -g @openhands/agent-canvas`, which has `--frontend-only` / `--backend-only` modes. There is also an agent server with "OpenAI-compatible Chat Completions and Responses endpoints" — [OpenHands repo](https://github.com/OpenHands/OpenHands); [OpenHands SDK docs](https://docs.openhands.dev/sdk).
- **MAF**: C#, Python and Go SDKs; A2A support; YAML declarative workflows — [MAF agent services](https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/agent-services/).
- **ChatDev 2.0**: web canvas plus Python SDK — [ChatDev README](https://github.com/OpenBMB/ChatDev).

### Gaps
- I did not verify which frameworks ship an MCP *server* that exposes workflow control (start, status, cancel). Many consume MCP tools, but that is a different thing.

## Q7. Maturity (as of 2026-10-02)

### Takeaway
LangGraph, CrewAI, MAF, ADK, Pydantic AI, Mastra, Agno, Haystack and DSPy all released in late Sep or early Oct 2026. AutoGen is in maintenance mode. MetaGPT's repository activity has slowed (last push Jan 2026).

### Cited Findings (GitHub REST API, fetched 2026-10-02, unless noted)
| Repo | Stars | License | Latest release (date) | Last push |
|---|---|---|---|---|
| langchain-ai/langgraph | 42,635 | MIT | `cli==0.4.32.dev0` (2026-09-23); PyPI `langgraph` 1.2.12 ([PyPI](https://pypi.org/pypi/langgraph/json)) | 2026-10-03 |
| crewAIInc/crewAI | 59,295 | MIT | 1.15.23 (2026-09-28) | 2026-10-03 |
| microsoft/autogen | 61,249 | CC-BY-4.0 (repo-level SPDX; code is MIT per README history — unverified) | python-v0.7.5 (2025-09-30) | 2026-04-15 |
| ag2ai/ag2 | 4,973 | Apache-2.0 | v1.1.1 (2026-09-29) | 2026-10-02 |
| microsoft/agent-framework | 13,913 | MIT | python-1.20.0 (2026-10-02) | 2026-10-03 |
| google/adk-python | 21,695 | Apache-2.0 | v2.11.0 (2026-10-02) | 2026-10-03 |
| run-llama/llama_index | 52,387 | MIT | v0.14.25 (2026-09-21) | 2026-10-01 |
| mastra-ai/mastra | 28,520 | NOASSERTION (mixed/custom; not verified) | @mastra/core@1.72.0 (2026-09-30) | 2026-10-03 |
| pydantic/pydantic-ai | 20,368 | MIT | v2.53.0 (2026-10-02) | 2026-10-03 |
| deepset-ai/haystack | 26,645 | Apache-2.0 | v3.3.0 (2026-10-01) | 2026-10-02 |
| huggingface/smolagents | 29,656 | Apache-2.0 | v1.26.0 (2026-05-29) | 2026-09-30 |
| stanfordnlp/dspy | 38,473 | MIT | 3.4.0 (2026-09-25) | 2026-10-02 |
| agno-agi/agno | 42,516 | Apache-2.0 | v3.1.1 (2026-10-02) | 2026-10-02 |
| OpenHands/OpenHands | ~89.8k ([GitHub page](https://github.com/OpenHands/OpenHands)) | MIT | 1.24.0 referenced in README | n/a |
| OpenHands/software-agent-sdk | 1,192 | MIT | v1.50.1 (2026-09-30) | 2026-10-03 |
| FoundationAgents/MetaGPT | 70,719 | MIT | not retrieved (rate-limited) | 2026-01-21 |
| OpenBMB/ChatDev | ~34.4k ([GitHub page](https://github.com/OpenBMB/ChatDev)) | Apache-2.0 | 2.0 released 2026-01-07 | n/a |

- **AutoGen**: "AutoGen is now in maintenance mode. It will not receive new features or enhancements and is community managed going forward. ... New users should start with Microsoft Agent Framework." — [AutoGen README](https://raw.githubusercontent.com/microsoft/autogen/main/README.md). Secondary sources date maintenance mode to Oct 2025 and MAF 1.0 GA to Apr 2 2026. AG2 is the community fork led by the original creators (Chi Wang, Qingyun Wu), and AG2 v1.0 broke compatibility with classic ConversableAgent — [agenticwire (secondary)](https://www.agenticwire.news/article/agent-frameworks-2026-autogen-ag2-guide); [atlan (secondary)](https://atlan.com/know/ai-agent/what-is-autogen/).
- **ADK 2.0**: graph-based workflow runtime and HITL as a built-in primitive — [ADK 2.0](https://adk.dev/2.0/); ADK Go 2.0 announced — [Google Developers Blog](https://developers.googleblog.com/announcing-adk-go-20/).
- **MetaGPT**: latest README news items are from Feb 2025 (MGX launch; SPO/AOT papers). The AFlow paper was an ICLR 2025 oral — [MetaGPT README](https://github.com/FoundationAgents/MetaGPT).
- **OpenHands SDK**: MLSys 2026 paper — [MLSys 2026](https://proceedings.mlsys.org/paper_files/paper/2026/hash/8ae9cf363ea625161f885b798c1f1f78-Abstract-Conference.html).
- **DSPy**: the repo is active (3.4.0, Sep 2026). It is a framework for programming and optimizing LM modules, not a durable workflow engine. I found no primary source describing DSPy checkpoint, resume or HITL workflow features in this pass — [GitHub API stanfordnlp/dspy](https://api.github.com/repos/stanfordnlp/dspy).

### Gaps
- The AutoGen license field from the API reads CC-BY-4.0 (docs license). The code license was not re-verified.
- The Mastra license shows NOASSERTION. Mastra may use a mixed license (open-source core plus enterprise directories), but this was not verified in 2026.
- MetaGPT and ChatDev latest-release tags were not retrieved because of the GitHub API rate limit.
- run-llama/workflows-py has moved (301), and its new location and stats were not retrieved.
- Governance (foundation versus single company): all are company- or lab-led (LangChain Inc., CrewAI Inc., Microsoft, Google, LlamaIndex Inc., Mastra, Pydantic Services, deepset, Hugging Face, Stanford, Agno, All Hands AI, DeepWisdom/FoundationAgents, OpenBMB/Tsinghua). AG2 is the only explicitly community-governed fork. This was not individually re-verified.

## Q8. Overall: how close does each framework come to the idea?

### Takeaway
No surveyed framework combines all of these: agent-per-node with external CLI harnesses, a plan negotiated with a planner agent and approved by the user, file-based durable state for HPC jobs that run for days, mid-run DAG mutation recorded in an append-only decision log, and an outer harness driving it through CLI/MCP. The closest are MAF (external coding-agent nodes, declarative YAML, file checkpoints, HITL, replayable runs), LangGraph (best-in-class durability, HITL, time travel, server) and OpenHands (event-sourced log, multi-harness via ACP). Each is missing the contract and HPC parts.

### Comparison table
Legend: ✓ built-in, ~ partial or DIY, ✗ absent or not found

| Framework | Node = autonomous agent? | External CLI agent node | Declarative/approvable plan | HITL gate | Durable resume | Long-running / HPC aware | Runtime DAG change | Event log / replay | Interface |
|---|---|---|---|---|---|---|---|---|---|
| LangGraph (+Agent Server) | ~ (function nodes; agents as subgraphs) | ~ (DIY; Server hosts Claude Agent SDK agents) | ✗ (code graph) | ✓ `interrupt()` | ✓ 3 durability modes, PG/SQLite | ~ / ✗ | ~ (`Send`, `Command`) | ✓ checkpoint history, time travel, fork | Py/JS SDK, server, CLI |
| CrewAI Flows | ~ (crews inside flow steps) | ✗ | ~ (YAML crews; auto `planning`, no approval) | ✓ `@human_feedback` | ✓ `@persist` SQLite | ✗ | ~ (router) | ~ (state fork) | Py SDK, CLI |
| AutoGen | conversational agents | ✗ | ✗ | ~ | ~ | ✗ | ~ (agent-driven) | ~ | maintenance mode |
| AG2 | conversational agents | ✗ | ✗ | ~ | not verified | ✗ | ~ | not verified | Py SDK |
| MS Agent Framework | ~ (executors; `AgentExecutor`) | ✓ GitHub Copilot CLI/SDK, Claude Agent SDK | ✓ YAML declarative workflows (1.0, Jul 2026) | ✓ request/response, `Question` | ✓ per-superstep, file/Cosmos | ~ / ✗ | ✗ (topology fixed for rehydrate) | ✓ "replayable" runs (Py ≥1.13) | .NET/Py/Go SDK |
| Google ADK 2.x | ~ (function, LLM agent, orchestrator nodes) | ~ (via A2A remote agents) | ~ (graph code) | ✓ `RequestInput` | ✓ completed nodes skipped | ✗ | ✓ dynamic workflows (code) | ~ | Py/Go/Java SDK |
| LlamaIndex Workflows | ~ (steps) | ✗ | ~ (pre-run validation) | ✓ | ~ (manual snapshots; DBOS runtime) | ✗ | ✓ event-driven | ~ | Py SDK, llamactl |
| Mastra | ~ (steps; agents as steps) | ✗ | ~ (TS code, Studio graph) | ✓ `suspend()` | ✓ snapshots, `restart()`, Inngest | ✗ | ~ (branch/foreach) | ✓ `timeTravel()` | TS SDK, Studio |
| Pydantic AI / pydantic-graph | ~ (BaseNode) | ✗ | ~ (Mermaid render) | ~ | ✓ via Temporal/DBOS/Prefect/Restate | ✗ | ~ | ~ | Py SDK |
| Haystack | ~ (components; Agent component) | ✗ | ~ (serializable pipelines) | ~ (breakpoints) | ~ JSON snapshots on breakpoint/error | ✗ | ✗ | ~ | Py SDK |
| smolagents | ✓ (manager + managed agents) | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (agent-driven) | ~ (memory) | Py SDK |
| DSPy | ✗ (LM modules) | ✗ | ✗ | ✗ | not found | ✗ | ✗ | ✗ | Py SDK |
| Agno | ~ (Agent/Team/function steps) | ✗ | ~ | ✓ step-level | ~ (db sessions) | ✗ | ~ (Router/Loop) | ~ | Py SDK, AgentOS |
| OpenHands SDK / Agent Canvas | ✓ (coding agent; delegation) | ✓ Canvas runs Claude Code, Codex, Gemini via ACP | ✗ | ✓ (confirmation) | ✓ event-sourced recovery | ~ (Docker/K8s, not Slurm) | ~ (delegation) | ✓ immutable event log, deterministic replay | Py SDK, agent server, CLI/canvas |
| MetaGPT | ✓ role agents | ✗ | ~ (fixed SOP) | ✗ | not verified | ✗ | ✗ | ~ | Py |
| ChatDev 2.0 | ✓ agent nodes (+python, human) | ✗ | ✓ YAML DAG + visual canvas | ✓ human node | not verified | ✗ | not verified | ~ (logs, artifacts) | Web UI, Py SDK |

### Inferences
- Unique gap 1: nodes that are long-lived external processes (headless CLI agents, Slurm jobs) with durable handles, so the orchestrator can reconnect, poll, cancel or rerun them across sessions. Existing frameworks checkpoint only between in-process steps.
- Unique gap 2: plan-as-contract (planner agent and human co-edit the DAG, approval is recorded) plus DAG edits mid-run recorded in an append-only decision log. MAF YAML and ChatDev YAML are the nearest artifacts, but they are not negotiated with an agent and not mutable under audit.
- Unique gap 3: being designed to be driven *by* an outer coding agent (Claude Code / Codex) through CLI+skill or MCP, rather than embedded in an application.
- Ideas worth borrowing: LangGraph's durability modes and thread/checkpoint model; MAF's superstep checkpoints and entry checkpoints for replay; ADK's deterministic node IDs that skip completed nodes on resume; LlamaIndex's explicit at-least-once semantics; OpenHands' event-sourced state; ACP as a multi-harness adapter (OpenHands Agent Canvas).

### Gaps
- Several "not verified" cells (AG2 durability, ChatDev resume, MetaGPT persistence) need direct doc checks.
- I did not look for third-party projects that already wrap `claude -p` / `codex exec` in a LangGraph or Prefect DAG for HPC; they would fall outside the frameworks surveyed here.
