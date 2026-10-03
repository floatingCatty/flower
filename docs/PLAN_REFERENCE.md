# flower plan reference (flower: 1)

A plan is a YAML contract. `flower plan validate plan.yaml` lists every problem with a fix hint.

```yaml
flower: 1                 # required
id: my-study                 # required: [A-Za-z0-9_.-]
title: Human title
description: |               # the goal, shown at approval and in the report
  What this workflow is for.
inputs:                      # given at `flower run plan.yaml -i name=value`
  structure: {type: path, required: true, description: POSCAR file}
  strain:    {type: number, default: 0.01}
policies:
  edits: unfinished          # plan-file edits picked up by `flower rerun`: ask (default) | unfinished | all
defaults:
  harness: {name: claude, model: sonnet}   # default for agent nodes
  timeout: {total: 2h, idle: 30m}          # killed if exceeded (idle = no output)
  retry:   {max_attempts: 2, backoff: 30s} # retries infrastructure failures (lost, node_fail, …)
  concurrency: 4                           # max local processes (shell/function/agent) at once
clusters:                    # for job nodes, and shell/function nodes with `cluster:`
  hpc:  {transport: ssh, host: myhpc, remote_root: ~/flower-runs, max_jobs: 20, min_poll: 60s,
         modules: [vasp/6.4], prelude: ["source ~/env.sh"], resources: {partition: cpu, account: abc}}
  here: {transport: local}   # flower runs on the login node itself
  box:  {transport: ssh, host: mybox, scheduler: none}     # no batch system: run directly on the host
                             # install: never  -> environment steps only check, never run setup.sh
nodes:                       # `nodes: []` is a valid draft (`flower start`): the run parks until steps are added
  - id: name                 # unique; letters, digits, - _
    kind: shell|function|agent|job|gate|wait
    title: optional label
    needs: [other]           # explicit dependencies (references ${x...} add edges automatically)
    when: "${scan.outputs.n} > 0"           # optional condition; false -> skipped
    trigger: all_success     # all_success (default) | all_done | any_success
    inputs: {k: "${other.outputs.key}"}     # resolved values: $FLOWER_INPUTS / FLOWER_IN_K / prompt contract
    outputs: {energy: number, converged: boolean}   # declared result contract (validated)
    files: {report: report.md}              # declared files (relative to the node dir), hashed
    retry: {max_attempts: 3, on: [lost, node_fail, exit_nonzero]}
    timeout: {total: 1h}
    cache: true              # reuse result when definition+inputs unchanged (default true; agents false)
    on_failure: continue     # downstream proceeds, run does not fail on this node
    foreach: "${scan.outputs.items}"        # fan-out: one child per item; ${item}, ${index}
    env: {OMP_NUM_THREADS: "4"}
    cwd: some/dir            # default: a fresh per-attempt work dir
    tmpdir: job              # TMPDIR in the attempt's directory (or a path); not part of the cache key
```

## Node kinds
* **shell** — `run: |` bash script (`set -euo pipefail`). Write outputs as JSON to `$FLOWER_OUTPUTS`.
  Add `cluster: name` to run it on that cluster instead (as a Slurm job, or directly on the host with
  `scheduler: none`), in its own attempt directory there; `stage_in`, `retrieve`, `resources`, `modules`,
  `prelude` work as for `job`.
* **environment** — on any node that runs on a cluster: `environment: abacus` uses the frozen recipe
  `envs/abacus/` (setup.sh / activate.sh / check.sh, see `docs/ENVIRONMENTS.md`). A generated step
  `env-abacus-<cluster>` checks it there (installs it if missing) before the node, which runs with it
  activated. Recipes are made with `flower env new|freeze|replay` and `flower remote exec --env`.
* **function** — `call: package.module:function`; kwargs = `args:` (or `inputs:`); returns a dict.
  `python: /path/to/python` to use another environment; `pythonpath: [dir]`. The cache key includes the
  source of the called module (its whole top-level package) when it lives on `pythonpath` or next to the
  plan, so editing the code invalidates cached and forked results; installed libraries are not tracked.
  With `cluster: name` the call runs on that cluster: the local module (or package) is shipped with it,
  `python:` is the interpreter *there* (default `python3`), and `ctx["workdir"]` is the remote attempt
  directory. A module that is not local must already be importable in the remote environment.
* **agent** — `prompt: |` (or `prompt_file:`), `harness: {name: claude|codex|pi|script, model, effort,
  permission: bypass|edits, tools: {allow: [...], deny: [...]}, budget_usd, command: [...]}`, `system:`.
  The agent must end with a JSON object matching `outputs` + `summary` + `rationale`; invalid answers
  get `repair_attempts` (default 2) correction turns. `effects: {amend: {auto_approve: true, max_nodes: 3,
  kinds: [shell, job], ops: [add, detour]}}` lets it propose plan changes (otherwise not allowed).
* **job** — batch job on a cluster: `cluster:`, `script: |` (payload, `set -eo pipefail`), `resources: {nodes,
  ntasks, ntasks_per_node, cpus_per_task, mem, time, partition, account, qos, gpus, extra: [...]}`,
  `stage_in: [{from: local/path, to: name}, {from: "remote:${relax.outputs.job_dir}/CHGCAR", to: CHGCAR, mode: link}]`,
  `retrieve: [glob, ...]`. Write `outputs.json` (`$FLOWER_OUTPUTS`) in the job dir. Outputs always include
  `job_id`, `job_dir` (on the cluster) and `local_dir` (where the declared `files:` / `retrieve:` were fetched
  on this machine — what a local analysis node should read). Parks while queued/running — no process is held. On a `scheduler: none`
  cluster the payload is a detached process on the host (`job_id` is its PID, logs are `job.out` /
  `job.err`); `resources.time` (else the node's `timeout.total`) is enforced; a process that dies without
  an exit code is `lost`; cancel stops its whole process tree.
* **gate** — human decision: `message:` (templated), `decisions: [approve, reject]`,
  `on_reject: {rerun: [node, ...], max_attempts: 3}` — a rework loop: the re-run nodes see the
  reviewer's text as `${feedback}` (empty on the first pass).
  Outputs `{decision, text, by}`.
* **wait** — `signal: name` (send with `flower signal RUN name --data '{...}'` or a file in
  `signals/`), `timer: 10m`, `deadline: 2d` (expiry succeeds with `expired: true`).

## In a step's environment
`FLOWER_OUTPUTS` (write the outputs JSON here) · `FLOWER_INPUTS` (the resolved `inputs:` as JSON) ·
`FLOWER_IN_<NAME>` (scalar inputs) · `FLOWER_STATE_DIR`: a directory kept across the *retries* of one start of the
step (write checkpoints here and resume from them when present); a deliberate `flower rerun` or an edit of the
step starts a new, empty one · `FLOWER_JOB_DIR` (cluster steps: this attempt's directory there) ·
`FLOWER_RUN_ID`, `FLOWER_NODE_ID`, `FLOWER_ATTEMPT`.

## References
`${inputs.x}` · `${node.outputs.key.sub}` · `${node.files.name}` · `${node.dir}` · `${node.summary}` ·
`${feedback}` (rework text from a gate) · `${plan.dir}` · `${run.id}` · `${run.dir}` ·
`${item}` / `${index}` (foreach) · `${env.VAR}`.
A value that is exactly one reference keeps its type (list, number…). `$${` is a literal `${`.

## Amendments (change a running plan)
`flower amend RUN change.yaml` with
```yaml
rationale: why the plan must change
ops:
  - {op: add, nodes: [{id: extra, kind: shell, needs: [a], run: "..."}]}
  - {op: detour, after: relax, nodes: [{id: kconv, kind: job, ...}]}   # inserted before relax's children
  - {op: replace, node: pending-node, with: {kind: shell, run: "..."}}
  - {op: replace, node: done-node, supersede: true, with: {...}}       # re-runs it + downstream
  - {op: stop, nodes: [pending-node]}
  - {op: drop, nodes: [pending-node]}
  - {op: set_needs, node: n, needs: [a, b]}
  - {op: add_clusters, clusters: {box: {transport: ssh, host: mybox, scheduler: none}}}   # new names only
  - {op: add_inputs, inputs: {host: {type: string, default: mybox}}}                       # value as default
```
History is immutable: finished nodes can only be superseded, never edited in place.
