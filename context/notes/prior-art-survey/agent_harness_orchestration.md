# Orchestration features in agent harnesses, vendor SDKs, and third-party coding-agent runners: prior art for an "every node is an agent" durable research-DAG CLI

Research date: 2026-10-02. Star counts are snapshots from GitHub pages fetched on that date through a summarizing fetcher, so treat them as approximate. The GitHub REST API was rate-limited from this host, which means release tags and dates could not be pulled for most repos (see Gaps).

Reference idea (the "target"): a lightweight agent-first workflow CLI. The workflow is a DAG in which every node is run by a headless coding-agent harness (claude -p, codex exec, pi, OpenHands). The user and a planning agent agree on the DAG as a contract before it runs. Runs are durable across sessions and can last hours to days on HPC/Slurm. The user can monitor them, and cancel, resume, or rerun any node. The DAG can change while it runs. State is kept as a plan file plus an append-only event log that records agent decisions. An outer harness drives it through a CLI plus skill, or through MCP.

## Q1. Is every node/step an autonomous agent (vs a fixed function/tool call)?

### Takeaway
Every Anthropic-native mechanism (subagents, agent teams, dynamic workflows, agent view) makes every worker a full Claude agent, but only Claude agents. In Claude Code workflows the orchestration between agents is plain JavaScript and not an agent. OpenAI's Agents SDK nodes are LLM agents inside one process, not coding-agent harnesses, and Agent Builder mixed agent nodes with fixed nodes. Several small third-party YAML-DAG engines (orc, yak, Archon) come closest to "node = headless coding-agent invocation". They mix agent nodes with shell, transform, and gate nodes, and some let each node pick claude or codex.

### Cited Findings
**Claude Code dynamic workflows**
- A dynamic workflow is "a JavaScript script that orchestrates many subagents at once. Claude writes the script for the task you describe, and a runtime executes it in the background." "Who decides what runs next: The script." — [code.claude.com/docs/en/workflows](https://code.claude.com/docs/en/workflows)
- Primitives: `agent(prompt, opts)` spawns one subagent and can return schema-validated JSON. `parallel()` is a barrier. `pipeline(items, ...stages)` streams items with no barrier. `phase()` and `log()` drive progress display. `workflow()` calls a nested workflow, one level deep. `budget` exposes the token budget. — [workflows docs](https://code.claude.com/docs/en/workflows); bundled `/workflow-authoring` skill reference text in Claude Code (local install, read 2026-10-02); [alexop.dev write-up](https://alexop.dev/posts/claude-code-workflows-deterministic-orchestration)
- The script itself has "No direct filesystem or shell access … Agents read, write, and run commands. The script coordinates the agents." `import()` is disallowed. — [workflows docs](https://code.claude.com/docs/en/workflows)
- Saved scripts live in `.claude/workflows/` (project) or `~/.claude/workflows/` (personal), and plugins can ship them in a `workflows/` directory. The docs show `.js` files. I found no official reference to a `*.workflow.mjs` naming convention. — [workflows docs](https://code.claude.com/docs/en/workflows)
- "In every approach the workers are Claude sessions. To involve a different tool, expose it to Claude as an MCP server." — [code.claude.com/docs/en/agents](https://code.claude.com/docs/en/agents)

**Claude Code agent teams**
- Teammates are "separate Claude Code instances", coordinated by a lead through a shared task list and a mailbox. — [agent-teams docs](https://code.claude.com/docs/en/agent-teams)

**OpenAI Agents SDK**
- The primitives are Agents (LLM plus instructions and tools), Handoffs, and Guardrails. It also has sandbox agents, sessions, tracing, human-in-the-loop, and "RunState serialization for resumable execution". — [openai.github.io/openai-agents-python](https://openai.github.io/openai-agents-python/)

**OpenAI Agent Builder (AgentKit)**
- It is a visual canvas with nodes such as Agent, File search, Guardrails, MCP, User approval, and If/else, a mix of agent and fixed nodes. — [openai.com/index/introducing-agentkit](https://openai.com/index/introducing-agentkit/) (via search summary)

**Third-party runners**
- orc: in a YAML workflow each node "independently specifies its provider and model" (claude, codex, or grok), or runs a shell script. "Every node has its own context and knows nothing about what came before it." — [github.com/tjdals12/orc](https://github.com/tjdals12/orc)
- yak: step types are `command`, `transform`, `agent`, `loop`, `map`, and `gate`. Its agent adapter is `claude-code`, built on the Claude Agent SDK. — [github.com/lchase/yak](https://github.com/lchase/yak)
- Archon: YAML DAG nodes are `prompt` (AI), `bash`, `loop`, or approval gates. Prompt nodes run through the Claude Agent SDK and a local Claude Code binary, with Codex and Pi configurable as fallback providers. — [github.com/coleam00/Archon](https://github.com/coleam00/Archon)
- baya-cli "turns a plain-text task list into an LLM-planned DAG and runs it across coding-agent CLIs like codex and claude". This comes from a search-result snippet only and was not verified. — search result surfaced alongside [augmentcode list](https://www.augmentcode.com/tools/open-source-agent-orchestrators)
- Gas Town / Gas City: worker "polecats" are coding-agent sessions (presets include Claude, Codex, Gemini, Cursor, and Copilot). Gas City "run[s] a formula as a graph across many agents … the orchestrator executes control beads; agents execute work beads." — [github.com/steveyegge/gastown](https://github.com/steveyegge/gastown); [docs.gascity.com](https://docs.gascity.com/getting-started/coming-from-gastown)
- Claude Squad, ccmanager, Conductor, Crystal, Vibe Kanban, and Sculptor each run one coding-agent session per task or workspace, usually in a git worktree. None of them is a DAG engine. — [claude-squad](https://github.com/smtg-ai/claude-squad); [ccmanager](https://github.com/kbwo/ccmanager); [conductor.build](https://www.conductor.build); [crystal](https://github.com/stravu/crystal); [vibe-kanban](https://github.com/BloopAI/vibe-kanban); [sculptor](https://github.com/imbue-ai/sculptor)
- Ruflo (formerly claude-flow) describes "swarm" topologies (hierarchical, mesh, adaptive, with consensus protocols) and "100+ specialized agents" exposed over MCP, not a user-authored DAG. — [github.com/ruvnet/claude-flow](https://github.com/ruvnet/claude-flow)

### Inferences
- Claude Code workflows are the closest vendor-native match to "every node is an agent". The difference is that nodes are always Claude subagents in the same process tree, not interchangeable external harnesses (codex exec, OpenHands, pi).
- orc and Archon are the closest structural match to "YAML DAG where nodes pick a harness". They also allow non-agent nodes (bash, transform), which the target idea deliberately excludes.

### Gaps
- I could not confirm whether claude-flow/Ruflo's hive-mind spawns separate `claude -p` processes per agent or runs agents in-process. The README summary was marketing-heavy.
- baya-cli is unverified (repo not fetched).

## Q2. Is there a plan-as-contract the user reviews/approves before execution?

### Takeaway
Claude Code workflows have a real pre-run approval step: the user sees the planned phases and can read or edit the raw script. Approval is per run, though, and no mid-run sign-off is possible. Agent teams auto-approve teammate plans. orc, yak, and Archon offer human gate or approval nodes inside the DAG. None of the surveyed tools frames the DAG itself as an iteratively negotiated, versioned contract between user and planner.

### Cited Findings
- Workflows: "In the CLI, the per-run prompt shows the planned phases" with options "Yes, run it", "Yes, and don't ask again", "View raw script", and "No". "Ctrl+G opens the script in your editor. Tab lets you adjust the prompt before the run starts." — [workflows docs](https://code.claude.com/docs/en/workflows)
- Workflows: in `claude -p` and the Agent SDK "Claude Code never shows this prompt". Approval then comes from permission rules (`Workflow`, `Workflow(<name>)`), auto mode, hooks, or `--permission-prompt-tool` / `canUseTool`. — [workflows docs](https://code.claude.com/docs/en/workflows)
- Workflows: "No mid-run user input … For sign-off between stages, run each stage as its own workflow." — [workflows docs](https://code.claude.com/docs/en/workflows)
- Agent teams: "When a teammate finishes planning, it sends a plan approval request to the lead. Claude Code approves the plan in the lead's session as soon as the request arrives, without the lead reviewing it." — [agent-teams docs](https://code.claude.com/docs/en/agent-teams)
- orc: "Approval nodes pause execution for human sign-off with customizable `on_reject` handlers"; `orc workflow approve`. — [orc](https://github.com/tjdals12/orc)
- yak: "a `gate` or an exhausted budget suspends the run rather than failing it". Gate steps are "human decision points with schema validation". — [yak](https://github.com/lchase/yak)
- Archon: "Interactive gates pausing execution awaiting human approve/reject decisions." — [Archon](https://github.com/coleam00/Archon)
- Gas Town: approval is at merge time. The "Refinery batches merge requests, runs verification gates, and merges to main using a Bors-style bisecting queue." — [gastown](https://github.com/steveyegge/gastown)
- Agent Builder had a "User approval" node type. — [AgentKit announcement](https://openai.com/index/introducing-agentkit/) (via search summary)

### Inferences
- The Claude Code workflow approval dialog is the nearest vendor analog to "approve the DAG before execution". But the reviewed artifact is generated JS code plus phase titles, not a declarative graph with per-node specs. Because control flow is code, the full node set is not knowable before the run.

### Gaps
- None of the sources describe a multi-round plan negotiation loop (user ↔ planner agent) that produces a versioned plan file as a first-class feature.

## Q3. Durability: does a run survive process/session death? Long-running (hours–days)? HPC/Slurm awareness?

### Takeaway
Claude Code workflows are resumable only within the same session, or a session resumed with `claude --resume`, through a journal of cached `agent()` results. They are not a daemon-backed durable engine, and stopping the process stops the run unless the session is moved to the background supervisor. Agent view's supervisor survives terminal close and sleep but not machine shutdown. The only true durable-execution story among vendors is Temporal's integration with the OpenAI Agents SDK, at the LLM-call level. orc, yak, Archon, and Gas City persist run state to disk or a database and resume from unfinished or failed nodes. I found no Slurm/HPC awareness in any surveyed project.

### Cited Findings
**Claude Code workflows**
- Resume works by replay. Completed agents return saved results. "The first agent whose prompt differs … runs again, and so does every agent after it". A failed agent causes it and "every agent that started after it" to rerun. Stopping a single agent counts as failing. — [workflows docs](https://code.claude.com/docs/en/workflows)
- "You can resume a run within the same Claude Code session." On exit, "Move to background and exit" carries the run over. Otherwise "the run stops with the session". Saved results stay under `~/.claude/projects/`, so a `claude --resume`d session can replay them. "In a session you start fresh, Claude has no earlier run to relaunch." — [workflows docs](https://code.claude.com/docs/en/workflows)
- The relaunch API is `Workflow({scriptPath, resumeFromRunId})`, with "the longest unchanged prefix of agent() calls" served from cache. The run directory holds `journal.jsonl`, which "records each agent's actual return value". `Date.now()`/`Math.random()` throw because "they would break resume". — bundled `/workflow-authoring` skill (local Claude Code install); also [workflows docs](https://code.claude.com/docs/en/workflows)
- Cloud sessions save run results with conversation history, which "survives when the session's VM is reclaimed". — [workflows docs](https://code.claude.com/docs/en/workflows)
- On usage limits, the run pauses and auto-continues if the limit resets within 24 hours, at most twice (v2.1.271+). This applies only in interactive subscription sessions, not `claude -p` or the SDK. — [workflows docs](https://code.claude.com/docs/en/workflows)
- Press coverage of the May 28, 2026 launch says workflows target tasks that "typically take hours or days" and that "interrupted jobs resume where they left off rather than restarting". — [reworked.co](https://www.reworked.co/digital-workplace/anthropic-announces-dynamic-workflows-in-claude-code/) (secondary; the official docs make the narrower same-session claim above)

**Claude Code agent view and agent teams**
- Agent view: "A separate supervisor process runs them, so you can close agent view, close your shell … Sessions are also preserved when your machine sleeps … Shutting down still stops running sessions." Commands are `claude --bg`, `claude agents [--json]`, `claude attach|logs|stop|rm <id>`, and `claude daemon status`. State lives in `~/.claude/daemon/roster.json` and `~/.claude/jobs/<id>/state.json`. Research preview. — [code.claude.com/docs/en/agent-view](https://code.claude.com/docs/en/agent-view)
- Agent teams: "No session resumption with in-process teammates: /resume and /rewind do not restore in-process teammates." Teammates are never spawned in `-p` / SDK mode. — [agent-teams docs](https://code.claude.com/docs/en/agent-teams)

**Claude Agent SDK**
- Sessions are written to `~/.claude/projects/<encoded-cwd>/*.jsonl` and support `continue`, `resume` (by ID), and `fork`. "Sessions persist the conversation, not the filesystem." A `SessionStore` adapter mirrors transcripts for cross-host resume. — [agent-sdk/sessions](https://code.claude.com/docs/en/agent-sdk/sessions)

**OpenAI**
- Temporal × OpenAI Agents SDK: "agent orchestration — the agent loop, tool selection, and handoffs — running inside the Workflow, while model calls run as Activities … Agents survive Worker restarts." Python is in public preview and TypeScript is available. — [temporal.io blog](https://temporal.io/blog/announcing-openai-agents-sdk-integration); [docs.temporal.io](https://docs.temporal.io/develop/python/integrations/openai-agents)
- The Agents SDK lists session backends (SQLite, Redis, MongoDB, Dapr, encrypted) and RunState serialization. — [openai-agents-python](https://openai.github.io/openai-agents-python/)
- Codex CLI: `codex exec resume [SESSION_ID]` / `--last`, plus `codex fork`. `codex cloud exec --env ENV_ID [--attempts 1-4]` and `codex cloud list` are marked Experimental. — [Codex CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli); [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

**Third-party**
- orc: "Runs can be stopped and resumed from unfinished nodes, retaining original input. Failed workflows likewise resume from failure points." — [orc](https://github.com/tjdals12/orc)
- yak: an "Append-only journal as sole durable state", "journaled to disk so runs resume exactly where they stopped", with content-addressed caching. — [yak](https://github.com/lchase/yak)
- Archon: SQLite (default) or PostgreSQL with tables for workflow runs. "Failed runs can restart from last checkpoint." — [Archon](https://github.com/coleam00/Archon)
- Gas Town: "Hooks — Git worktree-based persistent storage for agent work. Survives crashes and restarts". Work state lives in the git-backed Beads ledger. Gas City's orchestrator does "stall detection, restart-with-backoff, reconcile-to-desired-state". — [gastown](https://github.com/steveyegge/gastown); [docs.gascity.com](https://docs.gascity.com/getting-started/coming-from-gastown)
- ccmanager: "Sessions restore automatically after restarts via ~/.config/ccmanager/sessions.json". This restores which worktrees were active, not run progress. — [ccmanager](https://github.com/kbwo/ccmanager)

### Inferences
- The Claude Code workflow journal is prefix-replay of a deterministic script, essentially event-sourced replay like Temporal's, but scoped to a Claude session. A failure in the middle of a fan-out reruns everything that started after it. That is a poor fit for multi-day HPC jobs, where per-node independent resume is needed.
- Nothing surveyed models a node as "submit a Slurm job, poll for hours, then resume the agent". The target's HPC/Slurm awareness looks unoccupied.

### Gaps
- No source found showing Slurm/PBS/HPC integration in any of these tools. Absence is inferred from READMEs and docs, not from exhaustive code search.
- The task brief says Claude Code workflows appeared around v2.1.147. I could not verify that version. The docs cite v2.1.202/203 for related features (size guideline, ultracode), and press dates the public launch to May 28, 2026 ([winbuzzer](https://winbuzzer.com/2026/05/29/anthropic-ships-opus-48-with-dynamic-workflows-xcxwbn/), [reworked.co](https://www.reworked.co/digital-workplace/anthropic-announces-dynamic-workflows-in-claude-code/)).

## Q4. Can the graph grow/change during execution?

### Takeaway
Claude Code workflows are dynamic only in the sense that JS control flow can branch, loop until dry, or fan out over agent-discovered lists at runtime. The script cannot be edited mid-run. Changing it means stop, edit, relaunch with prefix-cache resume. Agent teams' task list can grow at runtime because the lead or teammates create tasks with dependencies. Gas Town formulas instantiate molecules from templates. I found no runner with an explicit "add or modify nodes in a running DAG" API.

### Cited Findings
- Workflow examples include "loop-until-dry" and "keep fixing until a check passes". `pipeline()` takes lists discovered by earlier agents. There is a 1,000-agents-per-run cap and 4,096 items per `parallel()`/`pipeline()` call. — [workflows docs](https://code.claude.com/docs/en/workflows); bundled `/workflow-authoring` skill
- To change a running workflow: "relaunch with Workflow({scriptPath, resumeFromRunId}) — the longest unchanged prefix of agent() calls returns cached results instantly; the first edited/new call and everything after it runs live." — bundled `/workflow-authoring` skill; [workflows docs](https://code.claude.com/docs/en/workflows)
- Agent teams: "Tasks can also depend on other tasks: a pending task with unresolved dependencies cannot be claimed until those dependencies are completed." A `TaskCreated` hook can veto new tasks. — [agent-teams docs](https://code.claude.com/docs/en/agent-teams)
- Gas Town: "Formulas (TOML definitions) are instantiated as molecules with tracked steps". Formulas go through cook → protomolecule → pour (persistent) or wisp (ephemeral). — [gastown README](https://github.com/steveyegge/gastown); search summary of [gastown.dev molecules docs](https://gastown.dev/docs/concepts/molecules) (the page itself failed to resolve via DNS when fetched)
- yak: "No dynamic DAG mutation or per-node rerun capabilities are documented." — [yak](https://github.com/lchase/yak)

### Inferences
- Agent teams (a runtime-growing task DAG with dependencies) and workflows (deterministic, resumable fan-out) each cover half of "dynamic DAG plus durable replay". Neither combines both with an editable persisted plan.

### Gaps
- Not verified whether orc or Archon allow node insertion during a run. Their READMEs do not mention it.

## Q5. State/audit: persisted plan file and event log, replay, record of agent decisions?

### Takeaway
Persisted state exists in most tools: Claude Code workflow scripts plus `journal.jsonl` plus per-agent transcripts, agent-team task files, Agent SDK JSONL sessions, Codex JSONL events, yak's append-only journal, Archon's SQLite, and Gas Town's git-backed Beads ledger. yak and the Claude Code workflow journal are the closest to "plan file plus append-only event log with replay". None stores structured "agent decision" records beyond transcripts.

### Cited Findings
- Workflows: "Every run writes its script to a file under your session's directory in ~/.claude/projects/ … diff it against a previous run's script, or edit it." — [workflows docs](https://code.claude.com/docs/en/workflows)
- Workflows: `<transcriptDir>/journal.jsonl` "records each agent's actual return value", alongside `agent-<id>.jsonl` transcripts. — bundled `/workflow-authoring` skill
- Agent teams: task list at `~/.claude/tasks/{team-name}/`, which persists. Team config at `~/.claude/teams/{team-name}/config.json`, removed at session end. Mailboxes at `~/.claude/teams/{team-name}/inboxes/{agent}.json`. — [agent-teams docs](https://code.claude.com/docs/en/agent-teams)
- Codex: `codex exec --json` emits "a JSON Lines stream capturing all events — thread lifecycle, item executions, reasoning, and errors". `--output-schema` and `-o` give structured final output. — [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- yak: "Append-only journal as sole durable state"; `yak graph` emits Mermaid. — [yak](https://github.com/lchase/yak)
- Gas Town: "every completion is recorded, every handoff logged, every closed bead becomes part of a permanent capability ledger" (search summary). OpenTelemetry logs cover "session lifecycle, agent state changes, bd calls with duration, mail operations". — [gastown](https://github.com/steveyegge/gastown)
- Ruflo claims "HIPAA, SOC2, GDPR audit trails as compliance modes. Every federation event produces a structured record." — [ruflo](https://github.com/ruvnet/claude-flow) (self-reported; unverified)

### Inferences
- The Claude Code workflow journal is a cache keyed by call order and prompt, not a semantic event log. It records return values but not node status transitions, decisions, or rationale in a schema an outer tool could audit.

### Gaps
- The exact schema of `journal.jsonl` is not publicly documented.

## Q6. Interface: CLI / MCP / SDK / UI. Can an outer agent drive it?

### Takeaway
Claude Code workflows can be launched headlessly. The Workflow tool is available in `claude -p` and the Agent SDK, with permission rules gating the launch. But monitoring is the TUI (`/workflows`) and there is no external CLI to query workflow run status. Agent view exposes `claude agents --json`, `claude logs`, `claude stop`, which an outer agent can drive. Codex exposes `exec --json`, `app-server`, and cloud subcommands. The third-party DAG runners are CLI-first (orc, yak, Archon), and Archon adds a web UI and chat integrations. Ruflo and Vibe Kanban expose MCP servers.

### Cited Findings
- Workflows are "available in the CLI, the Desktop app, the IDE extensions, non-interactive mode with `claude -p`, and the Agent SDK". The `ultracode` keyword does not trigger from `-p` or relayed text. — [workflows docs](https://code.claude.com/docs/en/workflows)
- `/workflows` keys: `p` pause/resume, `x` stop agent or run, `r` restart a running agent, `s` save script as a command. — [workflows docs](https://code.claude.com/docs/en/workflows)
- Agent view CLI: `claude --bg`, `claude agents --json`, `claude attach|logs|stop|rm <id>`, `claude daemon status`. — [agent-view docs](https://code.claude.com/docs/en/agent-view)
- Codex: `exec` (stable), `resume`, `fork`, `cloud exec/list` (experimental), `app-server` over stdio, WebSocket, or Unix socket (experimental), and `mcp` (manage servers). — [Codex CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- orc: `orc workflow run|status|logs|stream|approve` and 20+ commands. — [orc](https://github.com/tjdals12/orc)
- yak: `run`, `pending`, `resume`, `cancel`, `graph`, `watch`, `artifacts`, `status`. — [yak](https://github.com/lchase/yak)
- Archon: CLI, Web UI (`archon serve`), Slack, Telegram, Discord, GitHub webhooks. — [Archon](https://github.com/coleam00/Archon)
- Vibe Kanban lists MCP integration. Ruflo registers as an MCP server. — [vibe-kanban](https://github.com/BloopAI/vibe-kanban); [ruflo](https://github.com/ruvnet/claude-flow)
- Conductor is a Mac app (YC S24) with "Conductor Cloud". Each workspace has "its own branch, git worktree, run environment, and shared context folder". It runs Claude Code, Codex, Cursor, and OpenCode. — [conductor.build](https://www.conductor.build); [conductor docs](https://www.conductor.build/docs)

### Inferences
- An outer Codex or Claude Code agent can launch a Claude Code workflow headlessly. It cannot easily poll per-node status or rerun one node from outside the session. That is the interface gap the target CLI-plus-skill/MCP design fills.

### Gaps
- Not verified whether orc, yak, or Archon ship an MCP server or agent skill for being driven by an outer harness.

## Q7. Maturity as of Oct 2026: stars, last release, license, maintainer

### Takeaway
Vendor features are moving fast and some are still preview: Claude Code workflows launched as a research preview in May 2026, agent teams are experimental, and agent view is a research preview. OpenAI Agent Builder shuts down on 2026-11-30. Among third parties, Ruflo, Archon, Vibe Kanban, and Gas Town have large followings, but Vibe Kanban is sunsetting and Crystal is deprecated. The closest structural matches (orc, yak) are tiny, with 0 stars.

### Cited Findings
| Project | Stars (≈, 2026-10-02) | License | Maintainer | Status / notes | Source |
|---|---|---|---|---|---|
| Claude Code dynamic workflows | n/a | proprietary | Anthropic | Launched 2026-05-28 (research preview per press). Available on all paid plans; turned on via `/config` on Pro | [docs](https://code.claude.com/docs/en/workflows), [reworked](https://www.reworked.co/digital-workplace/anthropic-announces-dynamic-workflows-in-claude-code/) |
| Claude Code agent teams | n/a | proprietary | Anthropic | Experimental, off by default; needs v2.1.32+ | [docs](https://code.claude.com/docs/en/agent-teams) |
| Claude Code agent view | n/a | proprietary | Anthropic | Research preview | [docs](https://code.claude.com/docs/en/agent-view) |
| OpenAI Agent Builder | n/a | proprietary | OpenAI | Deprecated 2026-06-03, shutdown 2026-11-30. OpenAI points users to the Agents SDK or ChatGPT Workspace Agents | [OpenAI community notice](https://community.openai.com/t/deprecation-notice-agent-builder/1382650), [mcp.directory](https://mcp.directory/blog/openai-agentkit-deprecation-2026) |
| Temporal × OpenAI Agents SDK | n/a | MIT (Temporal SDK) | Temporal | Public preview (Python); TS available | [temporal.io](https://temporal.io/blog/announcing-openai-agents-sdk-integration) |
| Ruflo (ex claude-flow) | ~73.7k | MIT | ruvnet | Active; swarm/MCP | [repo](https://github.com/ruvnet/claude-flow) |
| Archon | ~23.6k | MIT | coleam00 | Active; YAML DAG | [repo](https://github.com/coleam00/Archon) |
| Vibe Kanban | ~28.2k | Apache-2.0 | BloopAI | "Vibe Kanban is sunsetting" | [repo](https://github.com/BloopAI/vibe-kanban) |
| Gas Town | ~18.2k | MIT | Steve Yegge | Active. Gas City SDK rewrite shipped Apr 2026 (Julian Knutsen, Chris Sells) | [repo](https://github.com/steveyegge/gastown), [rywalker.com](https://rywalker.com/research/gas-city) |
| Claude Squad | ~8.6k | AGPL-3.0 | smtg-ai | tmux + worktrees, no DAG | [repo](https://github.com/smtg-ai/claude-squad) |
| Crystal | ~3.1k | MIT | stravu | Deprecated Feb 2026 → Nimbalyst | [repo](https://github.com/stravu/crystal) |
| ccmanager | ~1.3k | MIT | kbwo | Session/worktree manager | [repo](https://github.com/kbwo/ccmanager) |
| Sculptor | ~235 | MIT | Imbue | "experimental research preview"; supports Claude Code and Pi | [repo](https://github.com/imbue-ai/sculptor) |
| Conductor | closed source | proprietary | Conductor (YC S24) | Mac app plus Conductor Cloud | [site](https://www.conductor.build) |
| orc | 0 | MIT | tjdals12 | Tiny; closest "per-node harness choice" DAG | [repo](https://github.com/tjdals12/orc) |
| yak | 0 | MIT | lchase | Tiny; append-only journal, gates, resume | [repo](https://github.com/lchase/yak) |

### Inferences
- The ground the target idea occupies (durable, auditable, multi-harness, agent-only DAG driven by an outer agent, HPC-aware) has many partial neighbors but no mature incumbent. The vendor feature (Claude Code workflows) is session-scoped and Claude-only. The durable engines (Temporal) are not coding-agent-harness-native. The coding-agent DAG engines that exist are either young and small (orc, yak) or software-dev-focused with a DB backend and no HPC story (Archon).

### Gaps
- Latest release tags and dates for every GitHub project could not be retrieved, because the GitHub API was rate-limited and the page summaries omitted them.
- I could not find a GitHub repo named "anthropic-workflow-design" (a reverse-engineered workflow spec). A GitHub search for the term returned 28 unrelated repos, the nearest being `zircote/workflows-plugin` (2 stars, "Claude Code plugin for consistent workflow orchestration patterns"). — [GitHub search](https://github.com/search?q=anthropic-workflow-design&type=repositories)
- Gas Town / Gas City docs on gastown.dev failed DNS resolution at fetch time. Molecule/formula details come from the README and search snippets.
- Not covered in depth: OpenHands' own orchestration features, pi's subagent features, and GitHub Agentic Workflows (`gh aw`, markdown-defined agent workflows compiled to Actions `.lock.yml`, seen in search results at [github.github.com/gh-aw](https://github.github.com/gh-aw/setup/creating-workflows/)). The last may be worth a separate look as event-triggered agent-per-workflow prior art.
