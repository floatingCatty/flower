# flower user guide

## 1. Concepts in five minutes

| Concept | What it is | Where it lives |
|---|---|---|
| **Plan** | A YAML contract: inputs, compute clusters, nodes and their dependencies. | your `plan.yaml` |
| **Run** | One execution of a plan with concrete inputs. | `.flower/runs/<run-id>/` |
| **Journal** | Append-only `events.jsonl`. Everything you see is derived from it. | `<run>/events.jsonl` |
| **Node** | One step: `agent`, `shell`, `function`, `job`, `gate`, `wait`. | in the plan |
| **Attempt** | One execution of a node (retries and reruns create new attempts). | `<run>/nodes/<node>/a<N>/` |
| **Gate** | A question for a human. The run parks until it is answered. | `<run>/pending/` |
| **Amendment** | An approved change to a running plan, applied as a new *generation*. | in the journal |
| **Tick** | One short pass that advances a run: collect finished work, start ready work. | `flower tick` |

A run moves through `awaiting_approval → running ⇄ parked → succeeded | failed | cancelled`.
*Parked* means nothing can progress until someone acts: a gate, a signal, or a timer. Exit code 3 always
means "needs you, or still going".

## 2. Everyday use

```bash
flower start "Si band gap with HSE"           # a run from minute one: empty draft plan, UI started
flower add RUN scf --cluster hpc -- 'mpirun pw.x -in scf.in'   # write a step into the plan and run it
flower run study.yaml -i structure=Si.cif     # or: review a whole plan, approve, watch it run
flower run study.yaml --yes --detach          # approve and run in the background
flower status                                 # latest run: table of nodes + what needs you
flower watch RUN                              # live view
flower log RUN                                # narrative timeline (who did what, when)
flower show RUN NODE                          # every attempt: inputs, outputs, files, errors, rationale
flower logs RUN NODE                          # readable agent transcript, Slurm stdout/err, shell output
flower output RUN NODE energy                 # one output value (or a file path)
flower answer RUN GATE approve --text "…"     # answer a decision
flower signal RUN lab-data --data '{"x": 1}'  # feed a wait node
flower rerun RUN NODE                         # redo a node (a foreach: all its items) and its downstream (unchanged steps reused)
flower amend RUN change.yaml                  # propose a plan change (needs approval)
flower cancel RUN [--node NODE]
flower fork RUN --from NODE -y                # new run, reusing all still-valid recorded results
flower compare RUN_A RUN_B --rtol 1e-6        # same results? (a rerun, a fork, a fresh clone: a run dir path works)
flower report RUN                             # report.md + report.html
flower ui                                     # web UI (background, one per project): prints its links
flower open me@host:/path/to/project          # on your laptop: remote project's UI in your browser, one command
```

Every command takes `RUN` as a full id, a unique fragment, or nothing (meaning the latest run).

## 3. Developing a workflow inside its run

You don't need a working plan before you start the run. Start it first, then do every computation as a
step of it, so the run's log records the exploration and the debugging, the user sees it in the UI from
the beginning, and the finished run is the result. There is nothing to redo at the end.

```bash
flower start "Reproduce Fig. 2 of the paper"  # empty draft plan (<id>/plan.yaml) + a run that waits for steps
flower add RUN fetch --out n:integer -- 'python3 fetch.py > "$FLOWER_OUTPUTS"'   # write the step, run it, stream it
flower add RUN fit --needs fetch --cluster box --env pyscf --stage-in fit.py -- 'python3 fit.py'
# fix the code (staged scripts are read at run time) and/or the step in plan.yaml, then:
flower rerun RUN STEP --follow        # applies the plan-file edit, reruns STEP, streams it, exit 0/1
flower rerun RUN NEWSTEP --follow     # a step written into plan.yaml by hand is added the same way
```

Why this shape: agents (and people) drift outside a workflow tool when the early phase has nowhere to go
and when running a command by hand is cheaper than recording it. `start` gives the work a home from the
first minute; `add` makes a recorded step cost one command. A plan written in full up front
(`flower run plan.yaml`) works the same way afterwards.

* **A machine you only know about later.** Add an `inputs:` entry (with `default:`, or give the value
  with `-i NAME=VALUE` on `add` / `rerun`) and a `clusters:` entry to plan.yaml. The next `add` or `rerun`
  picks them up as part of its amendment (ops `add_inputs`, `add_clusters`). Existing inputs and clusters
  stay fixed for the life of the run: a change to one is reported, not applied (`flower fork` for that).
* **Plan-file edits.** `rerun` compares the plan file with the run's plan for STEP, its downstream
  steps (and the generated environment steps they use), and steps that are new in the file. It
  proposes the differences as one amendment, with a diff and a record of who made it. History stays
  immutable: a finished step is superseded, never edited in place.
* **Approval of those edits.** Set by `policies: {edits: ask | unfinished | all}` in the plan:
  * `ask` (default): you approve with `flower approve RUN amend-…`, or pass `--yes` when you are the
    approver;
  * `unfinished`: edits to steps that have not succeeded, and new steps, apply at once (still
    recorded); edits to finished results still ask;
  * `all`: every edit applies at once.
* **`--follow`.** Watches one step: its events, its live output for local steps, then its result or
  its error with the log tail. The rest of the run carries on in the background. While someone
  follows a step, polling drops to 0.5 s.
* **Measured on a real remote machine.** Adding and debugging an ABACUS step this way took about
  27 s per attempt. ABACUS itself took 23 s of that; launch and collection about 1 s each.

### Keeping agents inside the run

`flower init` ships the instructions with the project rather than relying on someone's personal setup:
the skill in `.claude/skills/flower/` and `.agents/skills/flower/`, and a managed block in `AGENTS.md`
(read by Codex and other agents). `flower init --hook` also adds a Claude Code hook in
`.claude/settings.local.json`. When an agent runs a computation (`python script.py`, `mpirun`,
`ssh host cmd`, `julia`, `sbatch`) beside an active run, the hook adds a one-line reminder to its context:
the command was not recorded, and the reminder names the `flower add` line to use instead. It never blocks,
stays quiet for `python -c`, tests and flower's own commands, and speaks at most once per 15 minutes per
session.

### Long jobs: checkpoints that survive retries

Each attempt of a step runs in a directory of its own, so a retried simulation would start from zero. Write
checkpoints to `$FLOWER_STATE_DIR` instead: it is shared by the retries of one start (a lost process, a
timeout, a listed `retry: {on: [...]}` class) and new for a deliberate `flower rerun` or an edited step. A
restartable step looks like `if [ -f "$FLOWER_STATE_DIR/state.chk" ]; then resume; else start; fi`.

### Changing a running campaign without redoing finished work

Some settings do not change what a step computes: `resources`, `timeout`, `retry`, and `tmpdir` (where temporary
files go: `tmpdir: job` puts `TMPDIR` in the attempt's own directory, useful when codes write GB-sized scratch
files and `/tmp` is small or shared). They are outside the cache key. Edit them in plan.yaml and apply with
`flower rerun RUN STEP --cached`: running items finish as they are, items not started yet use the new definition,
and finished items keep their results. (`--cached` also applies while items are still running.) A step can
size itself from `$FLOWER_MEM_MB` and `$FLOWER_CPUS`, exported from its `resources`.

For a few heavy items that need a different setup (more memory, one at a time), add a separate step for just
those items on a new cluster entry (e.g. the same host with `max_jobs: 1`) and merge both in the analysis:
the failures stay in the record, the reruns are explicit.

### Several studies on one machine

Without a batch system nothing arbitrates between runs: three studies each allowed `max_jobs: 6` on a 96-core
workstation can start 160 threads. Give the cluster entry a budget, `cpus: 90`, and the steps their size,
`resources: {cpus_per_task: 16}`: a launch then waits while the machine (transport + host, whatever the entry is
called in each plan) has too many cores in use across all runs of the project. A single step larger than the
budget still runs when the machine is otherwise idle.

### Large campaigns

`flower status` summarises a foreach with more than 12 items on one line (running and failed items stay listed;
`--items` lists all), and a waiting foreach step shows how many items have finished. `flower remote exec --env
NAME --installed` runs in an environment's installed prefix, to try an API before writing a step. The
background driver restarts itself when flower's own code changes, survives transient errors, and `--follow`
restarts a driver that has died.

## 4. Writing good plans

* **Use deterministic nodes for computation and agents for judgement.** Calculations, parsing and
  plotting go in `job`, `shell` and `function` nodes. Choosing parameters, interpreting results and
  writing go in `agent` nodes. Agents can still extend the plan through amendments when they find more
  work is needed.
* **Declare `outputs` for anything downstream uses.** flower validates them. A job that "succeeded"
  without its outputs fails with class `contract`, not silently.
* **Ask agents for a `rationale`.** It is requested automatically and kept as provenance in the
  report, next to the decision it explains.
* **Put human judgement in `gate` nodes** with `on_reject.rerun` for rework loops. The re-run nodes see
  the reviewer's text as `${feedback}`.
* **Make long waits explicit.** Lab data or an external pipeline becomes a `wait` node with
  `signal:` and `deadline:`.
* **Use `foreach` for screening.** One child per item, collected into `${node.outputs.items}`.
  `on_failure: continue` lets the study proceed when some items fail.
* **Use retries for infrastructure only.** By default `lost`, `node_fail`, `preempted`, `transient` and
  `remote` are retried. Add classes such as `exit_nonzero` explicitly if your step is flaky.
* **Run `flower plan validate` until it is clean.** It lists *every* issue with a hint.

## 5. Agents

```yaml
- id: analyse
  kind: agent
  harness: {name: claude, model: sonnet, permission: bypass, tools: {deny: [WebFetch]}, budget_usd: 2}
  prompt: |
    Read ${relax.outputs.job_dir}/OUTCAR and judge whether the relaxation converged properly.
  outputs: {converged: boolean, issues: array}
  effects:
    amend: {auto_approve: true, max_nodes: 2, kinds: [job], ops: [add, detour]}
```

* Harnesses: `claude` (`claude -p`), `codex` (`codex exec`), `pi` (`pi --mode json`), and `script` (any
  executable that reads the prompt on stdin). `command: [path, args…]` overrides the executable.
* The prompt is extended with a *node contract*: inputs, working directory, upstream results, the exact
  JSON to return, and amendment instructions when allowed. Answers that don't match get repair turns on
  the same session (`repair_attempts`, default 2).
* Recorded per attempt: the exact argv (`proc/argv.json`), the session id, the raw stream
  (`proc/stdout.log`), the final answer (`answer.json`), tokens and cost.
* When flower is itself driven by Claude Code, variables tied to the parent session are removed
  before nested `claude -p` runs, so nested runs don't fail with a "nested session" error or with 401.
* Typical failure classes: `auth` (log the harness in), `quota_retry` (retried after the reset time),
  `config` (bad model or flag), `schema_invalid`, `budget`, `timeout`, `idle_timeout`.

## 6. HPC clusters and remote machines

```yaml
clusters:
  hpc:
    transport: ssh            # or local, when flower runs on the login node
    scheduler: slurm          # default; `none` runs the payload directly on the host (no batch system)
    host: myhpc               # an ~/.ssh/config alias; key-based, non-interactive (BatchMode)
    remote_root: ~/flower-runs
    max_jobs: 20              # concurrent jobs from this run
    min_poll: 60s             # be polite to slurmctld
    modules: [vasp/6.4]
    prelude: ["source ~/venv/bin/activate"]
    env: {OMP_NUM_THREADS: "1"}
    resources: {partition: cpu, account: proj123, time: "24:00:00"}
```

* Each attempt runs in `<remote_root>/<run>/<node>/a<N>/`. The payload is `user.sh`, wrapped by a
  generated `job.sh` that records the start, the exit code and the owner.
* No daemon is needed. To advance runs while you are away, add a cron entry (or `scrontab` on the
  cluster):
  `*/5 * * * * cd /path/to/project && flower tick --all >> .flower/cron.log 2>&1`
* For MFA sites, open a ControlMaster yourself (`ssh -fNM myhpc`); flower reuses it after
  `ssh -O check`. It never starts one.
* Hand files from one job to the next on the cluster with
  `stage_in: [{from: "remote:${prev.outputs.job_dir}/charge-density", to: charge-density, mode: link}]`.

### A machine without a batch system (`scheduler: none`)

For a workstation or server you can `ssh` into:

```yaml
clusters:
  box: {transport: ssh, host: mybox, scheduler: none, remote_root: ~/flower-runs,
        prelude: ["conda activate abacus"]}
nodes:
  - {id: scf, kind: job, cluster: box, script: "mpirun -np 16 abacus > abacus.out"}
  - {id: parse, kind: shell, cluster: box, run: "python parse.py > \"$FLOWER_OUTPUTS\""}
  - {id: fermi, kind: function, cluster: box, call: "abacus_si:fermi_from_dos", python: python3,
     args: {out_dir: "${scf.outputs.job_dir}/OUT.si"}}
```

* The payload starts as a detached process (`nohup setsid`) from the host's login shell, so it sees that
  machine's own environment: `PATH`, conda, modules, plus `prelude`. Nothing stays connected while it
  runs; each tick checks it in one `ssh` call (`min_poll`, default 10 s).
* `job_id` is the process id. Logs are `job.out` / `job.err` in the attempt directory.
* `resources.time` (or the node's `timeout.total`) is enforced with `timeout`: class `timeout`.
* A process that disappears without an exit code (killed, machine rebooted) becomes `lost` after
  `lost_after` (default 60 s) and is retried per the node's retry policy.
* `flower cancel` sends TERM, then KILL, to the payload's whole process group (mpirun and its ranks).
* Any `shell` or `function` node can run there too: give it `cluster: box`. A `function` node's local
  module (next to the plan or on `pythonpath`) is copied along and run with the remote `python:`.
* A path in one node's outputs is a path on the machine where that node ran. A local node reading
  `${scf.outputs.job_dir}` from a remote node gets a remote path: keep the consumer on the same cluster,
  or fetch the files with `retrieve:` / `files:`.

## 7. When something goes wrong

| You see | Meaning | Do |
|---|---|---|
| `contract` | finished but outputs or files don't match the declaration | `flower show RUN NODE`, then fix the script or the declaration |
| `exit_nonzero` | the payload failed | `flower logs RUN NODE`, fix it, `flower rerun RUN NODE` |
| `timeout` / `oom` | Slurm (or `scheduler: none` time) limit hit | raise `resources.time` / `mem` via an amendment, then rerun |
| `node_fail` / `preempted` / `lost` | infrastructure; retried automatically | nothing, unless retries run out |
| `remote` | the cluster was unreachable for a long time | check `ssh host`; `flower rerun` |
| `auth` | harness not logged in | log the CLI in (`claude /login`, `codex login`), then rerun |
| `schema_invalid` | the agent never produced the declared JSON | tighten the prompt or relax `outputs` |
| run `parked` | waiting for a gate, signal or timer | `flower status` shows exactly which, and the command to run |

## 8. Files you can rely on

```
.flower/runs/<run>/
  events.jsonl          the journal; never edit
  plan.yaml             approved base plan (human-readable copy)
  plan.current.yaml     plan after amendments
  pending/              open questions (*.request.json); answers can be dropped here as *.answer.json
  signals/              drop *.json files here to send signals ({"name": …, "data": …})
  nodes/<node>/a<N>/    work/ (cwd), proc/ (argv, logs, exit), inputs.json, answer.json, job/
  report.md / .html     generated by `flower report`
```
