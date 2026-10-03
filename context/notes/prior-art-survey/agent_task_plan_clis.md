# Agent-native task/plan/spec tools for coding agents (prior art for an "agent-per-node DAG workflow CLI")

Scope: tools that give coding agents an external, persistent plan/task graph (CLI + files + agent-driven). Evaluated against the target idea: a DAG where every node is executed by a headless agent session (`claude -p`, `codex exec`, pi, OpenHands), plan-as-contract approved before execution, durable over hours–days (HPC/Slurm), monitorable, cancel/resume/rerun per node, dynamic DAG, plan file + append-only event log recording agent decisions, driven by an outer harness via CLI + skill / MCP.

Repo stats below were pulled from the GitHub REST API on 2026-10-02 (`api.github.com/repos/<owner>/<repo>` and `/releases/latest`) unless noted; beads/gastown stats come from the GitHub web page (API rate-limited after redirect).

Quick reference table (stars / license / latest release, as of 2026-10-02):

| Tool | Stars | License | Latest release | Last push |
|---|---|---|---|---|
| Claude Task Master (eyaltoledano/claude-task-master) | 28,130 | MIT + Commons Clause (API: NOASSERTION) | task-master-ai@0.43.1, 2026-03-31 | 2026-04-28 |
| beads (now gastownhall/beads) | ~27.6k | MIT | v1.x (v1.0.0 ~2026-04-03; v1.2.2, v1.3.0-rc.1 seen) | — |
| Gas Town (now gastownhall/gastown) | ~18.2k | MIT | v1.0.0 ~2026-04-03 | — |
| Backlog.md (MrLesk/Backlog.md) | 6,928 | MIT | v1.53.0, 2026-09-24 | 2026-09-28 |
| GitHub Spec Kit (github/spec-kit) | 139,864 | MIT | v1.1.0, 2026-10-02 | 2026-10-02 |
| BMAD-METHOD (bmad-code-org/BMAD-METHOD) | 53,742 | MIT per README (API: NOASSERTION) | v6.12.0, 2026-09-04 | 2026-10-02 |
| Agent OS (buildermethods/agent-os) | 5,463 | MIT | v3.0.0, 2026-01-20 | 2026-08-29 |
| OpenSpec (Fission-AI/OpenSpec) | 70,938 | MIT | v1.14.0, 2026-09-30 | 2026-10-02 |
| Shrimp Task Manager (cjo4m06/mcp-shrimp-task-manager) | 2,145 | MIT | no GitHub release | 2025-08-21 (stale) |
| Conductor (gemini-cli-extensions/conductor) | 3,752 | Apache-2.0 | conductor-v0.4.1, 2026-03-11 | 2026-09-01 |
| Superpowers (obra/superpowers) | 294,488 | MIT | v6.4.2, 2026-09-25 | 2026-09-27 |
| Ralph (snarktank/ralph) | 21,893 | MIT | no GitHub release | 2026-02-02 |

## Q1. Does it represent work as a dependency graph (DAG), and is each task executed by an agent session (auto-spawning `claude -p` / `codex exec` per task)?

### Takeaway
Many tools model dependencies (beads, Task Master, Backlog.md, Shrimp, Claude Code native Tasks, Kiro, Spec Kit's `[P]` markers), but almost none spawn a fresh headless agent per node automatically. The ones that do spawn sessions per unit of work (Gas Town via tmux, snarktank/ralph via a bash loop, Superpowers/Claude Code subagents in-process, Kiro "waves") are either coding-specific, sequential, or tied to one IDE. No surveyed tool combines an explicit DAG with "one external headless agent process per node" as its core abstraction.

### Cited Findings
- **beads**: "dependency-aware graph"; hierarchical IDs (`bd-a3f8`, `bd-a3f8.1`); `bd ready` lists tasks with no open blockers; edge types include blocking/parent-child plus `relates-to`, `duplicates`, `supersedes`, `replies-to`; `bd update <id> --claim` atomically claims work. "Beads is a ledger and coordination system—it does not execute tasks." — [beads README](https://github.com/steveyegge/beads)
- **Gas Town**: orchestrates Claude Code, Codex, Copilot, Gemini etc. in **tmux-backed sessions**; `gt sling <bead-id> <rig>` assigns a bead to a worker; "Polecats" are workers with "persistent identity but ephemeral sessions. Spawned for tasks, sessions end on completion"; `--agent` flag overrides runtime per spawn. — [Gas Town README](https://github.com/steveyegge/gastown)
- **Gas Town molecules**: Formulas = "source TOML template defining workflow steps"; protomolecule = frozen template; molecule = active instance; docs present steps as an ordered checklist a polecat walks through (`gt prime` shows "formula checklist inline", then `gt done`) — not documented as a DAG. — [gastown docs: molecules](https://github.com/gastownhall/gastown/blob/main/docs/concepts/molecules.md)
- **Claude Task Master**: parses a PRD into tasks with dependencies, complexity analysis, subtask expansion, tags, "next task"; does NOT do autonomous execution or per-task Claude Code spawning — it is a planning/coordination tool used by the host agent. — [Task Master README](https://github.com/eyaltoledano/claude-task-master)
- **Backlog.md**: "milestones & dependencies" to "make execution order reviewable, with task detail showing what a task waits on and what waits on it"; does not execute tasks or spawn agents. — [Backlog.md README](https://github.com/MrLesk/Backlog.md)
- **Shrimp Task Manager (MCP)**: tools `plan_task, analyze_task, reflect_task, split_tasks, execute_task, verify_task`; "automatic management of task relationships" (dependencies); `execute_task` returns "guidance to the agent" rather than running anything. — [Shrimp README](https://github.com/cjo4m06/mcp-shrimp-task-manager)
- **Claude Code native Tasks**: tasks carry `blockedBy` / `blocks`; "if task B depends on task A's output, the system knows not to start B until A finishes", controlling order when running parallel sub-agents (secondary sources). — [CircleCI blog](https://loop.circleci.com/claude-codes-task-tool-from-sequential-to-parallel-work), [claudelog](https://www.claudelog.com/faqs/what-are-tasks-in-claude-code/)
- **AWS Kiro specs**: `requirements.md`/`design.md`/`tasks.md`; Kiro "analyzes task dependencies and executes independent tasks concurrently across 'waves'… Waves execute sequentially; tasks within a wave execute concurrently." Execution is by Kiro's own agent inside the Kiro IDE/CLI. — [Kiro docs: Specs](https://kiro.dev/docs/specs/) (page "last updated October 2, 2026")
- **GitHub Spec Kit**: `/speckit-constitution → specify → plan → tasks → implement → converge`; `tasks.md` has "[P] parallel markers and dependencies"; the host agent (Copilot, Claude Code, Codex…) executes; user repeats "implement → converge until convergence reports Converged". — [Spec Kit README](https://github.com/github/spec-kit)
- **OpenSpec**: `/opsx:explore`, `/opsx:propose` → `openspec/changes/<feature>/{proposal.md, specs/, design.md, tasks.md}`, `/opsx:apply`, `/opsx:archive`; tasks.md is a checklist executed by the host agent; no task DAG. — [OpenSpec README](https://github.com/Fission-AI/OpenSpec)
- **Superpowers**: brainstorming → using-git-worktrees → writing-plans (tasks of "2-5 minute" granularity) → executing-plans or subagent-driven-development; option to dispatch "a fresh subagent per task with a review after each (most thorough)". Subagents are in-harness (e.g. Claude Code Task tool), not external `claude -p` processes. — [Superpowers README](https://github.com/obra/superpowers)
- **Ralph (Huntley)**: literally `while :; do cat PROMPT.md | claude-code ; done`; `fix_plan.md` is a prioritized todo list; "Only one thing" per loop. — [ghuntley.com/ralph](https://ghuntley.com/ralph/) (2025-07-14)
- **snarktank/ralph**: `ralph.sh` — "each iteration spawns a new AI instance (Amp or Claude Code) with clean context"; picks highest-priority story with `passes: false` from `prd.json`; dependencies between stories not documented (priority ordering only). — [snarktank/ralph](https://github.com/snarktank/ralph)
- **Anthropic ralph-wiggum plugin**: loop runs "inside your current session" via a Stop hook that blocks exit and re-feeds the prompt; `--completion-promise`, `--max-iterations`. — [claude-code/plugins/ralph-wiggum](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum)
- **Anthropic long-running harness**: initializer agent (first session) + coding agent (subsequent sessions), one feature per session; feature list JSON (`category`, `description`, `steps`, `passes`) — a flat list, not a DAG. — [Anthropic engineering, 2025-11-26](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- **OpenAI ExecPlans (PLANS.md)**: narrative milestones that are "independently verifiable"; one agent works through them ("more than seven hours from a single prompt"); no DAG. — [OpenAI Cookbook: ExecPlans](https://developers.openai.com/cookbook/articles/codex_exec_plans)
- **Conductor**: per-"track" `spec.md` + `plan.md` with phases/tasks; host agent implements via `/conductor:conductor-implement`. — [Conductor README](https://github.com/gemini-cli-extensions/conductor)
- **BMAD-METHOD**: skill/prompt-based personas inside the host IDE (Claude Code, Codex marketplace plugins), not autonomous agent spawning; artifacts include story files and `sprint-status.yaml`. — [BMAD README](https://github.com/bmad-code-org/BMAD-METHOD)
- **Linear for Agents**: agents appear as workspace users; issues can be delegated to Codex, Cursor, Copilot, Devin etc.; API has an agent "sessions" abstraction. Issue graph is Linear's (SaaS), execution is by each vendor's cloud agent. — [Linear changelog 2025-12-04](https://linear.app/changelog/2025-12-04-openai-codex-agent), [Linear agents integrations](https://linear.app/integrations/agents)

### Inferences
- Closest in "shape" to the target idea: **Gas Town** (ledger graph + automatic per-task agent sessions + health monitoring + durable hooks), then **beads** (as the state layer), then **snarktank/ralph** and the **Anthropic harness** (fresh session per unit, file-based state), then **Kiro** (dependency waves + per-task execution, but IDE-bound).
- Pure ledgers (Task Master, Backlog.md, Shrimp, beads alone) prove agents can maintain a dependency graph through a CLI/MCP; they leave scheduling and execution to the outer agent — exactly the gap the proposed tool would fill.
- Spec-driven kits (Spec Kit, OpenSpec, Kiro, Conductor, BMAD, Agent OS) contribute the "plan-as-contract" UX, not a runtime.

### Gaps
- Could not confirm whether Gas Town formulas support explicit inter-step `needs`/DAG edges; docs fetched describe sequential checklists. Yegge's "Welcome to Gas Town" Medium post (Jan 2026) returned HTTP 403, so MEOW/GUPP/NDI definitions could not be verified from the primary source.
- Claude Code Tasks dependency semantics (`blockedBy`) are confirmed only by secondary sources; official docs fetched confirm persistence but not the field names.
- Not verified whether Kiro's wave execution can use non-Kiro agents or run headless from a CLI.

## Q2. Plan-as-contract: is the plan human-approved before execution? Can it be amended during execution, and is that tracked?

### Takeaway
Human approval gates are standard in spec-driven kits (Kiro phase gates, Spec Kit, OpenSpec, Conductor, Superpowers, Backlog.md's "wait for my approval"). Amendment tracking is weak everywhere: changes are tracked only implicitly via git diffs of Markdown/JSON, except OpenAI ExecPlans (explicit Decision Log + Surprises & Discoveries) and beads/Dolt (versioned DB history). None treat the approved DAG as a versioned contract with recorded diffs/approvals of mid-run changes.

### Cited Findings
- **Kiro**: three-phase workflow with "approval gates between phases"; users must approve progression. — [Kiro docs](https://kiro.dev/docs/specs/)
- **Spec Kit**: "review the result before continuing" after each skill; optional quality gates (`clarify`, `analyze`). — [Spec Kit](https://github.com/github/spec-kit)
- **OpenSpec**: "you review the plan before any code is written"; completed changes archived to `openspec/changes/archive/[date]-[feature]/`; philosophy "fluid not rigid… iterative not waterfall". — [OpenSpec](https://github.com/Fission-AI/OpenSpec)
- **Conductor**: "Plans require review before implementation begins"; "Git-Aware Revert" that "understands logical units of work (tracks, phases, tasks) rather than just commit hashes". — [Conductor](https://github.com/gemini-cli-extensions/conductor)
- **Superpowers**: plans require explicit human sign-off before execution. — [Superpowers](https://github.com/obra/superpowers)
- **Backlog.md**: recommended workflow has agent "write an implementation plan in the task" and "wait for my approval before coding"; "three review checkpoints" (task spec, implementation plan, code). — [Backlog.md](https://github.com/MrLesk/Backlog.md)
- **OpenAI ExecPlans**: "living documents. Contributors are required to revise it as progress is made, as discoveries occur, and as design decisions are finalized"; mandatory sections Progress (timestamped checkboxes), Surprises & Discoveries, Decision Log, Outcomes & Retrospective; "Record every decision… Decision: … / Rationale: … / Date/Author: …"; explicitly autonomous: "do not prompt the user for 'next steps'; simply proceed to the next milestone". — [OpenAI Cookbook](https://developers.openai.com/cookbook/articles/codex_exec_plans)
- **Anthropic harness**: feature list is a quasi-contract the agent must not alter: "edit this file only by changing the status of a passes field… 'It is unacceptable to remove or edit tests.'" — [Anthropic engineering](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- **Gas Town**: human talks to the Mayor ("describe what you want"), Mayor "breaks down into tasks" and spawns agents; blockers escalate "Deacon → Mayor → Overseer" (Overseer = human). No formal pre-execution approval step documented. — [Gas Town README](https://github.com/steveyegge/gastown)
- **BMAD**: emphasizes "keeping you in control" and "explicit decisions". — [BMAD](https://github.com/bmad-code-org/BMAD-METHOD)

### Inferences
- The ExecPlans Decision Log and Anthropic's "only flip `passes`" rule are the best existing precedents for constraining/recording agent changes to an approved plan; a tool could formalize these as typed events (plan_amended, decision_recorded) with an approval state.
- Spec kits' approval is per-document and per-phase, not per-node; none support approving a DAG diff mid-run.

### Gaps
- No tool found that records "who approved which plan version" as structured data.

## Q3. Persistence: files in repo, git-backed, DB? Event log / history / audit of agent decisions?

### Takeaway
Persistence is overwhelmingly Markdown/JSON in the repo (git as history). beads is the outlier: it moved to Dolt (versioned SQL) with JSONL export and per-issue history; Gas Town adds `.events.jsonl` session logs and git-worktree "hooks" that survive crashes. Explicit append-only decision logs exist only as conventions (ExecPlans Decision Log, ralph `progress.txt`, Anthropic `claude-progress.txt`).

### Cited Findings
- **beads**: primary storage Dolt — embedded mode (`.beads/embeddeddolt/`, single writer) or server mode (`dolt sql-server`, concurrent writers); `.beads/issues.jsonl` export is "not the source of truth or a backup"; `bd show <id>` shows history/change tracking; hash-based IDs prevent merge collisions; "Compaction: semantic summarization of closed tasks"; `bd remember` stores durable facts. — [beads README](https://github.com/steveyegge/beads)
- beads moved from SQLite+git to exclusively Dolt in early Feb 2026, causing "friction for existing Beads users"; v1.0.1 fully Dolt-backed. — [DoltHub blog 2026-04-15](https://www.dolthub.com/blog/2026-04-15-common-beads-workflows/), [DoltHub blog 2026-04-02](https://www.dolthub.com/blog/2026-04-02-restoring-beads-classic/)
- beads origin: "Claude wanted SQLite. We compromised on both, and Beads was born, in about 15 minutes of mad design." — [LWN](https://lwn.net/Articles/1070995/) (May 2026, quoting Yegge)
- **Gas Town**: hooks are "Git worktree-based persistent storage for agent work. Survives crashes and restarts"; "Seance" "discovers previous agent sessions via `.events.jsonl` logs, enabling agents to query predecessors for context and decisions". — [Gas Town README](https://github.com/steveyegge/gastown)
- Gas Town poured wisps: "If a session dies, completed steps remain closed and work resumes from the last checkpoint"; heuristic "If you would curse losing the progress after a crash, set `pour = true`." — [gastown molecules doc](https://github.com/gastownhall/gastown/blob/main/docs/concepts/molecules.md)
- **Task Master**: `.taskmaster/tasks/tasks.json`, local-first. — [Task Master](https://github.com/eyaltoledano/claude-task-master)
- **Backlog.md**: "every task is a plain `.md` file in your repo"; completed tasks remain in git as "a permanent record of what was attempted and why". — [Backlog.md](https://github.com/MrLesk/Backlog.md)
- **Shrimp**: JSON in `DATA_DIR` with automatic task-history backups. — [Shrimp](https://github.com/cjo4m06/mcp-shrimp-task-manager)
- **Claude Code Tasks**: "To share a task list across sessions, set `CLAUDE_CODE_TASK_LIST_ID` to use a named directory in `~/.claude/tasks/`". — [Claude Code docs: interactive mode](https://code.claude.com/docs/en/interactive-mode)
- **Spec Kit** `.specify/` + `spec.md/plan.md/tasks.md`; **OpenSpec** `openspec/changes/…`, `openspec/specs/`, beta "Stores" for "planning in a repo of its own… shared by `git push`"; **Kiro** `.kiro/specs`; **Conductor** `conductor/tracks.md`, `conductor/tracks/<id>/{spec.md,plan.md,metadata.json}`. — [Spec Kit](https://github.com/github/spec-kit), [OpenSpec](https://github.com/Fission-AI/OpenSpec), [Kiro](https://kiro.dev/docs/specs/), [Conductor](https://github.com/gemini-cli-extensions/conductor)
- **snarktank/ralph**: `prd.json` (status), `progress.txt` "Append-only learnings for future iterations", git commits, AGENTS.md updated with discovered patterns. — [snarktank/ralph](https://github.com/snarktank/ralph)
- **Anthropic harness**: `claude-progress.txt` (log of work and decisions), `init.sh`, descriptive git commits for rollback. — [Anthropic engineering](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- **AGENTS.md**: "a README for agents"; "just standard Markdown"; no task/plan structure; stewarded by the Agentic AI Foundation under the Linux Foundation; "used by over 60k open-source projects". — [agents.md](https://agents.md/)

### Inferences
- No surveyed tool stores a structured, replayable append-only event log of node state transitions + agent decisions as the source of truth; Gas Town's `.events.jsonl` and beads/Dolt history come closest. An event-sourced plan file + JSONL log would be a differentiator, and Dolt-backed beads shows both the appeal and the migration pain of a heavier store.

### Gaps
- Exact schema of Gas Town `.events.jsonl` not verified.

## Q4. Execution & monitoring: does it run things, track status, handle long-running external jobs, cancel/resume?

### Takeaway
Only Gas Town (and its successor Gas City), Kiro, and loop-style harnesses (ralph, Anthropic harness) actually drive execution; Gas Town alone has a monitoring/health/recovery layer (Witness, Deacon, `gt feed`, stall detection). None address long-running external compute (HPC/Slurm jobs over hours–days) — they assume the agent session itself is the long-running unit and work is code changes verified by tests/merges.

### Cited Findings
- **Gas Town monitoring**: watchdog chain "Daemon ← Boot ← Deacon ← Witnesses & Refineries"; Witness detects stuck agents and triggers nudge/handoff; "GUPP Violation" = "hooked work with no progress for an extended period"; `gt feed` TUI with a Problems view surfacing "agents needing human intervention"; Convoys labeled `mountain` get "autonomous stall detection and smart skip logic"; Refinery is a Bors-style bisecting merge queue. — [Gas Town README](https://github.com/steveyegge/gastown)
- Gas Town resume: `gt prime` for "context recovery"; `gt seance` to query predecessor sessions. — [Gas Town README](https://github.com/steveyegge/gastown)
- **Gas City** (announced 2026-04-24): SDK/successor that deconstructs Gas Town into composable "packs"; Gas Town ships as a pack; company (CEO Chris Sells, Yegge as advisor) sells Beads Team Server, Gasworks, Workshop. — [Product Hunt / search summary](https://www.producthunt.com/products/gas-city), [gascity.com/about](https://gascity.com/about/), [The New Stack](https://thenewstack.io/steve-yegges-ai-agent-orchestration-project-gas-town-comes-to-the-cloud-and-brings-the-wasteland-with-it/)
- Gas Town and beads both released 1.0.0 on 2026-04-03; Gas Town launched January 2026, described by Yegge as "100% vibe coded. I've never seen the code". — [Wikipedia: Steve Yegge](https://en.wikipedia.org/wiki/Steve_Yegge)
- **Kiro**: "Run task" individually or "Run all tasks"; "Tasks are updated as in-progress or completed". — [Kiro docs](https://kiro.dev/docs/specs/)
- **snarktank/ralph**: max iterations (default 10); exit when all stories `passes: true` and agent prints `<promise>COMPLETE</promise>`. — [snarktank/ralph](https://github.com/snarktank/ralph)
- **ralph-wiggum plugin** warns completion-promise is exact string match; "Always rely on `--max-iterations` as your primary safety mechanism." — [ralph-wiggum plugin](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum)
- **Huntley**: tests as backpressure; human decides "when to reset and restart". — [ghuntley.com/ralph](https://ghuntley.com/ralph/)
- **Task Master, Backlog.md, Shrimp, beads**: track status only; no execution. — sources above.
- **Conductor**: status/review/revert commands, task checkpoints with "three recovery flows". — [Conductor](https://github.com/gemini-cli-extensions/conductor)
- **Linear**: inbox notifications "when Codex needs your input or when work is ready for review"; Cursor shows "thoughts and tools, to-dos, as it progresses". — [Linear changelog](https://linear.app/changelog/2025-12-04-openai-codex-agent), [Linear blog](https://linear.app/blog/how-cursor-integrated-with-linear-for-agents)

### Inferences
- Gas Town is the strongest existing evidence that agent-per-task orchestration with durable git-backed state and supervisor agents is workable, but it is coding/merge-queue oriented, tmux-interactive (not headless `-p`/`exec`), and has a reputation for chaos/"vibe coded" quality.
- No tool models "agent submits external job, then waits/polls for hours" as a first-class node state (e.g., `waiting_external` with job IDs, Slurm integration). This is an open niche.
- Per-node cancel/rerun is absent in ledgers; Conductor's track/phase/task-level git revert is the nearest "rerun node" analog.

### Gaps
- Could not verify Gas Town's cost/reliability numbers or whether it supports fully headless (`claude -p`) workers.
- Kiro's handling of long-running/background tasks and cancel semantics not verified.

## Q5. Interface: CLI, MCP server, skill?

### Takeaway
The dominant pattern is CLI + slash-command/skill installed into the host agent, with MCP as an optional channel. This matches the proposed "outer harness drives CLI + skill/MCP" design.

### Cited Findings
- **beads**: `bd` CLI (`bd ready`, `bd create`, `bd update --claim`, `bd prime`, `bd remember`) + MCP server for Claude Code, Copilot CLI, others. — [beads](https://github.com/steveyegge/beads)
- **Gas Town**: `gt` CLI (`gt mayor attach`, `gt convoy create`, `gt sling`, `gt feed`, `gt seance`, `gt escalate`); Claude hooks in `.claude/settings.json` "for mail injection and startup". — [Gas Town](https://github.com/steveyegge/gastown)
- **Task Master**: MCP server + CLI. — [Task Master](https://github.com/eyaltoledano/claude-task-master)
- **Backlog.md**: `backlog task create|list`, `backlog board`, `backlog browser` (kanban web UI), `backlog mcp start`. — [Backlog.md](https://github.com/MrLesk/Backlog.md)
- **Shrimp**: MCP-only tools + optional web GUI / React task viewer. — [Shrimp](https://github.com/cjo4m06/mcp-shrimp-task-manager)
- **Spec Kit**: `specify` CLI (Python 3.11+, uv) installs agent skills/commands; supports many agents via integration keys. — [Spec Kit](https://github.com/github/spec-kit)
- **OpenSpec**: `openspec init|update|config` CLI + slash commands in 30+ tools (`/opsx-propose` in Cursor, `$openspec-propose` in Codex). — [OpenSpec](https://github.com/Fission-AI/OpenSpec)
- **Superpowers**: skills framework supporting Claude Code, Codex, Cursor, Devin, Gemini, Copilot, OpenCode, Pi, Qwen Code and others. — [Superpowers](https://github.com/obra/superpowers)
- **Conductor**: plugin for Antigravity and Claude Code (originally Gemini CLI extension), `/conductor:*` commands. — [Conductor](https://github.com/gemini-cli-extensions/conductor)
- **BMAD**: skills CLI + marketplace plugins for Claude Code and Codex. — [BMAD](https://github.com/bmad-code-org/BMAD-METHOD)
- **Agent OS v3**: refocused on standards ("Discover Standards", "Deploy Standards", "Shape Spec", "Index Standards"); the README no longer mentions v2's create-tasks/implement-tasks/orchestrate-tasks. — [Agent OS](https://github.com/buildermethods/agent-os)

### Inferences
- Shipping as `CLI + skill (+ optional MCP)` with multi-harness install targets (Claude Code, Codex, pi, OpenCode) is now table stakes; Superpowers/OpenSpec/Spec Kit show a single package can target 15–30 harnesses.

### Gaps
- Agent OS v3 storage layout and whether tasks/orchestration were fully removed could not be confirmed beyond README text.

## Q6. Maturity and momentum (as of 2026-10-02)

### Takeaway
Spec-driven kits and skill frameworks have the most adoption (Superpowers ~294k, Spec Kit ~140k, OpenSpec ~71k, BMAD ~54k stars) and are actively releasing; task ledgers are mid-sized (Task Master ~28k but slowing, beads ~28k, Backlog.md ~7k). Execution-oriented orchestrators (Gas Town ~18k) are younger and more volatile (Dolt migration, Gas City pivot). Shrimp appears unmaintained.

### Cited Findings
- Stats in the table above — GitHub API, 2026-10-02 (e.g. [spec-kit](https://github.com/github/spec-kit) v1.1.0 released 2026-10-02; [OpenSpec](https://github.com/Fission-AI/OpenSpec) v1.14.0 2026-09-30; [Backlog.md](https://github.com/MrLesk/Backlog.md) v1.53.0 2026-09-24; [Superpowers](https://github.com/obra/superpowers) v6.4.2 2026-09-25; [BMAD](https://github.com/bmad-code-org/BMAD-METHOD) v6.12.0 2026-09-04).
- Task Master last push 2026-04-28, last release 0.43.1 on 2026-03-31; README now points to parent company "Hamster" products. — [Task Master](https://github.com/eyaltoledano/claude-task-master)
- Task Master license: "MIT License with Commons Clause" — prohibits selling Task Master itself or offering it as a hosted service. — [Task Master](https://github.com/eyaltoledano/claude-task-master)
- Shrimp last push 2025-08-21, no releases. — [Shrimp](https://github.com/cjo4m06/mcp-shrimp-task-manager)
- beads and gastown repos redirected from `steveyegge/*` to `gastownhall/*`; beads releases v1.2.2 and v1.3.0-rc.1 observed. — [newreleases.io beads v1.3.0-rc.1](https://newreleases.io/project/github/gastownhall/beads/release/v1.3.0-rc.1), [DoltHub 2026-07-22 proxied server mode](https://dolthub.com/blog/2026-07-22-introducing-beads-proxied-server-mode/)
- A third party launched "Gas Town by Kilo" (hosted). — [SD Times](https://sdtimes.com/softwaredev/gas-town-by-kilo-multi-agent-orchestrator-now-available/)

### Inferences
- The "plan approval + Markdown artifacts" UX is commoditized; the differentiator for the proposed tool would be the runtime: headless agent-per-node execution, external long-job awareness (Slurm), event-sourced audit, and dynamic DAG amendment with approval — none of which the high-star tools provide.
- Superpowers' star count (294k) is unusually high; reported as returned by the API but worth a sanity check before citing in a final report.

### Gaps
- Exact latest release tags/dates for beads and gastown (API rate-limited); Conductor's original launch date (believed Dec 2025 as a Gemini CLI extension) not verified; Kiro GA/pricing not checked; stars for Linear integrations N/A (SaaS).
