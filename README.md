# flower

**Durable, file-first workflows for long-running research driven by people and coding agents.**

You describe a multi-step study as a plan (YAML). You approve it. flower runs it, for minutes or for
days. Steps can be agent tasks (Claude Code, Codex, pi), shell or Python steps, Slurm jobs on a cluster,
human decisions, or waits for external data. You can stop, look away, come back in a new session, and
the run tells you exactly where it is, what happened, why, and what it needs from you.

```text
$ flower status
Screening fcc metals for atomic-oxygen binding  ·  run fcc-catalyst-screening-20261003-…  ·  PARKED  ·  9m12s
  plan generation 2 (4be1…)  ·  14/16 done  ·  agents used 310k tokens

     NODE              KIND                  TIME  RESULT / ACTIVITY
  ✓  choose            agent:codex/gpt-5.5  1m31s  Picked Au, Cu, Pt, Ni to span weak→strong O binding
  ✓    lattice[0]      job@hpc                 6s  Au: a0 = 4.056 Å, B = 174 GPa (EMT)
  …
  ✓      adsorption    job@hpc                 0s  4/4 item(s) succeeded
  ✓        analyze     agent:codex/gpt-5.5  2m02s  Pt is closest to the -0.5 eV target; 6-layer check added
  ✓          converge  job@hpc                 9s  O on Pt(111) fcc, 6 layers: E_ads = -0.431 eV
  ◆            review  gate                    1m  needs your decision (approve / revise / abort) — see below

Waiting for you:
  • gate review#a1: Best candidate: Pt …
      flower answer fcc-catalyst-screening-… review#a1 approve [--text '…']
```

## Why

Coding agents are now very good at single tasks. They struggle with long, multi-round work: jobs that
outlive the session, context that gets compacted, decisions nobody recorded. flower keeps the state
outside the agent:

* **The plan is a contract.** The user approves its digest before anything runs. Changes during the
  run are *amendments*: proposed (by an agent or a person), approved, then appended as a new plan
  generation. History is never rewritten.
* **The journal is the truth.** Every run is a directory with an append-only `events.jsonl`.
  `status`, `log`, `report` and the scheduler are all folds of that log, so a run can be replayed and
  audited, and any process (you, cron, another agent) can continue it.
* **Nothing holds a process open.** Local steps run under detached supervisors. Slurm jobs park
  until they finish. `flower tick` advances a run in one short pass, so there is no daemon to babysit;
  cron or `scrontab` is enough on a cluster.
* **It is easy to read.** Every node reports a one-line summary and, for agents, a rationale. Failures
  have a class (`timeout`, `oom`, `node_fail`, `auth`, `contract`, …) and a hint. Every command prints
  what to do next. `flower report` writes a Markdown/HTML report with the decisions, results, files,
  plan changes and a full timeline.

## Install

```bash
pip install -e .            # Python ≥ 3.9; dependencies: pyyaml, jsonschema
flower doctor            # which harnesses and Slurm tools are available
```

## Quick start

```bash
flower init                       # creates .flower/ in this directory
flower plan new study.yaml        # a commented starter plan
flower plan validate study.yaml   # lists every problem with a fix hint
flower run study.yaml             # shows the plan, asks you to approve, then runs it live
```

`ctrl-c` detaches. Running steps keep going. Continue with `flower resume RUN` (foreground) or
`flower resume RUN --detach` (background). From any later session:

```bash
flower ls                     # all runs
flower status [RUN]           # where it is, what needs you
flower log [RUN]              # what happened, in order, by whom
flower show RUN NODE          # attempts, inputs, outputs, files, errors, rationale
flower logs RUN NODE          # agent transcript / Slurm output / shell output
flower answer RUN GATE approve --text "why"
flower rerun RUN NODE         # run it again (a foreach: all its items) plus everything downstream (unchanged steps are reused)
flower report RUN             # report.md + report.html
```

## A plan

```yaml
flower: 1
id: si-bands
title: Silicon band structure
inputs:
  structure: {type: path}
clusters:
  hpc: {transport: ssh, host: myhpc, remote_root: ~/flower-runs, modules: [qe/7.3]}
nodes:
  - id: relax
    kind: job
    cluster: hpc
    resources: {nodes: 1, ntasks: 32, time: "04:00:00"}
    stage_in: ["${inputs.structure}"]
    script: |
      mpirun pw.x -in relax.in > relax.out
      python parse.py relax.out > "$FLOWER_OUTPUTS"      # {"energy": …, "converged": true}
    outputs: {energy: number, converged: boolean}

  - id: check
    kind: agent
    harness: {name: claude, model: sonnet}
    prompt: |
      Inspect ${relax.outputs.job_dir}/relax.out. Is the relaxation physically sensible?
    outputs: {ok: boolean, concerns: array}

  - id: bands
    kind: job
    cluster: hpc
    when: "${check.outputs.ok}"
    stage_in: [{from: "remote:${relax.outputs.job_dir}/out", to: out, mode: link}]   # no download round trip
    script: mpirun pw.x -in bands.in > bands.out

  - id: sign-off
    kind: gate
    message: "Bands done (${bands.summary}). Publish?"
    decisions: [approve, reject]
```

Node kinds: `agent`, `shell`, `function`, `job`, `gate`, `wait`. Plus `foreach` fan-out, `when`
conditions, retries keyed on failure class, timeouts, caching and rework loops. The full format is in
[`docs/PLAN_REFERENCE.md`](docs/PLAN_REFERENCE.md), also printed by `flower plan reference`.
The user guide is [`docs/GUIDE.md`](docs/GUIDE.md).

## Web UI

```bash
flower ui                         # starts (or reuses) this project's UI in the background and prints its links
flower ui status | stop
```

Each project gets one UI server that keeps running in the background, on a stable port.
- **On the same machine:** open the `http://localhost:<port>/?token=…` link it prints.
- **Project on a remote machine, browser on your laptop:** run one command on the laptop. It starts the
  UI there if needed, opens an ssh tunnel and opens your browser:

  ```bash
  flower open me@cluster.example.org:~/projects/si-study
  ```

  Without flower on the laptop, use the `ssh -N -L <port>:<…>/.flower/ui.sock me@host` line that
  `flower ui` prints, then browse to `http://localhost:<port>`. To set this up once, add
  `LocalForward <port> <…>/.flower/ui.sock` to that host in `~/.ssh/config`; any later `ssh` login then
  carries the UI.

**Why no token is needed through the tunnel.** The tunnel goes to the server's Unix socket, which only
you can open (mode 0600). Through it, the link needs no token. On a shared machine, the TCP port is
still guarded by the token, and actions always need the token the page carries.

The UI is a local, dependency-free web view of the project's runs. It uses Eleforge's visual language:
zinc panels, a dot-grid canvas and the "constellation" DAG. The light theme is the default;
the Theme button switches to dark.
- **Canvas:** the layout is measured against a real render of Eleforge's `ConstellationNode`.
  - **Nodes:** the run is a 48px "folder" circle. Its nodes are 36px circles with a 2px border in the
    node kind's colour: agent, human gate, HPC job, shell, function, wait and fan-out. They sit on
    git-graph lanes.
  - **Status:** shown on the ring:
    - pending: dashed;
    - running: a spinning comet arc;
    - completed: a breathing glow;
    - failed: red;
    - waiting on you: a pulsing amber ring.
  - **Edges:** routed at right angles in the colour of the node they point to. Hovering a node
    highlights its edges and shows an Eleforge-style tooltip.
  - **Controls:** **Labels** hides node names for the minimal Eleforge look. Pan by dragging or
    scrolling; zoom with Ctrl+scroll. A banner appears when something needs you.
- **Typography:** follows Eleforge's short type scale. Sizes:
  - 16px titles;
  - 10px uppercase section labels;
  - 14px reading text (goal, summary, decision questions);
  - 12px metadata;
  - 10px chips;
  - 11px monospace with aligned digits for ids, digests, paths and logs.

  Emphasis comes from weight and colour, not new sizes.
- **Run panel:** decision cards with one button per decision plus a feedback box, and a signal form for
  wait nodes. Also progress, goal, inputs and the approved contract.
- **Node panel:** click a node for its attempts. Each shows the agent's summary and rationale, outputs,
  files and images, and Slurm or agent logs. Rerun and cancel are here too. The plan change that added
  a node is named.
- **Timeline and Plan history:** the timeline has a filter. The plan history shows every generation with
  its diff, rationale and approver.
- **Links:** `#node=<id>` and `#tab=<tab>` deep links. `?theme=dark|light` picks the theme.

Screenshots of the demo run are in [`docs/demo/`](docs/demo/), as `ui-*.png`.

Everything it shows comes from the journal. Every action goes through the same engine calls as the CLI
and is recorded under your name. It binds to `127.0.0.1` and needs a per-start token (shared login nodes
are safe). It only serves files that belong to the run, and works offline, with no CDN.

## Driving flower from a coding agent

```bash
flower skill install claude      # or: codex | agents | project | all
```

The skill teaches the agent the loop: draft → validate → show the user → **the user approves** →
run detached → `flower wait --json` → relay decisions → rerun or amend → report. Every command takes
`--json` and returns `{ok, data, error, next}`. Exit codes are `0` succeeded, `1` failed, `2` usage
error, `3` needs a decision or still running. `flower mcp` serves the same verbs over MCP. Approval
verbs are deliberately not exposed there, so an agent cannot approve its own plan.

Agent steps run the real harness CLIs (`claude -p`, `codex exec --json`, `pi --mode json`) in a
dedicated work directory. flower records the exact argv, the session id, the transcript, tokens and
cost. It validates the agent's final JSON against the declared outputs and uses up to N repair turns on
the same session if it doesn't match.

## HPC and remote machines

`job` nodes submit with `sbatch` (locally or over `ssh` with your `~/.ssh/config` aliases; automation never
opens a ControlMaster). Submission is idempotent: a write-ahead intent, a deterministic job name, a remote
job-id file and an in-job duplicate guard mean a crash at any point re-attaches instead of submitting
twice. Polling batches `squeue` → `sacct` → evidence files, tolerates sacct lag and MinJobAge, and
reports typed failures (`timeout`, `oom`, `node_fail`, `preempted`, `exit_nonzero`, `lost`). Try it without
a cluster: `flower fake-slurm ./fs` installs a local fake Slurm.

A machine without a batch system works too: `scheduler: none` runs the payload as a detached process
straight on the host (over `ssh`, in that machine's own login environment), with the same idempotent
launch, batched checks, time limit, cancel of the whole process tree and `lost` detection. Any `shell`
or `function` node can run on a cluster or remote machine by adding `cluster: <name>`. A function's
local module is shipped with it and runs in the remote Python. See the
[guide](docs/GUIDE.md#a-machine-without-a-batch-system-scheduler-none).

## Developing inside the run

Start the run with a rough plan. Then fix steps where they fail:
```bash
flower rerun RUN STEP --follow     # picks up edits to plan.yaml (a recorded amendment), reruns STEP, streams it
```
`policies: {edits: unfinished}` lets edits to steps that haven't succeeded apply at once; edits to
finished results still ask for approval. The run's log records how the workflow was made to work, and
the finished run is the result. See [the guide](docs/GUIDE.md#3-developing-a-workflow-inside-its-run).

## Software on remote machines

flower does not decide how software gets installed. An agent explores the target, through the logged
`flower remote exec`, and writes a small recipe: `envs/<name>/setup.sh`, `activate.sh` and `check.sh`,
plus any lock files it wants. `flower env freeze` content-hashes and versions the recipe, and
`flower env replay --fresh` proves it works from scratch. Steps then say `environment: <name>`: a
generated step checks the environment on the cluster, installs it if missing, and the steps run with it
activated. A changed recipe installs into a new prefix and invalidates cached results. See
[`docs/ENVIRONMENTS.md`](docs/ENVIRONMENTS.md); the ABACUS benchmark's `envs/abacus/` is a real example.

## Example: an end-to-end study, run for real

[`examples/catalyst-screening`](examples/catalyst-screening) is a complete study, and
[`docs/demo/`](docs/demo/) holds the record of one real run of it (2026-10-03).

**The study.**
1. A **Codex agent** chooses candidate metals.
2. **Slurm jobs** fit lattice constants and compute O adsorption energies with ASE.
3. A **wait** node receives a reference value from the lab.
4. A second agent ranks the results and **amends the plan** to add a convergence job.
5. A **human reviews** the ranking: "revise", then "approve".
6. A plot and an **agent-written report** finish it.

**Faults injected during the run.**
- An injected `NODE_FAIL` was retried automatically.
- The background driver was killed with `SIGKILL` in the middle of the run. A single `flower tick` from a
  fresh process collected the jobs that had finished in the meantime, and the run continued.

**Outcome.** 17/17 nodes succeeded in 7m56s. Afterwards, `flower fork RUN --from plot` replayed the
whole study in 3 seconds. It reused every recorded result, including the agents' decisions and the job
their amendment added, without calling the LLM again. Only the plot was re-executed, and the new run
asked a human to review it again.

**Record of the run** (in `docs/demo/`). The run was made before the project was renamed from
forgeflow to flower, so these files and screenshots still show the old name (`forgeflow` commands,
`.forgeflow/` paths, `ff-` job names):
- [`final-status.txt`](docs/demo/final-status.txt)
- [`timeline.txt`](docs/demo/timeline.txt)
- [`report.md`](docs/demo/report.md) / [`report.html`](docs/demo/report.html)
- [`agent-REPORT.md`](docs/demo/agent-REPORT.md)
- [`ro-crate-metadata.json`](docs/demo/ro-crate-metadata.json)

## Quality

`pytest tests` runs 664 tests:
- **core:** journal, plan, template, engine, crash injection, CLI;
- **hpc:** a fake Slurm with injected faults, plus crash windows during submit, poll and retrieve;
- **agents:** fake Claude, Codex and pi streams; repair turns; amendment policies; MCP; reports.

Three adversarial test passes found 47 bugs. All are fixed and now covered by regression tests
(`tests/*/BUGS.md` keeps the record).

## Design

[`docs/DEV_PLAN.md`](docs/DEV_PLAN.md) explains every design decision and the mature practice it
borrows: Smithers, Archon, yak, orc, gh-aw, jobflow, AiiDA, DBOS, snakemake-slurm, CatGo, and Eleforge's
LabFlow and Slurm code. The research behind it is in [`context/notes/`](context/notes/).

License: Apache-2.0.
