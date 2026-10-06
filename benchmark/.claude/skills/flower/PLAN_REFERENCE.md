# flower plan reference (flower: 1)

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
clusters:                    # machines a step can run on (`cluster: name`); host details are run inputs
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
    # on a cluster:
    cluster: hpc             # a Slurm job there, or a process on the host with `scheduler: none`
    environment: pyscf       # the frozen software recipe envs/pyscf/ (see below)
    resources: {nodes, ntasks, ntasks_per_node, cpus_per_task, mem, time, partition, account, qos, gpus, extra: [...]}
    stage_in: [{from: local/path, to: name}, {from: "remote:${relax.outputs.job_dir}/CHGCAR", to: CHGCAR, mode: link}]
    retrieve: [glob, ...]    # fetched back next to the step's outputs
```

## Steps
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

## In a step's environment
`FLOWER_OUTPUTS` (write the outputs JSON here, or print it as the last stdout line) · `FLOWER_INPUTS` (the resolved `inputs:` as JSON) ·
`FLOWER_IN_<NAME>` (scalar inputs) · `FLOWER_STATE_DIR`: a directory kept across the *retries* of one start of the
step (write checkpoints here and resume from them when present); a deliberate `flower rerun` or an edit of the
step starts a new, empty one, and `flower rerun --keep-state` continues the last one (a checkpointed job that
reached its time limit; `retry: {on: [timeout]}` retries it automatically) · `FLOWER_JOB_DIR` (cluster steps: this
attempt's directory there) · `FLOWER_CPUS`, `FLOWER_MEM_MB` · `FLOWER_RUN_ID`, `FLOWER_NODE_ID`, `FLOWER_ATTEMPT`.

## References
`${inputs.x}` · `${node.outputs.key.sub}` · `${node.files.name}` · `${node.dir}` · `${node.summary}` ·
`${feedback}` (rework text from a gate) · `${plan.dir}` · `${run.id}` · `${run.dir}` ·
`${item}` / `${index}` (foreach) · `${env.VAR}` · `${step.partial}`: a foreach's items finished so far (null for
the others) *without* waiting for the step, for a preview of a long campaign (rerun the preview to refresh it).
A value that is exactly one reference keeps its type (list, number…). `$${` is a literal `${`.

## Changing a running plan
Edit the plan file, then `flower rerun RUN` (or `flower rerun RUN STEP`, which also runs STEP again with what
depends on it); `flower add` appends a step. Every edit applies at once and is recorded (who, the diff): a step
whose command, inputs or environment changed runs again with its downstream, and its earlier attempts stay in the
record; a description, title, timeout or retry change re-runs nothing. Of an existing cluster only `cpus`,
`max_jobs` and `min_poll` may change; new clusters and inputs may be added.
