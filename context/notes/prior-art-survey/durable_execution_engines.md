# Durable-execution / workflow-orchestration engines as prior art for an agent-per-node, durable, plan-approved research-workflow CLI

Scope: Temporal, Restate, Inngest+AgentKit, DBOS, Prefect (+ControlFlow/Marvin), Dagster, Hatchet, Trigger.dev, Cloudflare Workflows/Agents SDK, Vercel Workflow DevKit, AWS Step Functions + Bedrock AgentCore, Airflow 3, plus Snakemake / Nextflow. Research date 2026-10-02. GitHub stars and latest releases were read from the live github.com repo and release pages on 2026-10-02. The GitHub API was rate-limited, so the numbers come from the HTML pages.

## Q1. Persistence model and replay semantics: how do they handle non-deterministic LLM calls?

### Takeaway
Every engine converges on the same pattern: the orchestration code is deterministic, and every LLM call or tool call runs as a journaled "activity/step" whose output is recorded. On replay, recorded outputs are reused instead of calling the model again. This is exactly the "record agent decisions so replay reuses them" idea, but at the granularity of *individual model calls inside a code-defined agent loop*. None of them records whole-node results from an opaque, external coding-agent process (`claude -p`, `codex exec`). The exception is Nextflow-style task caching and the coding-agent-specific runtime Smithers.

### Cited Findings
**Temporal (event history + deterministic replay)**
- In the OpenAI Agents SDK integration, "Agent orchestration—the agent loop, tool selection, and handoffs—runs inside the Workflow, while model calls run as Activities". Model calls "retry durably and are not repeated during Workflow replay." — [Temporal docs: OpenAI Agents SDK integration](https://docs.temporal.io/develop/python/integrations/openai-agents)
- There are three tool classes. `activity_as_tool` runs in Activities (I/O). `@function_tool` runs in the Workflow (deterministic). Hosted tools run at the provider during the model Activity. "Model calls are always routed through Activities. Tools are not." MCP servers run as Activities through a durable proxy (`OpenAIAgentsPlugin.mcp_servers`). — [Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents)
- For long conversations, Temporal recommends Continue-as-New to "start a fresh Execution carrying the history forward" when history grows too large. — [Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents)
- Temporal also has an AI cookbook (durable agent with tools, OpenAI Agents SDK) and a Vercel AI SDK integration. — [Temporal AI cookbook](https://docs.temporal.io/ai-cookbook/durable-agent-with-tools); [Temporal blog: Vercel AI SDK](https://temporal.io/blog/building-durable-agents-with-temporal-and-ai-sdk-by-vercel)

**Restate (journal)**
- "Every step is recorded in a journal, so if the process crashes, the agent picks up exactly where it left off". "LLM calls are not repeated (saving cost and time)" and "Tool side effects are not duplicated." — [Restate docs: durable agents](https://docs.restate.dev/ai/patterns/durable-agents.md); [Restate tour: AI agents](https://docs.restate.dev/tour/ai-agents)
- Framework adapters: Vercel AI `durableCalls(ctx)` middleware, OpenAI Agents `DurableRunner`, Google ADK `RestatePlugin()`, Pydantic AI `RestateAgent`, LangChain `RestateMiddleware()`, plus a manual loop that wraps calls in `ctx.run()`. — [Restate docs](https://docs.restate.dev/ai/patterns/durable-agents.md)
- Virtual Objects are keyed, single-instance-per-key durable handlers with transactional state and queued concurrency, used for per-session agent memory. — [Restate tour](https://docs.restate.dev/tour/ai-agents)

**DBOS (database checkpoints)**
- "DBOS uses a database to durably store workflow and step state". It defaults to SQLite, and Postgres is used for production. Each LLM call or tool call is a step whose result is checkpointed. — [DBOS AI quickstart](https://docs.dbos.dev/ai/ai-quickstart)
- OpenAI Agents SDK: `DBOSRunner.run`/`run_sync` replace `Runner.run` inside a `@DBOS.workflow`. — [DBOS: OpenAI Agents](https://docs.dbos.dev/integrations/openai-agents)
- Pydantic AI + DBOS: "call `agent.run()` inside a `@DBOS.workflow`". Model requests and MCP communication are routed through DBOS steps, and an MCP server connects once per workflow. — [Pydantic AI docs: DBOS](https://pydantic.dev/docs/ai/capabilities/durable_execution/dbos/)
- Other native integrations: LlamaIndex, Google ADK, Vercel AI SDK. — [DBOS AI quickstart](https://docs.dbos.dev/ai/ai-quickstart)

**Inngest + AgentKit (step memoization)**
- When an AgentKit run is owned by an Inngest function, model calls go through `step.ai` so they "retry and cache model results durably". Side effects use `step.run`, and waits use `step.waitForEvent`. — [Inngest agents skill (inngest-skills, secondary)](https://www.skills.sh/inngest/inngest-skills/inngest-agents); [PromptLayer glossary (secondary)](https://www.promptlayer.com/glossary/inngest-agent-kit)

**Hatchet (durable event log)**
- "Every time a piece of a durable task completes, it creates a new checkpoint (an entry in a durable event log), from which we can replay without needing to re-execute the actual application logic." — [Hatchet docs: durable execution](https://docs.hatchet.run/home/durable-execution)
- In Hatchet's agent model, "every agent [is] a durable task". The task spawns child tasks, waits, decides, and spawns more. Progress is checkpointed so it can "resume on any worker without re-executing completed work." — [Hatchet AI agents guide (search snippet; page 404 when fetched)](https://docs.hatchet.run/guides/ai-agents)

**Vercel Workflow DevKit / Workflow SDK (event sourcing)**
- `"use workflow"` functions are deterministic orchestrators, and `"use step"` functions hold I/O and are retried. "The workflow code gets re-run multiple times during its lifecycle, each time using the event log to resume the workflow to the correct spot". `Math.random`/`Date` are fixed during replay. Steps default to 3 retries. — [Workflow SDK docs: workflows and steps](https://workflow-sdk.dev/docs/foundations/workflows-and-steps)
- `DurableAgent` is a durable agent class. — [Vercel workflow skill/docs mirror (secondary)](https://www.mintlify.com/vercel/workflow)

**Cloudflare Workflows**
- Each `step.do` is independently retriable, and state is persisted between steps. `step.sleep`/`sleepUntil`/`waitForEvent` are available. GA since April 2025. — [Cloudflare blog: Workflows GA](https://blog.cloudflare.com/workflows-ga-production-ready-durable-execution/)

**Airflow 3 Common AI provider**
- `apache-airflow-providers-common-ai` 0.1.0 (April 14, 2026) adds `@task.llm`, `@task.agent`, `@task.llm_branch`, `@task.llm_sql`, `@task.llm_file_analysis` and `@task.llm_schema_compare` (built on Pydantic AI). With `durable=True` it caches "each model response and tool result" to object storage, and "on retry, cached steps replay instantly: no repeated LLM calls, no repeated tool execution." — [Apache Airflow blog: Common AI provider](https://airflow.apache.org/blog/common-ai-provider/)

**Prefect / ControlFlow / Marvin**
- ControlFlow (task-centric agentic workflows on Prefect) was archived. Its engine merged into Marvin 3.0, which uses Pydantic AI instead of LangChain (`marvin.Task`, `marvin.Agent`, `marvin.run`). — [aitoolsatlas ControlFlow changelog (secondary)](https://aitoolsatlas.ai/tools/controlflow/changelog); the GitHub repo PrefectHQ/ControlFlow shows "archived by the owner on Mar 19, 2026" ([GitHub](https://github.com/PrefectHQ/ControlFlow))

**Nextflow nf-agent (task-level caching)**
- An agent is "a process-shaped primitive: it declares typed inputs and outputs, renders a prompt, calls a language model". "Each invocation runs as an ordinary Nextflow task", and "work directories, retries, parallelism, resume, and lineage all apply." It calls the model from the driver JVM over the OpenAI wire protocol through a langchain4j runner. It is not a CLI coding agent. — [Nextflow plugin registry: nf-agent](https://registry.nextflow.io/plugins/nf-agent)

**Smithers (coding-agent-specific; outside the assigned engine list, but the closest match)**
- "Every completed step is persisted to SQLite the moment it finishes". Its CLI has `rewind`, `fork` and `replay`. — [smithers-orchestrator README](https://cdn.jsdelivr.net/npm/smithers-orchestrator@0.32.0/README.md)

### Inferences
- The engines' replay model (deterministic orchestrator + recorded nondeterministic steps) maps cleanly onto a "plan file + append-only event log" design:
  - The **DAG/plan** plays the role of the deterministic workflow code.
  - Each **agent node's final output plus its decisions** plays the role of the recorded activity/step result.
- An agent-per-node CLI would journal at node granularity, not per model call. That is coarser than Temporal, Restate or DBOS, but it matches Nextflow's `-resume` caching.
- Replaying *inside* a crashed `claude -p` / `codex exec` session is not possible with these engines. The external harness owns its own loop, so the activity boundary is the whole subprocess. Partial in-node recovery would depend on the harness's own session resume (e.g. `--resume`), not on the engine.
- Temporal's sandbox-agent work (below) shows how to make a coding agent durable *inside* Temporal. It requires the agent loop to be an OpenAI Agents SDK SandboxAgent in Python, not an arbitrary CLI.

### Gaps
- Inngest's own AgentKit docs page was not fetched directly. The `step.ai` details come from Inngest's published skill text and a glossary. The AgentKit GitHub release cadence is stale: the last tagged release is agent-kit@0.13.2 on 2025-11-13 ([GitHub](https://github.com/inngest/agent-kit)).
- I found no first-party evidence of Prefect 3 itself shipping an agent-durability layer. Pydantic AI's docs page that I fetched documented only DBOS, and mentioned Temporal and Prefect only in passing.
- Dagster has no documented "LLM call as journaled step" feature. Its AI features (see Q5) are operational assistants, not agent execution.

## Q2. Long-running support, human waits, cancellation, retries, per-step rerun

### Takeaway
All the durable engines support sleeps or waits of hours to weeks without holding compute, plus human-in-the-loop signals or tokens and per-step retries. **DBOS is the richest on explicit per-step rerun**: fork-from-step and rewind, from both the CLI and the API. Smithers offers rewind/fork/replay for coding-agent runs. Temporal offers reset/fork-style operations and session forking for sandbox agents.

### Cited Findings
- **DBOS**:
  - Cancel: `dbos workflow cancel` or `DBOS.cancel_workflow`, which preempts "at the beginning of its next step".
  - Resume: `dbos workflow resume` resumes "from its last completed step".
  - Fork: `DBOS.fork_workflow` copies the "inputs and all its steps up to the selected step, then begins executing the new workflow from the selected step" under a new ID.
  - Rewind: `DBOS.rewind_workflow` keeps the same ID, "discards the workflow's recorded steps from the selected step onward" and re-enqueues the workflow.
  - Inspection: `dbos workflow list` / `dbos workflow steps`.
  - Source: [DBOS docs: workflow management](https://docs.dbos.dev/python/tutorials/workflow-management)
- **DBOS** claims support for agents that "run for hours, days, or weeks (potentially waiting for human responses)". — [DBOS AI quickstart](https://docs.dbos.dev/ai/ai-quickstart)
- **Temporal**:
  - The approval callback runs in Workflow context and can wait on Signals/Updates ("An agent action that should not proceed unattended can pause for a person"). — [Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents)
  - The sandbox demo (April 16, 2026) has an AgentWorkflow with a "durable idle loop" and a SessionManagerWorkflow (start/stop/fork/rename). Forking pauses the source workflow, snapshots it, and launches a new workflow "with identical conversation history but an independent lifecycle". Idle agents consume zero compute. — [Temporal blog: agentic sandboxes](https://temporal.io/blog/introducing-temporal-and-agentic-sandboxes-openai-agents-sdk)
  - Sandbox support is "pre-release and may change before general availability." — [Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents)
- **Trigger.dev v4** (GA): tasks "wait for minutes, hours, or days without consuming resources". Waitpoints are a primitive that blocks runs until conditions are met. One waitpoint can block many runs, and one run can be blocked by many waitpoints. Waitpoint tokens are used for human approval. — [Trigger.dev v4 GA](https://trigger.dev/launchweek/2/trigger-v4-ga)
- **Cloudflare Workflows**: `step.sleep`, `step.sleepUntil`, `step.waitForEvent`, and per-step retries. — [Cloudflare blog](https://blog.cloudflare.com/workflows-ga-production-ready-durable-execution/)
- **Cloudflare Agents SDK v0.3.7** (2026-02-03) added the `AgentWorkflow` class, intended for "long-running tasks (over 30 seconds), multi-step pipelines, and human approval flows". — [Cloudflare changelog](https://developers.cloudflare.com/changelog/post/2026-02-03-agents-workflows-integration)
- **Vercel Workflow SDK**: `sleep()` suspends "without consuming any resources". `createWebhook()` suspends "till the URL is resumed". — [Workflow SDK docs](https://workflow-sdk.dev/docs/foundations/workflows-and-steps)
- **Hatchet**: durable tasks either wait (sleep or event) or spawn children. They are used for "agentic workflows with human-in-the-loop steps". — [Hatchet docs](https://docs.hatchet.run/home/durable-execution)
- **Airflow 3.1+**:
  - HITL operators: `ApprovalOperator`, `HITLOperator`, `HITLBranchOperator`, `HITLEntryOperator`. — [Astronomer: HITL in Airflow](https://www.astronomer.io/docs/learn/airflow-3/airflow-human-in-the-loop)
  - The Common AI provider adds iterative human review of agent output (approve / reject / request changes, looped). — [Airflow blog](https://airflow.apache.org/blog/common-ai-provider/)
- **AWS**:
  - Step Functions added a Bedrock AgentCore integration (March 2026): "invoke AI agent runtimes with built-in retries, run multiple agents in parallel using Map states". — [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-sdk-integrations/)
  - Async patterns: Step Functions task-token callbacks, the direct integration, or Lambda durable functions that wait for the agent's callback "without continuously consuming compute". — [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-sdk-integrations/); [CloudThat (secondary)](https://www.cloudthat.com/resources/blog/optimizing-serverless-ai-workflows-with-amazon-bedrock-agentcore)
- **Smithers**: runs coding-agent work "for minutes or days with crash recovery, retries, human approvals". Approvals are first-class. — [Smithers README](https://cdn.jsdelivr.net/npm/smithers-orchestrator@0.32.0/README.md)

### Inferences
- DBOS's fork/rewind semantics are a direct template for "rerun node X and its downstream" in the proposed CLI: discard the event-log suffix from the node onward and re-enqueue.
- Approval-before-execution (plan approval) maps onto a HITL wait at the DAG root in every engine. None of them treats the *plan itself* as a negotiated artifact. They model approval only as an in-flight gate.

### Gaps
- I did not verify Temporal's exact "reset workflow to event ID" CLI semantics in this session. It is a well-known feature (`temporal workflow reset`) but was not re-fetched.
- I did not verify Restate cancellation/kill CLI semantics or awakeables (the fetched pages omitted them).

## Q3. Dynamic graphs (child workflows, dynamic fan-out, DAG growth during execution)

### Takeaway
Code-first durable engines (Temporal, Restate, DBOS, Hatchet, Inngest, Trigger.dev, Vercel, Cloudflare) are dynamic by construction: the "graph" is whatever the code spawns at runtime. Static-DAG engines (Airflow, Dagster, Snakemake, Nextflow) support dynamic mapping or branching but not arbitrary mid-run graph surgery. No engine exposes the DAG as an *editable data artifact* that a human or a planning agent amends while it runs.

### Cited Findings
- **Hatchet**: "A durable task builds the workflow at runtime through child spawning". The agent loop "re-spawns itself until done", and the parent is evicted while its children execute, which frees its worker slot. — [Hatchet AI agents guide (search snippet)](https://docs.hatchet.run/guides/ai-agents)
- **Trigger.dev v4** supports "agentic loops, human-in-the-loop flows, and parallel fan-out patterns". — [Trigger.dev v4 GA](https://trigger.dev/launchweek/2/trigger-v4-ga)
- **AWS Step Functions**: Map states run multiple AgentCore agents in parallel. — [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-sdk-integrations/)
- **DBOS**: durable queues for "parallel tool calls or tasks across many servers". — [DBOS (search snippet of docs)](https://docs.dbos.dev/integrations/pydantic-ai)
- **Airflow Common AI** offers `@task.llm_branch`, an LLM-chosen branch inside a static DAG. — [Airflow blog](https://airflow.apache.org/blog/common-ai-provider/)
- **Smithers**: "A workflow is a JSX tree of tasks, each with a Zod-validated output". `<Branch>` and `<Loop>` components provide dynamic flow. — [Smithers README](https://cdn.jsdelivr.net/npm/smithers-orchestrator@0.32.0/README.md)

### Inferences
- The proposed "grow/modify DAG during execution" maps best onto two models:
  - Hatchet-style "orchestrator task spawns children".
  - Temporal-style child workflows plus signals that append nodes.

  In both, the change must itself be recorded as an event, so that replay re-applies the same graph mutation deterministically.
- Static-DAG tools would need a re-plan-and-resume cycle, as with Snakemake/Nextflow re-runs using caching.

### Gaps
- I did not fetch Temporal child-workflow docs or Restate sub-agent docs in this session. Both features are well known but are not cited here.

## Q4. Infrastructure weight; file-only or single-binary; HPC/Slurm fit

### Takeaway
The lightest options are:
- **DBOS**: a library, SQLite by default, no separate server.
- **Smithers**: SQLite.
- **Vercel Workflow Local World**: a filesystem directory.
- **Temporal dev server**: one binary, SQLite file.
- **Restate**: one binary.
- **Inngest**: one binary, SQLite.

Everything else needs a server and a DB (Hatchet/Postgres, Prefect/Dagster/Airflow servers) or is cloud-only (Cloudflare, AWS, Vercel prod). Only Snakemake and Nextflow are natively HPC/Slurm-shaped. Of those, only Nextflow has an official (API-based, not coding-agent-based) LLM-agent node primitive.

### Cited Findings
- **Temporal**: the CLI bundles a dev server and Web UI as "a single process with zero runtime dependencies". `temporal server start-dev --db-filename <file>` persists to SQLite, and it is in-memory if the flag is omitted. Ports: localhost:7233 (server), :8233 (UI). — [Temporal docs: run a development server](https://docs.temporal.io/develop/run-a-development-server)
- **Restate**: "The Restate Server is available as a self-contained binary under BSL-license for self-hosting" (brew or `npx @restatedev/restate-server`). The UI is at localhost:9070. — [Restate docs: get restate](https://docs.restate.dev/get-restate); [Restate tour](https://docs.restate.dev/tour/ai-agents)
- **Inngest**: the self-hosted server "runs from the CLI as a single binary, starts with SQLite and embedded Redis by default, and can move to Postgres". The default DB is `./.inngest/main.db`, and the server runs via `inngest start` on port 8288. — [Inngest docs: self-hosting](https://www.inngest.com/docs/self-hosting)
- **DBOS**: a library that defaults to SQLite, with Postgres for production. It adds durability "without an external orchestrator". — [DBOS AI quickstart](https://docs.dbos.dev/ai/ai-quickstart); [Pydantic AI + DBOS article](https://pydantic.dev/articles/pydantic-ai-dbos)
- **Hatchet**: "uses Postgres as a durability layer for both the task runtime and the observability system". Local runs use the Hatchet CLI + Docker. — [Hatchet docs (search snippet)](https://docs.hatchet.run/guides/ai-agents)
- **Vercel Workflow SDK** "Worlds" are Local, Vercel, Postgres (community) and others. "The Local World stores workflow data in a `.workflow-data/` directory and processes steps synchronously for development and testing." Inspection: `npx workflow inspect runs [--backend @workflow/world-postgres]`. — [Workflow SDK docs: deploying](https://workflow-sdk.dev/docs/deploying)
- **Cloudflare Workflows** is serverless on Workers (Cloudflare-hosted only). — [Cloudflare blog](https://blog.cloudflare.com/workflows-ga-production-ready-durable-execution/)
- **Smithers** persists to SQLite and is run via `bunx smithers-orchestrator`. — [Smithers README](https://cdn.jsdelivr.net/npm/smithers-orchestrator@0.32.0/README.md)
- **Nextflow nf-agent** requires Nextflow >=26.08.0-edge and is Apache-2.0. Its docs say nothing about Slurm or HPC. — [nf-agent registry](https://registry.nextflow.io/plugins/nf-agent)
- **Snakemake** has a SLURM executor plugin ecosystem (e.g. `snakemake-executor-plugin-slurm-jobstep`) on a stable executor-plugin interface. — [snakemake-interface-executor-plugins (PyPI)](https://pypi.org/project/snakemake-interface-executor-plugins/9.3.1); [slurm-jobstep mirror](https://www.gitlink.org.cn/NSCCN/snakemake-executor-plugin-slurm-jobstep)
- **Snakemake and LLM agents**: published work has agents *write and drive* Snakefiles (view Snakefile, roll back, execute wrappers or rules) rather than placing agents inside rules. — [arXiv 2507.20122: From Prompt to Pipeline](https://arxiv.org/html/2507.20122v2)

### Inferences
- For HPC/Slurm, none of the durable engines (Temporal, Restate, DBOS, etc.) has a Slurm executor. Running on a login node or a long-lived service node would be required, and an hours-to-days Slurm job would be an activity that polls `squeue`/`sacct` or waits for a callback. DBOS (library + SQLite file) or Temporal dev-server (single binary + SQLite file) are the most HPC-login-node-friendly, but long-lived daemons on login nodes are often disallowed by HPC centers.
- A file-only plan + append-only JSONL event log (the proposed design) is lighter than all of these. It is closest in spirit to Vercel's Local World (`.workflow-data/`) and Smithers (SQLite).

### Gaps
- I did not verify Nextflow's own Slurm executor docs in this session. It is well known, but the citation was not fetched. I also did not check whether nf-agent tasks can be dispatched to Slurm, since the model call happens in the driver JVM.
- I found no published example of an LLM coding agent (`claude -p`/`codex exec`) run as a Snakemake rule or Nextflow process with Slurm. Searches returned nothing.

## Q5. Interface for an external (outer-harness) agent: CLI, MCP, SDK, skills

### Takeaway
Most engines now ship agent-facing surfaces: MCP servers and/or "skills" for Claude Code and Codex. But these let an outer agent *author or monitor* workflows written in code. They do not let it hand over a declarative plan for agent nodes to execute. Smithers is the exception: an outer coding agent drives it through a CLI or an optional MCP server, and its nodes are coding-agent harnesses.

### Cited Findings
- **DBOS** provides an MCP server that lets agents "observe and monitor your workflows". The CLI is `dbos workflow list/steps/cancel/resume`. — [DBOS AI quickstart](https://docs.dbos.dev/ai/ai-quickstart); [DBOS workflow management](https://docs.dbos.dev/python/tutorials/workflow-management)
- **Dagster**:
  - An official Dagster+ MCP server is a "hosted endpoint that gives an AI client structured access to a Dagster Plus organization". — [Cursor marketplace: Dagster](https://cursor.com/marketplace/dagster)
  - Compass powers the "Dagster+ AI agent" (diagnosis via UI, Slack or Teams). — [Dagster Compass](https://compass.dagster.io/dagster-compass)
  - The `dg` CLI and "auto implement" (labs) cover AI-driven pipeline authoring. — [Dagster docs: auto-implement](https://docs.dagster.io/guides/labs/dagster-ai/auto-implement)
- **Temporal sandbox demo**: the TUI reference client interacts through signals and queries. — [Temporal blog](https://temporal.io/blog/introducing-temporal-and-agentic-sandboxes-openai-agents-sdk)
- **Restate**: UI at localhost:9070 with "step-by-step execution trace of your agent". — [Restate tour](https://docs.restate.dev/tour/ai-agents)
- **Vercel Workflow**: `npx workflow inspect runs` CLI. — [Workflow SDK docs](https://workflow-sdk.dev/docs/deploying)
- Vendor-published agent skills exist for Inngest (inngest-skills/inngest-agents), DBOS (dbos-inc/agent-skills), Trigger.dev (trigger-agents), Vercel (vercel-plugin/workflow) and Astronomer (airflow-hitl). — [skills.sh: inngest-agents](https://www.skills.sh/inngest/inngest-skills/inngest-agents); [claudemarketplaces: dbos-python](https://claudemarketplaces.com/skills/dbos-inc/agent-skills/dbos-python); [mcpservers.org: trigger-agents](https://mcpservers.org/de/agent-skills/triggerdotdev/trigger-agents); [mcpservers.org: vercel workflow](https://mcpservers.org/agent-skills/vercel/vercel-plugin/workflow-2); [claudeskills.info: airflow-hitl](https://claudeskills.info/skills/astronomer/agents/airflow-hitl/)
- **Smithers**:
  - Adapters: Claude Code, Codex, Cursor, Pi, Antigravity, Gemini, and any AI SDK model ("Swap the harness without rewriting the workflow").
  - Interface: CLI `bunx smithers-orchestrator` with `rewind`/`fork`/`replay`, plus an optional MCP server "to invoke Smithers autonomously".
  - License: MIT.
  - Source: [Smithers README](https://cdn.jsdelivr.net/npm/smithers-orchestrator@0.32.0/README.md)

### Inferences
- The "outer harness drives the CLI through a skill or MCP" pattern is already mainstream for *operating* engines. The proposed tool's novelty is not the interface but the content: declarative agent-node DAGs plus plan approval.

### Gaps
- I did not confirm an official Temporal, Restate, Hatchet or Inngest MCP server for workflow control in this session.

## Q6. Maturity as of 2026-10-02 (stars, latest release, license, pricing)

### Takeaway
All the major engines are actively released, most within the last 3 weeks. Agent-specific layers are younger. Temporal's sandbox support is pre-release, AgentKit has had no release in more than 10 months, nf-agent is at 0.1.1 and Smithers at 0.35.0. ControlFlow is archived.

### Cited Findings (GitHub star counts and latest release read from github.com on 2026-10-02)

| Project | Stars | Latest release (date) | License / notes |
|---|---|---|---|
| [temporalio/temporal](https://github.com/temporalio/temporal) | 23,430 | v1.32.0 (2026-09-11) | License not re-verified (believed MIT) |
| [restatedev/restate](https://github.com/restatedev/restate) | 4,509 | v1.7.13 (2026-10-01) | Server BSL ([docs](https://docs.restate.dev/get-restate)) |
| [inngest/inngest](https://github.com/inngest/inngest) | 5,907 | v1.45.1 (2026-09-17) | Server/CLI SSPL, each release relicensed to Apache-2.0 on a delay. SDKs Apache-2.0. Self-hosting free with "no execution metering" ([docs](https://www.inngest.com/docs/self-hosting)) |
| [inngest/agent-kit](https://github.com/inngest/agent-kit) | 940 | agent-kit@0.13.2 (2025-11-13) | Stale releases |
| [dbos-inc/dbos-transact-py](https://github.com/dbos-inc/dbos-transact-py) | 1,604 | 3.2.0 (2026-09-29) | License not re-verified (believed MIT) |
| [PrefectHQ/prefect](https://github.com/PrefectHQ/prefect) | 23,963 | 3.8.7 (2026-09-26) | |
| [PrefectHQ/marvin](https://github.com/PrefectHQ/marvin) | 6,202 | v3.2.7 (2026-03-04) | |
| [PrefectHQ/ControlFlow](https://github.com/PrefectHQ/ControlFlow) | 1,387 | — | Archived 2026-03-19 |
| [dagster-io/dagster](https://github.com/dagster-io/dagster) | 16,232 | 1.13.25 (2026-10-01) | |
| [hatchet-dev/hatchet](https://github.com/hatchet-dev/hatchet) | 8,050 | v0.107.0 (2026-09-15) | |
| [triggerdotdev/trigger.dev](https://github.com/triggerdotdev/trigger.dev) | 16,457 | v4.7.2 (2026-10-02) | |
| [cloudflare/agents](https://github.com/cloudflare/agents) | 5,736 | agents@0.26.0 (2026-10-02) | |
| [vercel/workflow](https://github.com/vercel/workflow) | 2,443 | workflow@5.0.1 (2026-10-01) | |
| [apache/airflow](https://github.com/apache/airflow) | 47,035 | 3.3.2 (2026-09-17) | Common AI provider 0.1.0 ([blog](https://airflow.apache.org/blog/common-ai-provider/)) |
| [snakemake/snakemake](https://github.com/snakemake/snakemake) | 2,881 | v9.27.0 (2026-09-11) | |
| [nextflow-io/nextflow](https://github.com/nextflow-io/nextflow) | 3,497 | v26.04.6 (2026-07-09, latest stable tag) | nf-agent 0.1.1 released 2026-09-22, Apache-2.0 ([registry](https://registry.nextflow.io/plugins/nf-agent)) |
| [smithersai/smithers](https://github.com/smithersai/smithers) | 428 | v0.35.0 (2026-08-17) | MIT (README) |

- Temporal × OpenAI Agents SDK is GA, but streaming and sandbox features are experimental or pre-release. — [Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents)
- Cloudflare Workflows has been GA since April 2025. — [Cloudflare blog](https://blog.cloudflare.com/workflows-ga-production-ready-durable-execution/)
- Trigger.dev v4 is GA. — [Trigger.dev](https://trigger.dev/launchweek/2/trigger-v4-ga)
- AWS Step Functions' AgentCore integration is GA in all Step Functions regions (March 2026). — [AWS](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-sdk-integrations/)

### Inferences
- The "agent-per-node, coding-harness" niche is small and young: Smithers has ~430 stars, and nf-agent is at 0.1.x and API-based. General engines are mature but code-first.

### Gaps
- Licenses for Temporal, DBOS, Prefect, Dagster, Hatchet, Trigger.dev, Vercel Workflow, Cloudflare Agents, Snakemake and Nextflow were not re-verified this session (training knowledge: mostly MIT or Apache-2.0).
- I did not collect current cloud pricing (Temporal Cloud actions, Inngest/Trigger.dev/Hatchet/Restate Cloud tiers).
- Star counts are point-in-time from HTML scraping.

## Q7. Does any engine already package "agent-per-node, durable, replayable, plan-approved" in a lightweight form?

### Takeaway
Not completely. The general durable engines provide the replay/journal substrate and agent-SDK adapters, but nodes are code, and agents are in-process SDK loops rather than external coding-agent CLIs. Two close partial matches exist:
- **Smithers** (MIT, SQLite, TSX workflows): its nodes are Claude Code, Codex, Pi or Cursor runs, with approvals, rewind/fork/replay, and a CLI/MCP. It is the nearest prior art.
- **Nextflow nf-agent**: LLM agent as a Nextflow task with resume and lineage, but API-based, not a coding harness.

Neither one documents a negotiated, approved plan file as the contract, HPC/Slurm-aware long jobs, or DAG mutation as an audited event.

### Cited Findings
- Smithers: "Tell your coding agent to do real, multi-step work, then Smithers runs it for minutes or days with crash recovery, retries, human approvals, and full observability". Its harnesses are Claude Code, Codex, Cursor, Pi, Antigravity and Gemini. Persistence is SQLite with `rewind`/`fork`/`replay`. — [Smithers README](https://cdn.jsdelivr.net/npm/smithers-orchestrator@0.32.0/README.md)
- nf-agent: agents run as ordinary Nextflow tasks with "retries, parallelism, resume, and lineage". It uses OpenAI-compatible API calls from the driver JVM. — [nf-agent registry](https://registry.nextflow.io/plugins/nf-agent)
- Temporal + OpenAI SandboxAgent runs a *coding agent* durably, with fork-to-another-sandbox-backend. It is Python SDK-bound and pre-release. — [Temporal blog](https://temporal.io/blog/introducing-temporal-and-agentic-sandboxes-openai-agents-sdk); [Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents)
- Airflow `@task.agent` with `durable=True` and HITL review provides an agent per node in a DAG, with cached model and tool results. It is Pydantic AI-based, with no coding-agent support documented. — [Airflow blog](https://airflow.apache.org/blog/common-ai-provider/)
- Hatchet frames "every agent [as] a durable task" that spawns children dynamically. — [Hatchet guide (search snippet)](https://docs.hatchet.run/guides/ai-agents)
- Another Claude Code orchestration plugin (orkestr) chains `claude -p` agents with declarative syntax. Its durability was not verified. — [orkestr README](https://cdn.jsdelivr.net/gh/Sixallfaces/orkestr@main/README.md)

### Inferences
- Gaps the proposed tool could fill relative to this prior art:
  1. The plan/DAG as a first-class, human-approved, versioned artifact separate from code. Engines model approval only as runtime gates.
  2. Node-level journaling of *external* coding-agent sessions (transcript, session-id for harness resume, outputs, decisions) instead of per-model-call journaling.
  3. A Slurm-aware long-job node type (submit → poll/callback → resume across sessions).
  4. Graph mutations recorded as events for audit and deterministic replay.
  5. Zero-daemon, file-only state (plan file + JSONL log) suitable for HPC home or scratch filesystems.
- Design borrowings worth citing:
  - DBOS fork/rewind-from-step semantics, for per-node rerun.
  - Temporal's workflow/activity split, with replay never re-calling recorded nondeterministic results.
  - Hatchet's parent-eviction during child execution, so the orchestrator holds no process while agents or Slurm jobs run.
  - Vercel Local World's directory-based state.
  - Airflow's `durable=True` object-store caching of model and tool results.

### Gaps
- Smithers' internal event model (journal vs snapshot), plan-approval semantics, and any HPC support were not examined beyond its README. A deeper read of its docs and repo is recommended, since it is the closest competitor. It may be covered by another researcher.
- I found no engine documentation describing Slurm or HPC execution of agent nodes.
