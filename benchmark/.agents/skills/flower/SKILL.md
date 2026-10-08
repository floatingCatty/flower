---
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
- **Where to run:** `flower remote list --json` lists the machines the user set up (once, with `flower remote
  add NAME user@host`: flower probed the rest): cores, memory, GPUs or Slurm partitions, limits and accounts, work
  and environment directories, `agent_may_use` and the user's `note`. Choose from it; do not ask the user for
  hosts, accounts or partitions. A step runs there with `--cluster NAME` (Slurm: `--set resources.partition=…
  --set resources.time=… --set resources.cpus_per_task=…` within the listed limits). Stay within `agent_may_use`
  and the note; ask before going beyond them. No suitable machine: ask the user to `flower remote add` one. A
  machine that refuses the connection (a password or second factor): ask the user to run `flower remote login NAME`.
- **Explore a machine** with `flower remote exec --cluster NAME [--run RUN] -- <cmd>` (logged in the run), not raw ssh.
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
