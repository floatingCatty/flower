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
flower run study.yaml -i structure=Si.cif     # review the plan, approve, watch it run
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
flower report RUN                             # report.md + report.html
flower ui                                     # web UI: live DAG, details, decisions (prints a URL with token)
```

Every command takes `RUN` as a full id, a unique fragment, or nothing (meaning the latest run).

## 3. Writing good plans

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

## 4. Agents

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

## 5. HPC clusters and remote machines

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

## 6. When something goes wrong

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

## 7. Files you can rely on

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
