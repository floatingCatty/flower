"""The plan reference and the skill that teaches coding agents to work in flower."""
from __future__ import annotations

from pathlib import Path

PLAN_REFERENCE = r"""# flower plan reference (flower: 1)

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
"""

SKILL_MD = r"""---
name: flower
description: Run multi-step computational work (shell commands, Slurm/HPC jobs, human approvals) as a recorded, re-runnable workflow with the `flower` CLI. Use when work has several steps, runs for hours to days, must survive session restarts, needs HPC jobs or human sign-off, or should end as a protocol others can re-run. Not for one-off quick tasks.
---

# flower — long computational work, recorded as you go

flower keeps the *state* of long work outside your context: a plan file of steps (each with a description)
and an append-only journal of what ran. A person can follow it in `flower ui`; any new session picks it up with
`flower status`; a finished study becomes a protocol anyone re-runs with one command.

## Rule 0
If the environment variable `FLOWER_INSIDE_RUN` is set, you are *inside* a flower step. Do the step's task;
never run flower commands from inside a step.

## The loop: the run first, then everything as steps
**Start the run before you explore.** `flower start "<goal>"` creates an empty draft plan (`<id>/plan.yaml`)
and a run, and starts the project's UI so the user can watch. Nothing is too early to be a step: downloading
inputs, the first quick test, a parameter probe.
- **One command per step:** `flower add RUN ID --description D [--needs X] [--out NAME:TYPE] [--file NAME=PATH]
  [--cluster C --env E --stage-in FILE] [--set KEY=VALUE] [--follow] -- <command>` writes the step into
  plan.yaml and runs it; `--follow` watches it and prints its result. Outputs: the command prints a JSON object as
  its last stdout line (or writes it to `$FLOWER_OUTPUTS`). Any other step key goes through `--set` (YAML value,
  dotted keys): `--set retry=3 --set timeout.total=2h --set resources.cpus_per_task=8 --set retrieve=[out/*]`.
- **Describe every step** (`--description`, one or two sentences): what it establishes, and how to read its
  result (which output or file answers the question, what a good value looks like). The run is read by people
  who were not there; the UI shows the description first. Missing ones only warn; add them later in plan.yaml
  and `flower rerun RUN` (a description edit re-runs nothing).
- **Fix and repeat:** edit the code or the step in plan.yaml, then `flower rerun RUN ID --follow`. `flower rerun
  RUN` applies plan edits (a new step, a description, a cluster's `cpus`). Every edit applies at once and is
  recorded; a changed finished step runs again, its earlier result stays in the record.
- **A person's decision in the plan:** `flower add RUN ID --gate "<what to decide, with the evidence>" --needs X`
  (`--set on_reject={rerun: [X]}` sends work back with the person's note as `${feedback}`).
- **A new machine later:** add an `inputs:` entry (value with `-i NAME=VALUE`) and a `clusters:` entry to
  plan.yaml; the next add/rerun picks them up.
- **Explore a remote host** with `flower remote exec --run RUN --cluster C -- <cmd>` (logged), not raw ssh.
- Reading papers, files and results directly is fine. *Running* computations beside the run is not: they are
  unrecorded and invisible to the user. That includes the quick check whose answer you rely on: make it a
  one-line `flower add` step. If a hook reminds you, move the work into a step.

**Software on a cluster:** make an environment recipe instead of hand-written preludes: `flower env new NAME`;
explore with `flower remote exec --run RUN --cluster C --env NAME -- <cmd>` (logged in the run); write envs/NAME/setup.sh
(install into $FLOWER_ENV_PREFIX, pin exact versions), activate.sh and check.sh; `flower env freeze NAME`; prove
it with `flower env replay NAME --run RUN --cluster C --fresh`; then `--env NAME` on the steps. Ask the user
before installing anything on a machine they share with others.

**A plan written in full up front** (a known workflow) is fine too: `flower plan validate plan.yaml --json`
until clean, show it to the user (`flower plan show plan.yaml`), and run it (`flower run plan.yaml`) once they
agree. `flower run plan.yaml --review` instead leaves the start to them: it waits for `flower approve RUN`.

## While it runs
- `flower status RUN --json` — what is done, running, failed, and what needs the user (one pass; starts a
  background driver if needed). `flower status RUN --follow --timeout 600` blocks until it finishes or needs a
  decision (run it as a background task if your harness supports it; don't poll in a tight loop). Both exit 0
  succeeded, 1 failed, 3 still running or waiting for a decision; any command exits 2 on an error.
- **A decision** (`open_gates`): `flower show RUN GATE`, relay it to the user in plain words, and answer with
  theirs: `flower approve RUN GATE [DECISION] --note "<their reason>"` or `flower reject RUN GATE --note
  "<what to change>"`. **The decision is the user's. Never answer on your own judgement.**
- **A failure:** `flower show RUN STEP` (error, stderr, outputs written before failing), `flower show RUN STEP
  --logs`, fix the cause, `flower rerun RUN STEP` (it and everything downstream; unchanged steps are reused).
- **The user watching:** `flower ui --json` starts or reuses the project's UI; give them its links (the local
  one, the `ssh -N -L …` line, or `flower ui user@host:/path` if flower is on their laptop).

## When it is done
`flower show RUN STEP KEY` prints one result. `flower export RUN STEP` turns the steps behind STEP into
`protocol.yaml` + `expected.json` + `PROTOCOL.md` next to the plan: anyone reproduces it with
`flower run protocol.yaml --follow` and checks with `flower compare RUN expected.json`. "The run was started" is
not "the run succeeded": check the final status before you report.

## Coming back later (new session)
`flower status` (the project's runs) → `flower status RUN` → `flower log RUN` (what happened, in order).
Everything you need is in the run, not in your memory. Every command accepts `--json` and prints
`{ok, data, error, next}`; follow `next`. The plan file's reference is `PLAN_REFERENCE.md` next to this file.
"""


def skill_targets(root: Path, target: str) -> list[Path]:
    home = Path.home()
    m = {
        "claude": [home / ".claude" / "skills" / "flower" / "SKILL.md"],
        "codex": [home / ".codex" / "skills" / "flower" / "SKILL.md"],
        "agents": [home / ".agents" / "skills" / "flower" / "SKILL.md"],
        "project": [root / ".claude" / "skills" / "flower" / "SKILL.md",
                    root / ".agents" / "skills" / "flower" / "SKILL.md"],
    }
    if target == "all":
        return m["claude"] + m["codex"] + m["agents"]
    return m[target]


def install_skill(root: Path, target: str) -> list[Path]:
    out = []
    for p in skill_targets(Path(root), target):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SKILL_MD)
        (p.parent / "PLAN_REFERENCE.md").write_text(PLAN_REFERENCE)
        out.append(p)
    return out
