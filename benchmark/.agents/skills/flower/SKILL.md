---
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
  [--cluster C --env E --stage-in FILE --retrieve GLOB --cpus N --mem 16G] -- <command>` writes the step into plan.yaml
  and runs it, streaming its output. The command writes outputs as JSON to `$FLOWER_OUTPUTS`.
- **Fix and repeat:** edit the code or the step in plan.yaml, then `flower rerun RUN ID --follow`. Edits are
  picked up as recorded amendments (`policies: {edits: unfinished}` lets new/unfinished steps through).
- **A new machine later:** add an `inputs:` entry (value with `-i NAME=VALUE` on add/rerun) and a
  `clusters:` entry to plan.yaml; the next add/rerun picks them up. Existing clusters/inputs are fixed,
  except a cluster's `cpus`, `max_jobs`, `min_poll`: edit them and run `flower sync RUN` (nothing re-runs).
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
   `flower remote exec --run RUN --cluster C --env NAME [--probe] -- <cmd>` (logged; `--probe` for
   look-only commands); write envs/NAME/setup.sh (install into $FLOWER_ENV_PREFIX, pin exact versions),
   activate.sh and check.sh; `flower env freeze NAME`; prove it with
   `flower env replay NAME --run RUN --cluster C --fresh`; then put `environment: NAME` on the steps.
   (`--run RUN` uses the run's cluster and inputs; `--plan P` reads a plan file, with `-i`/`--inputs`.)
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
