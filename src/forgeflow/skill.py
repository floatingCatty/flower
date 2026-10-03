"""Plan reference, starter template, and the skill that teaches coding agents to drive forgeflow."""
from __future__ import annotations

from pathlib import Path

PLAN_REFERENCE = r"""# forgeflow plan reference (forgeflow: 1)

A plan is a YAML contract. `forgeflow plan validate plan.yaml` lists every problem with a fix hint.

```yaml
forgeflow: 1                 # required
id: my-study                 # required: [A-Za-z0-9_.-]
title: Human title
description: |               # the goal, shown at approval and in the report
  What this workflow is for.
inputs:                      # given at `forgeflow run plan.yaml -i name=value`
  structure: {type: path, required: true, description: POSCAR file}
  strain:    {type: number, default: 0.01}
defaults:
  harness: {name: claude, model: sonnet}   # default for agent nodes
  timeout: {total: 2h, idle: 30m}          # killed if exceeded (idle = no output)
  retry:   {max_attempts: 2, backoff: 30s} # retries infrastructure failures (lost, node_fail, …)
  concurrency: 4                           # max local processes (shell/function/agent) at once
clusters:                    # for job nodes
  hpc:  {transport: ssh, host: myhpc, remote_root: ~/ff-runs, max_jobs: 20, min_poll: 60s,
         modules: [vasp/6.4], prelude: ["source ~/env.sh"], resources: {partition: cpu, account: abc}}
  here: {transport: local}   # forgeflow runs on the login node itself
nodes:
  - id: name                 # unique; letters, digits, - _
    kind: shell|function|agent|job|gate|wait
    title: optional label
    needs: [other]           # explicit dependencies (references ${x...} add edges automatically)
    when: "${scan.outputs.n} > 0"           # optional condition; false -> skipped
    trigger: all_success     # all_success (default) | all_done | any_success
    inputs: {k: "${other.outputs.key}"}     # resolved values: $FF_INPUTS / FF_IN_K / prompt contract
    outputs: {energy: number, converged: boolean}   # declared result contract (validated)
    files: {report: report.md}              # declared files (relative to the node dir), hashed
    retry: {max_attempts: 3, on: [lost, node_fail, exit_nonzero]}
    timeout: {total: 1h}
    cache: true              # reuse result when definition+inputs unchanged (default true; agents false)
    on_failure: continue     # downstream proceeds, run does not fail on this node
    foreach: "${scan.outputs.items}"        # fan-out: one child per item; ${item}, ${index}
    env: {OMP_NUM_THREADS: "4"}
    cwd: some/dir            # default: a fresh per-attempt work dir
```

## Node kinds
* **shell** — `run: |` bash script (`set -euo pipefail`). Write outputs as JSON to `$FF_OUTPUTS`.
* **function** — `call: package.module:function`; kwargs = `args:` (or `inputs:`); returns a dict.
  `python: /path/to/python` to use another environment; `pythonpath: [dir]`. The cache key includes the
  source of the called module (its whole top-level package) when it lives on `pythonpath` or next to the
  plan, so editing the code invalidates cached and forked results; installed libraries are not tracked.
* **agent** — `prompt: |` (or `prompt_file:`), `harness: {name: claude|codex|pi|script, model, effort,
  permission: bypass|edits, tools: {allow: [...], deny: [...]}, budget_usd, command: [...]}`, `system:`.
  The agent must end with a JSON object matching `outputs` + `summary` + `rationale`; invalid answers
  get `repair_attempts` (default 2) correction turns. `effects: {amend: {auto_approve: true, max_nodes: 3,
  kinds: [shell, job], ops: [add, detour]}}` lets it propose plan changes (otherwise not allowed).
* **job** — Slurm batch job: `cluster:`, `script: |` (payload, `set -eo pipefail`), `resources: {nodes,
  ntasks, ntasks_per_node, cpus_per_task, mem, time, partition, account, qos, gpus, extra: [...]}`,
  `stage_in: [{from: local/path, to: name}, {from: "remote:${relax.outputs.job_dir}/CHGCAR", to: CHGCAR, mode: link}]`,
  `retrieve: [glob, ...]`. Write `outputs.json` (`$FF_OUTPUTS`) in the job dir. Outputs always include
  `job_id` and `job_dir`. Parks while queued/running — no process is held.
* **gate** — human decision: `message:` (templated), `decisions: [approve, reject]`,
  `on_reject: {rerun: [node, ...], max_attempts: 3}` — a rework loop: the re-run nodes see the
  reviewer's text as `${feedback}` (empty on the first pass).
  Outputs `{decision, text, by}`.
* **wait** — `signal: name` (send with `forgeflow signal RUN name --data '{...}'` or a file in
  `signals/`), `timer: 10m`, `deadline: 2d` (expiry succeeds with `expired: true`).

## References
`${inputs.x}` · `${node.outputs.key.sub}` · `${node.files.name}` · `${node.dir}` · `${node.summary}` ·
`${feedback}` (rework text from a gate) · `${plan.dir}` · `${run.id}` · `${run.dir}` ·
`${item}` / `${index}` (foreach) · `${env.VAR}`.
A value that is exactly one reference keeps its type (list, number…). `$${` is a literal `${`.

## Amendments (change a running plan)
`forgeflow amend RUN change.yaml` with
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
```
History is immutable: finished nodes can only be superseded, never edited in place.
"""

PLAN_TEMPLATE = """forgeflow: 1
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
      echo '{"items": ["a", "b"], "summary": "prepared 2 items"}' > "$FF_OUTPUTS"
    outputs: {items: array}

  - id: work
    kind: shell
    foreach: "${prepare.outputs.items}"
    run: |
      echo "working on ${item}"
      echo '{"value": 1}' > "$FF_OUTPUTS"
    outputs: {value: number}

  - id: review
    kind: gate
    needs: [work]
    message: "work produced ${work.outputs.count} results. Continue?"
    decisions: [approve, reject]
"""

SKILL_MD = r"""---
name: forgeflow
description: Plan, run, monitor and grow long multi-step workflows (agent steps, shell/python steps, Slurm/HPC jobs, human approvals) with the `forgeflow` CLI. Use when work is multi-step, long-running (hours to days), must survive session restarts, needs HPC jobs, human sign-off, or an auditable record. Not for one-off quick tasks.
---

# forgeflow — durable workflows you can drive across sessions

forgeflow keeps the *state* of long work outside your context: a plan file (the contract the user
approves) and an append-only journal. Any new session can pick up a run with `forgeflow status`.

## Rule 0
If the environment variable `FORGEFLOW_INSIDE_RUN` is set, you are *inside* a forgeflow node.
Do the node's task and answer with its JSON contract. Never run forgeflow commands from inside a node.

## When to use it
Use forgeflow when the work has several steps with dependencies, any step can take long (Slurm jobs,
long agent tasks), a human should approve the plan or key decisions, or the user wants a record of
what was done and why. For a single quick command, just do it directly.

## The loop
1. **Draft the plan** with the user: `forgeflow plan new plan.yaml` or write YAML
   (`forgeflow plan reference` prints the full format). Prefer deterministic `shell`/`function`/`job`
   nodes for computation and `agent` nodes for judgement (analysis, choosing parameters, writing).
   Declare `outputs` for every node whose result is used later.
2. **Validate until clean:** `forgeflow plan validate plan.yaml --json` — fix every listed issue.
3. **Show the plan to the user:** `forgeflow plan show plan.yaml`. Explain it in your own words.
4. **Create the run:** `forgeflow run plan.yaml -i name=value --json` → status `awaiting_approval`
   (exit code 3). **The decision is the user's. Never approve on your own judgement.** Only after the
   user explicitly says yes: `forgeflow approve RUN --note "approved by <user> in chat"`.
   (If the user tells you up front they approve, `forgeflow run plan.yaml --yes --detach --json`.)
5. **Let it run in the background:** approval starts a background driver. Monitor with
   `forgeflow wait RUN --timeout 600 --json` (run it as a background task if your harness supports it;
   don't poll in a tight loop). Exit code 0 = succeeded, 1 = failed, 3 = needs a decision / still running.
6. **When it parks on a decision** (`open_gates` in the JSON): read it with
   `forgeflow show RUN --gate GATE`, relay the question to the user in plain words, and answer with their
   decision: `forgeflow answer RUN GATE <decision> --text "<their reason>"`.
7. **When something fails:** `forgeflow show RUN NODE` (error class, stderr, agent text), fix the cause,
   then `forgeflow rerun RUN NODE` (re-runs it and everything downstream; unchanged nodes are reused).
   If the plan itself must change, write an amendment YAML and `forgeflow amend RUN change.yaml`
   (the user approves it like the plan).
8. **Report:** `forgeflow report RUN` writes report.md/report.html (decisions, outputs, files,
   timeline). Summarise it for the user; "the run was admitted" is not "the run succeeded" — check the
   final status.

## Coming back later (new session)
`forgeflow ls` → `forgeflow status RUN` (what is done, what is running, what needs the user) →
`forgeflow log RUN` (what happened, in order). Everything you need is in the run, not in your memory.

## Useful commands
- `forgeflow show RUN NODE` — attempts, inputs, outputs, files, errors, rationale
- `forgeflow logs RUN NODE` — agent transcript / Slurm stdout / shell output
- `forgeflow output RUN NODE [key]` — a node's outputs (or one value/file path)
- `forgeflow cancel RUN [--node N]`, `forgeflow signal RUN NAME --data '{...}'`, `forgeflow note RUN "text"`
- Every command accepts `--json` and prints `{ok, data, error, next}`; follow `next` suggestions.
"""


def skill_targets(root: Path, target: str) -> list[Path]:
    home = Path.home()
    m = {
        "claude": [home / ".claude" / "skills" / "forgeflow" / "SKILL.md"],
        "codex": [home / ".codex" / "skills" / "forgeflow" / "SKILL.md"],
        "agents": [home / ".agents" / "skills" / "forgeflow" / "SKILL.md"],
        "project": [root / ".claude" / "skills" / "forgeflow" / "SKILL.md",
                    root / ".agents" / "skills" / "forgeflow" / "SKILL.md"],
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
