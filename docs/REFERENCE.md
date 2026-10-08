# flower reference

Everything flower accepts: the commands, the plan file, and software environments. How a team works with it is
in [USING.md](USING.md).

## Commands

Every command takes `--json` (one object: `{ok, data, error, next}`) and `--actor NAME`. Every command returns at
once (a background driver carries the run on); `--follow` waits. Exit codes: `status`, and any `--follow`, exit
0 when the run (or step) succeeded, 1 when it failed, 3 while it is still running or waits for a decision; the
other commands exit 0 when done; any command exits 2 on an error (usage, invalid plan, unknown run).

| command | what it does |
|---|---|
| `flower init [DIR] [--hook] [--skill T]` | make a project: `.flower/` (gitignored), the agent skill, an `AGENTS.md` block, optionally a Claude Code hook; re-running it refreshes them |
| `flower start "GOAL"` | a run now, with an empty draft plan that grows step by step |
| `flower add RUN ID [options] -- CMD` | write a step into the run's plan file and run it: `--description`, `--needs`, `--out NAME:TYPE`, `--file NAME=PATH`, `--in NAME=VALUE`, `--cluster`, `--env`, `--stage-in`, `--foreach`; any other key with `--set KEY=VALUE` (YAML value, dotted key); `--gate MESSAGE` for a person's decision instead of a command |
| `flower rerun RUN [STEP] [--only] [--keep-state]` | apply the plan file's edits; with STEP, run it again with what depends on it (`--only`: STEP alone) |
| `flower run PLAN [-i K=V] [--inputs F] [--machine ROLE=NAME] [--review] [--reuse RUN [--rerun-from STEP]]` | a run of a plan file; `--machine` runs a protocol's cluster ROLE on your machine NAME; `--review` waits for `flower approve RUN` first; `--reuse` takes an earlier run's still-valid results and its inputs |
| `flower status [RUN] [--follow [--timeout S]]` | without RUN: the project's runs; with it: where the run is and what needs you (one scheduling pass, a background driver if needed) |
| `flower show RUN [STEP [KEY] \| GATE] [--logs [--attempt N]]` | a run, a step (attempts, outputs, errors; `--logs`: its raw output), one output value or file path, or a gate |
| `flower log RUN [--node STEP] [-f] [--note TEXT]` | every event, actor and decision, in order; `--note` adds one |
| `flower approve RUN [GATE] [DECISION] [--note T]` / `flower reject RUN [GATE] [--note T]` | answer the plan (`run --review`) or a gate; a rejection's note is what a rework step reads as `${feedback}` |
| `flower cancel RUN [--node STEP]` | cancel the run or one step |
| `flower export RUN STEP…` | the steps behind STEP as a protocol: `protocol.yaml` (with the environment versions pinned), `expected.json`, `PROTOCOL.md` next to the plan |
| `flower compare A B` | do two runs (or a run and an `expected.json`) agree within `--rtol`/`--atol`? |
| `flower ui [start\|status\|stop\|user@host:/path]` | the project's web UI (background, stable port); `host:/path` opens another machine's project through ssh |
| `flower remote add\|list\|check\|login\|exec` | the machines work runs on, set up once per person: see [Machines](#machines) |
| `flower env new\|freeze\|replay\|check\|show NAME` | software recipes, see [Environments](#environments) |
| `flower plan validate\|show PLAN` | every problem of a plan file with a fix hint / a readable overview |

## The plan file (`flower: 1`)

A plan is a YAML file of steps. `flower plan validate plan.yaml` lists every problem with a fix hint.

```yaml
flower: 1                    # required
id: my-study                 # required: [A-Za-z0-9_.-]
title: Human title
description: |               # optional: the goal at more length, shown at the top of the UI
  What this workflow is for.
inputs:                      # given at `flower run plan.yaml -i name=value` (or --inputs file.json)
  structure: {type: path, required: true, description: POSCAR file}
  strain:    {type: number, default: 0.01}
defaults:
  timeout: {total: 2h, idle: 30m}          # killed if exceeded (on a cluster: the job's time limit; idle = no
                                           # output, local steps only)
  retry:   {max_attempts: 2, backoff: 30s} # retries infrastructure failures (lost, node_fail, ...)
  concurrency: 4                           # max local processes at once
clusters:                    # optional: a step's `cluster:` names a machine of `flower remote list` (the person's
                             # machines file; the run records its settings). Declared here instead, a plan carries
                             # its own; host details are then run inputs:
  hpc:  {transport: ssh, host: "${inputs.host}", ssh_options: "${inputs.ssh_options}", max_jobs: 20,
         min_poll: 60s, modules: [vasp/6.4], prelude: ["source ~/env.sh"], resources: {partition: cpu, account: abc}}
                             # work goes to remote_root (default ~/flower-runs) on that machine
  here: {transport: local}   # Slurm on the machine flower runs on
  box:  {transport: ssh, host: mybox, scheduler: none, cpus: 32}   # no batch system: run directly on the host;
                             # cpus: the cores flower may use there, shared by all runs of the project
                             # (each step counts resources.cpus_per_task, default 1). Every step on a budgeted
                             # cluster waits for it: give quick checks their own entry for the same host
                             # without `cpus` (e.g. here: {transport: local, scheduler: none})
                             # install: never  -> environment steps only check, never run setup.sh
environments: {pyscf: "sha256:..."}   # optional pins of recipe versions (`flower export` writes them)
nodes:                       # `nodes: []` is a valid draft (`flower start`): the run parks until steps are added
  - id: name                 # unique; letters, digits, - _
    kind: shell              # optional: shell (a command, the default) | gate (a person's decision)
    title: optional label
    description: |           # what the step establishes and how to read its result (shown first in the UI;
                             # a missing one warns; editing it never re-runs the step)
    run: python3 ${plan.dir}/fit.py   # bash (set -euo pipefail). Outputs: a JSON object written to
                             # $FLOWER_OUTPUTS, or printed as the last line of stdout
    needs: [other]           # explicit dependencies (references ${x...} add edges automatically)
    when: "${scan.outputs.n} > 0"           # optional condition; false -> skipped
    inputs: {k: "${other.outputs.key}"}     # resolved values, as JSON in $FLOWER_INPUTS
    outputs: {energy: number, converged: boolean}   # declared result contract (validated)
    files: {report: report.md}              # declared files (relative to the step's directory), hashed
    retry: {max_attempts: 3, on: [lost, node_fail, exit_nonzero]}
    timeout: {total: 1h}
    on_failure: continue     # steps after it still run (e.g. an analysis of a fan-out with some failed items)
    foreach: "${scan.outputs.items}"        # fan-out: one item per element; ${item}, ${index}
    env: {OMP_NUM_THREADS: "4"}             # environment variables
    tmpdir: job              # TMPDIR inside the attempt's directory (or a path): scratch files off /tmp
    # on a machine:
    cluster: narval          # a machine (`flower remote list`) or a `clusters:` entry: a Slurm job there, or a
                             # process on the host with `scheduler: none`
    environment: pyscf       # the frozen software recipe envs/pyscf/ (see below)
    resources: {nodes, ntasks, ntasks_per_node, cpus_per_task, mem, time, partition, account, qos, gpus, extra: [...]}
    stage_in: [{from: local/path, to: name}, {from: "remote:${relax.outputs.job_dir}/CHGCAR", to: CHGCAR, mode: link}]
    retrieve: [glob, ...]    # fetched back next to the step's outputs
```

### Steps
* **shell** — `run:` is a bash script. With `cluster:` it runs in its own attempt directory on that machine,
  as a Slurm job or (`scheduler: none`) a detached process; nothing is held while it runs. Outputs then also
  include `job_id`, `job_dir` (on the cluster) and `local_dir` (where `files:` / `retrieve:` were fetched on
  this machine: what a local analysis step should read).
* **environment** — `environment: NAME` uses the frozen recipe `envs/NAME/` (setup.sh / activate.sh /
  check.sh). A generated step `env-NAME-<cluster>` checks it there (installs it if
  missing) before the step, which runs with it activated. Without `cluster:` the step runs on this machine
  with it (the implicit cluster `local`). Recipes are made with `flower env new|freeze|replay` and
  `flower remote exec --env`.
* **gate** — a person's decision: `message:` (templated), `decisions: [approve, reject]`,
  `on_reject: {rerun: [node, ...], max_attempts: 3}` — a rework loop: the re-run steps see the reviewer's
  text as `${feedback}`. Outputs `{decision, text, by}`.

### In a step's environment
`FLOWER_OUTPUTS` (write the outputs JSON here, or print it as the last stdout line) · `FLOWER_INPUTS` (the resolved `inputs:` as JSON) ·
`FLOWER_IN_<NAME>` (scalar inputs) · `FLOWER_STATE_DIR`: a directory kept across the *retries* of one start of the
step (write checkpoints here and resume from them when present); a deliberate `flower rerun` or an edit of the
step starts a new, empty one, and `flower rerun --keep-state` continues the last one (a checkpointed job that
reached its time limit; `retry: {on: [timeout]}` retries it automatically) · `FLOWER_JOB_DIR` (cluster steps: this
attempt's directory there) · `FLOWER_CPUS`, `FLOWER_MEM_MB` · `FLOWER_RUN_ID`, `FLOWER_NODE_ID`, `FLOWER_ATTEMPT`.

### References
`${inputs.x}` · `${node.outputs.key.sub}` · `${node.files.name}` · `${node.dir}` · `${node.summary}` ·
`${feedback}` (rework text from a gate) · `${plan.dir}` · `${run.id}` · `${run.dir}` ·
`${item}` / `${index}` (foreach) · `${env.VAR}` · `${step.partial}`: a foreach's items finished so far (null for
the others) *without* waiting for the step, for a preview of a long campaign (rerun the preview to refresh it).
A value that is exactly one reference keeps its type (list, number…). `$${` is a literal `${`.

### Changing a running plan
Edit the plan file, then `flower rerun RUN` (or `flower rerun RUN STEP`, which also runs STEP again with what
depends on it); `flower add` appends a step. Every edit applies at once and is recorded (who, the diff): a step
whose command, inputs or environment changed runs again with its downstream, and its earlier attempts stay in the
record; a description, title, timeout or retry change re-runs nothing. Of an existing cluster only `cpus`,
`max_jobs` and `min_poll` may change; new clusters and inputs may be added.


## Machines

Where work runs is set up once per person, not per plan: `~/.flower/machines.yaml` (`$FLOWER_MACHINES`
overrides), outside every repository. You give how to reach a machine; flower probes the rest.

| command | what it does |
|---|---|
| `flower remote add NAME TARGET [-i KEY] [--login] [--cores N] [--note TEXT]` | TARGET: an `~/.ssh/config` alias, `user@host[:port]`, or `local`; `--login` when it asks for a password or a code (a shared connection, opened here first). Only a machine flower reached is saved. Probes the scheduler (Slurm or none), cores, memory, GPUs and load, Slurm partitions (time limits, node sizes, GPUs) and your accounts, a scratch directory (work and environments go there when there is one), the tools there (module, conda, apptainer, …) and free space; writes the entry |
| `flower remote list [NAME]` | the machines, what they have, and what agents may use (`--json` is what agents read) |
| `flower remote check [NAME]` | reach a machine (or all) and refresh what was probed |
| `flower remote login NAME [--hours H]` | a password or a second factor: open the shared ssh connection again (you type it once); flower reuses it while it is alive |
| `flower remote exec --cluster NAME [--run RUN] -- CMD` | a command there (noted in the run's log with `--run`) |

```yaml
narval:
  ssh: narval                     # what you give
  note: ask before using more than 256 cores        # optional, for agents and people
  agent_may_use: {cores: 256, hours: 24}            # optional: what an agent may take without asking
  account: def-xyz                # optional: anything written beside `probed:` wins over it
  probed: {scheduler: slurm, partitions: [...], accounts: [def-xyz], work_dir: /scratch/me/flower-runs,
           env_dir: /scratch/me/flower-envs, tools: [sbatch, module, apptainer], ...}
```

A step names a machine as `cluster: NAME`; the run records the settings it used (no keys or passwords: those stay
with ssh). On a machine without a scheduler, flower shares out `agent_may_use.cores` (else all its cores) among
the project's runs. An exported protocol keeps its own cluster names: `flower run protocol.yaml --machine
remote=narval` runs the steps of `remote` on your `narval` (the protocol's host inputs are then not needed).

## Environments

flower does not decide how software gets installed: an agent explores the target (through the logged
`flower remote exec`) and writes a small recipe. flower guarantees one thing: **the recipe that worked is frozen
(content-hashed) and replayed the same way**, and the steps that need it run with it activated.

### The recipe: `envs/<name>/`, next to the plan (or in a parent directory)

| file | written by | contract |
|---|---|---|
| `setup.sh` | the agent | installs into `$FLOWER_ENV_PREFIX`, which does not exist yet; runs with `bash -e` in a copy of the recipe (`$FLOWER_ENV_DIR`), non-interactive |
| `activate.sh` | the agent | sourced before every step that uses the environment; only *exported* variables reach the step (a container wrapper is a script on PATH, not a shell function) |
| `check.sh` | the agent | exits 0 if and only if the environment works; prints the versions relied on (recorded) |
| any other file | the agent | lock files, patches, data the setup needs; all hashed into the recipe |
| `env.yaml` | the agent (optional) | `description`, `setup_timeout` |
| `FROZEN.json` | flower | the hash of every file, who froze it and when; git keeps the earlier versions |

On a target, version `<hash>` installs into `<env_dir>/<name>-<hash12>`: the machine's `env_dir` (on scratch
when probing found one; default `~/.flower/envs`). **A changed recipe gets a new
prefix**, so an environment that results were produced with is never modified.

### The commands

| command | what it does |
|---|---|
| `flower env new NAME` | `envs/NAME/` with commented template scripts |
| `flower remote exec --cluster C --env NAME [--run RUN] -- CMD` | a command on the machine (or run RUN's cluster), in an exploration prefix, with `activate.sh` sourced once it exists; noted in the run's log |
| `flower env freeze NAME` | hash every file into `FROZEN.json` |
| `flower env replay NAME --run RUN --cluster C [--fresh]` | run `check.sh`, else `setup.sh` then `check.sh`; `--fresh` into a new empty prefix: proof that the recipe works from scratch |
| `flower env check NAME --run RUN --cluster C` | `check.sh` only |
| `flower env show [NAME]` | state (draft / frozen / changed), hash, when frozen |

### In a plan

A step names `environment: NAME` (without `cluster:` it runs on this machine, on the implicit cluster
`local`). flower adds **one generated step per (environment, cluster)**, e.g. `env-abacus-box`: it runs
`check.sh` (and `setup.sh` when that fails), and is never served from cache, so a deleted environment is noticed.
Steps that use it depend on it and get its activation in their definition, so **a new recipe version changes
their cache key**. Validation refuses a recipe that is missing, never frozen, edited since it was frozen, or not
the version the plan's `environments:` pins (as an exported protocol does), and names the command to run.
`install: never` on a cluster makes the generated step only check.

Whether a result is bit-identical depends on what the recipe pins (an explicit conda list with checksums, an
image digest, exact versions); flower's part is that the same frozen recipe is always run the same way, into its
own prefix, and checked.
