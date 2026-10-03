"""Plan reference, starter template, and the skill that teaches coding agents to drive flower."""
from __future__ import annotations

from pathlib import Path

PLAN_REFERENCE = r"""# flower plan reference (flower: 1)

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
  box:  {transport: ssh, host: mybox, scheduler: none, cpus: 32}   # no batch system: run directly on the host;
                             # cpus: the cores flower may use there, shared by all runs of the project
                             # (each step counts resources.cpus_per_task, default 1)
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
  activated. A shell/function step with `environment:` and no `cluster:` runs on this machine with it (the
  implicit cluster `local`): use that for local analysis instead of naming an interpreter path. Recipes are made with `flower env new|freeze|replay` and `flower remote exec --env`.
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
"""

PLAN_TEMPLATE = """flower: 1
id: {id}
title: Describe the study in a few words
description: |
  The goal of this workflow, in plain language. It is shown when you approve the plan and at the top
  of the final report.

inputs:
  topic: {type: string, default: "example"}

defaults:
  harness: {name: claude, model: sonnet}
  timeout: {total: 1h}
  retry: {max_attempts: 2}

nodes:
  - id: prepare
    kind: shell
    run: |
      echo "preparing ${inputs.topic}"
      echo '{"items": ["a", "b"], "summary": "prepared 2 items"}' > "$FLOWER_OUTPUTS"
    outputs: {items: array}

  - id: work
    kind: shell
    foreach: "${prepare.outputs.items}"
    run: |
      echo "working on ${item}"
      echo '{"value": 1}' > "$FLOWER_OUTPUTS"
    outputs: {value: number}

  - id: review
    kind: gate
    needs: [work]
    message: "work produced ${work.outputs.count} results. Continue?"
    decisions: [approve, reject]
"""

SKILL_MD = r"""---
name: flower
description: Plan, run, monitor and grow long multi-step workflows (agent steps, shell/python steps, Slurm/HPC jobs, human approvals) with the `flower` CLI. Use when work is multi-step, long-running (hours to days), must survive session restarts, needs HPC jobs, human sign-off, or an auditable record. Not for one-off quick tasks.
---

# flower — durable workflows you can drive across sessions

flower keeps the *state* of long work outside your context: a plan file (the contract the user
approves) and an append-only journal. Any new session can pick up a run with `flower status`.

## Rule 0
If the environment variable `FLOWER_INSIDE_RUN` is set, you are *inside* a flower node.
Do the node's task and answer with its JSON contract. Never run flower commands from inside a node.

## When to use it
Use flower when the work has several steps with dependencies, any step can take long (Slurm jobs,
long agent tasks), a human should approve the plan or key decisions, or the user wants a record of
what was done and why. For a single quick command, just do it directly.

## The loop: the run first, then everything as steps
**Start the run before you explore.** `flower start "<goal>"` creates an empty draft plan
(`<id>/plan.yaml`) and a run that parks until it has steps, and starts the project's UI so the user can
watch. Nothing is too early to be a step: downloading inputs, the first quick test, a parameter probe.
- **One command per step:** `flower add RUN ID [--needs X] [--out NAME:TYPE] [--file NAME=PATH]
  [--cluster C --env E --stage-in FILE --retrieve GLOB] -- <command>` writes the step into plan.yaml
  and runs it, streaming its output. The command writes outputs as JSON to `$FLOWER_OUTPUTS`.
- **Fix and repeat:** edit the code or the step in plan.yaml, then `flower rerun RUN ID --follow`. Edits are
  picked up as recorded amendments (`policies: {edits: unfinished}` lets new/unfinished steps through).
- **A new machine later:** add an `inputs:` entry (value with `-i NAME=VALUE` on add/rerun) and a
  `clusters:` entry to plan.yaml; the next add/rerun picks them up. Existing clusters/inputs are fixed.
- **Explore a remote host** with `flower remote exec` (logged), not raw ssh.
- Reading papers, files and results directly is fine. *Running* computations beside the run is not: they
  are unrecorded and invisible to the user. That includes the quick check whose answer you rely on (a symmetry
  test, a unit conversion with the study's code): make it a one-line `flower add` step. If a hook reminds you,
  move the work into a step.
For a workflow that is already known end to end, writing the whole plan first (below) is fine too.

1. **Draft the plan** with the user: `flower plan new plan.yaml` or write YAML
   (`flower plan reference` prints the full format). Prefer deterministic `shell`/`function`/`job`
   nodes for computation and `agent` nodes for judgement (analysis, choosing parameters, writing).
   Declare `outputs` for every node whose result is used later.
2. **Validate until clean:** `flower plan validate plan.yaml --json` — fix every listed issue.
3. **Show the plan to the user:** `flower plan show plan.yaml`. Explain it in your own words.
4. **Create the run:** `flower run plan.yaml -i name=value --json` → status `awaiting_approval`
   (exit code 3). **The decision is the user's. Never approve on your own judgement.** Only after the
   user explicitly says yes: `flower approve RUN --note "approved by <user> in chat"`.
   (If the user tells you up front they approve, `flower run plan.yaml --yes --detach --json`.)
5. **Let it run in the background:** approval starts a background driver. Monitor with
   `flower wait RUN --timeout 600 --json` (run it as a background task if your harness supports it;
   don't poll in a tight loop). Exit code 0 = succeeded, 1 = failed, 3 = needs a decision / still running.
   **Software on a cluster:** if a step needs software on a remote target, make an environment recipe
   instead of hand-written preludes: `flower env new NAME`; explore the target with
   `flower remote exec --plan P --cluster C --env NAME [--probe] -- <cmd>` (logged; `--probe` for
   look-only commands); write envs/NAME/setup.sh (install into $FLOWER_ENV_PREFIX, pin exact versions),
   activate.sh and check.sh; `flower env freeze NAME`; prove it with
   `flower env replay NAME --plan P --cluster C --fresh`; then put `environment: NAME` on the steps.
   Ask the user before installing anything on a machine they share with others.
   To let the user watch it, run `flower ui --json` (starts or reuses the project's UI in the background)
   and give them its links: the local one if they sit at this machine, else the `ssh -N -L …` line, or
   `flower open user@host:/path/to/project` if they have flower on their laptop.
6. **When it parks on a decision** (`open_gates` in the JSON): read it with
   `flower show RUN --gate GATE`, relay the question to the user in plain words, and answer with their
   decision: `flower answer RUN GATE <decision> --text "<their reason>"`.
7. **When something fails:** `flower show RUN NODE` (error class, stderr, agent text), fix the cause,
   then `flower rerun RUN NODE` (re-runs it and everything downstream; unchanged nodes are reused).
   If the plan itself must change, write an amendment YAML and `flower amend RUN change.yaml`
   (the user approves it like the plan).
8. **Report:** `flower report RUN` writes report.md/report.html (decisions, outputs, files,
   timeline). Summarise it for the user; "the run was admitted" is not "the run succeeded" — check the
   final status.

## Coming back later (new session)
`flower ls` → `flower status RUN` (what is done, what is running, what needs the user) →
`flower log RUN` (what happened, in order). Everything you need is in the run, not in your memory.

## Useful commands
- `flower show RUN NODE` — attempts, inputs, outputs, files, errors, rationale
- `flower logs RUN NODE` — agent transcript / Slurm stdout / shell output
- `flower output RUN NODE [key]` — a node's outputs (or one value/file path)
- `flower cancel RUN [--node N]`, `flower signal RUN NAME --data '{...}'`, `flower note RUN "text"`
- Every command accepts `--json` and prints `{ok, data, error, next}`; follow `next` suggestions.
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
