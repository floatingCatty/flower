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
  [--cluster C --env E --stage-in FILE --retrieve GLOB --cpus N --mem 16G] -- <command>` writes the step into
  plan.yaml and runs it, streaming its output. The command writes outputs as JSON to `$FLOWER_OUTPUTS`.
- **Describe every step** (`--description`, one or two sentences): what it establishes, and how to read its
  result (which output or file answers the question, what a good value looks like). The run is read by people
  who were not there; the UI shows the description first. Missing ones only warn; add them later in plan.yaml
  and `flower sync RUN` (a description edit re-runs nothing).
- **Fix and repeat:** edit the code or the step in plan.yaml, then `flower rerun RUN ID --follow`.
  `flower sync RUN` applies plan edits without re-running anything (a new step, a description, a cluster's
  `cpus`). An edit of a *finished* step waits for the user's approval.
- **A new machine later:** add an `inputs:` entry (value with `-i NAME=VALUE`) and a `clusters:` entry to
  plan.yaml; the next add/rerun/sync picks them up.
- **Explore a remote host** with `flower remote exec --run RUN --cluster C -- <cmd>` (logged), not raw ssh.
- Reading papers, files and results directly is fine. *Running* computations beside the run is not: they are
  unrecorded and invisible to the user. That includes the quick check whose answer you rely on: make it a
  one-line `flower add` step. If a hook reminds you, move the work into a step.

**Software on a cluster:** make an environment recipe instead of hand-written preludes: `flower env new NAME`;
explore with `flower remote exec --run RUN --cluster C --env NAME [--probe] -- <cmd>`; write envs/NAME/setup.sh
(install into $FLOWER_ENV_PREFIX, pin exact versions), activate.sh and check.sh; `flower env freeze NAME`; prove
it with `flower env replay NAME --run RUN --cluster C --fresh`; then `--env NAME` on the steps. Ask the user
before installing anything on a machine they share with others.

**A plan written in full up front** (a known workflow) is fine too: `flower plan validate plan.yaml --json`
until clean, `flower plan show plan.yaml` to the user, then `flower run plan.yaml --json` (status
`awaiting_approval`, exit 3). **The decision is the user's. Never approve on your own judgement.** Only after the
user says yes: `flower approve RUN --note "approved by <user> in chat"` (or `flower run plan.yaml -y` when they
said so up front).

## While it runs
- `flower status RUN --json` — what is done, running, failed, and what needs the user (one pass; starts a
  background driver if needed). `flower status RUN --follow --timeout 600` blocks until it finishes or needs a
  decision (run it as a background task if your harness supports it; don't poll in a tight loop). Exit codes:
  0 succeeded, 1 failed, 2 error, 3 needs a decision / still running.
- **A decision** (`open_gates`): `flower show RUN --gate GATE`, relay it to the user in plain words, answer
  with theirs: `flower approve RUN GATE [DECISION] --note "<their reason>"` or `flower reject RUN GATE --text "…"`.
- **A failure:** `flower show RUN STEP` (error, stderr, outputs written before failing), `flower logs RUN STEP`,
  fix the cause, `flower rerun RUN STEP` (it and everything downstream; unchanged steps are reused).
- **The user watching:** `flower ui --json` starts or reuses the project's UI; give them its links (the local
  one, the `ssh -N -L …` line, or `flower ui user@host:/path` if flower is on their laptop).

## When it is done
`flower show RUN STEP KEY` prints one result. `flower export RUN STEP` turns the steps behind STEP into
`protocol.yaml` + `expected.json` + `PROTOCOL.md` next to the plan: anyone reproduces it with
`flower run protocol.yaml -y` and checks with `flower compare RUN expected.json`. "The run was admitted" is not
"the run succeeded": check the final status before you report.

## Coming back later (new session)
`flower status` (the project's runs) → `flower status RUN` → `flower log RUN` (what happened, in order).
Everything you need is in the run, not in your memory. Every command accepts `--json` and prints
`{ok, data, error, next}`; follow `next`.
