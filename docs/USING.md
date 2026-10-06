# Using flower in your project

This page is for a team, its people and its coding agents, adopting flower in an existing research project.
Every command and plan field is in the [reference](REFERENCE.md).

## 1. Install

```bash
pip install "git+https://github.com/floatingCatty/flower"
```

Python ≥ 3.9; the only dependency is pyyaml. Install it on the machine that drives the work,
usually a login node or a workstation. Remote machines that flower reaches over ssh do not need it.

> The package is not on PyPI yet, and **`pip install flower` installs Celery Flower**, an unrelated tool.
> Install from the repository as above.

## 2. Set up the project, once

From the project's root:

```bash
flower init            # add --hook if your agents use Claude Code
```

This creates:

| what | where | in git? |
|---|---|---|
| runs: append-only event logs, attempt directories, outputs, run inputs | `.flower/` | **no**: `flower init` adds it to `.gitignore` inside a git repository |
| the agent skill: how to work inside flower | `.claude/skills/flower/`, `.agents/skills/flower/` | yes |
| a short instruction block for any agent that reads `AGENTS.md` (Codex and others) | `AGENTS.md` | yes |
| with `--hook`: a Claude Code hook that reminds an agent when it computes outside a run | `.claude/settings.local.json` | no (local) |

It also says which of `ssh`, `rsync` and `sbatch` are missing on this machine (needed only for remote machines
and Slurm). Re-running it refreshes the skill after an upgrade.

Commit the plan files (`<study>/plan.yaml`), the scripts they call, and the software recipes in `envs/<name>/`.
With these, someone else can re-run the study from a fresh clone.

## 3. How an agent works in the project

The skill and the `AGENTS.md` block teach an agent this loop:

```bash
flower start "Reproduce Table 2 of <paper>"            # a run exists from the first minute
flower add RUN fetch --description "Downloads the data; n is the number of records." \
    --out n:integer --follow -- python3 fetch.py      # fetch.py prints {"n": ...} as its last line
flower add RUN fit --cluster box --env pyscf --set resources.cpus_per_task=8 --stage-in fit.py \
    --description "Fits the model; read chi2 (about 1 is good)." -- python3 fit.py
# fix code or plan.yaml, then:
flower rerun RUN fit --follow        # re-run a step (and what depends on it)
flower rerun RUN                     # apply plan-file edits (a new step, a description, a cluster's cpus)
```

- **Every computation is a step,** including quick checks whose answers the agent relies on.
- **Every step has a `--description`**: what it establishes and how to read its result. A missing one only
  warns.
- **Outputs:** a step's command prints a JSON object as its last line (or writes it to `$FLOWER_OUTPUTS`);
  `--out NAME:TYPE` declares what it must contain.
- **Edits apply at once and are recorded** (who, the diff): a finished step whose command changed runs again
  with what depends on it; its earlier result stays in the record.
- **Reading results:** `flower status RUN`, `flower show RUN STEP [KEY]` and `flower show RUN STEP --logs` all
  take `--json`. Commands return at once; `--follow` waits (`flower status RUN --follow`: until the run
  finishes or needs a decision). `status` and `--follow` exit 0 succeeded, 1 failed, 3 still running or waiting;
  any command exits 2 on an error.
- **Long work** runs in a background driver; the agent never needs to hold a terminal open.
- **Attribution:** set `FLOWER_ACTOR` (for example `agent:claude`, `agent:codex`) so the log says who did what.
  The default is `human:$USER`. The name is declared, not authenticated.

## 4. How a person works in the project

The same commands, plus:

- **The web UI:** `flower ui` starts or reuses the project's UI server and prints its address and token. From a
  laptop, `flower ui user@host:/path/to/project` opens it through an ssh tunnel. Each step shows its
  description, what it ran, and what it found (outputs, files, reports rendered in place).
- **Decisions:** `gate` steps (and a plan started with `flower run --review`) wait for a person. Answer them in
  the UI or with `flower approve RUN [GATE] [DECISION] --note "…"` / `flower reject RUN [GATE] --note "…"`; a
  gate with `on_reject` sends the work back with the note as `${feedback}`.
- **History:** `flower log RUN` (every event, actor and decision; `--note "…"` adds one); the UI's Timeline and
  Plan history tabs show the same.

## 5. A finished study as a protocol

`flower export RUN STEP` writes, next to the plan:
- `protocol.yaml`: the plan's own definitions of STEP and everything it depends on, with the versions of the
  environment recipes they used pinned (`environments:`). Probes, previews and side studies drop out.
- `expected.json`: those steps' results.
- `PROTOCOL.md`: what it does, needs and took.

Commit the three. Anyone reproduces the study with `flower run protocol.yaml --follow` (plus `--inputs` for machine
details) and checks it with `flower compare RUN expected.json`. To re-run a study with one step changed,
`flower run plan.yaml --reuse RUN --rerun-from STEP` takes the earlier run's results and inputs for everything
upstream.

## 6. Machines and software

- **Clusters** are named in the plan: `transport: ssh` with `scheduler: slurm` or `none` (a workstation without a
  batch system). Host names and ssh options are run *inputs*, given with `-i NAME=VALUE` or
  `--inputs file.json`. They are stored with the run, never in the plan file, so a plan can be shared without
  anyone's machine details.
- **`cpus: N`** on a cluster is a core budget shared by all runs of the project on that host. Change it in a
  running plan by editing the plan file, then `flower rerun RUN`.
- **Software** comes from frozen recipes in `envs/<name>/`: setup, activate and check scripts, pinned and
  hashed. Explore with `flower remote exec --run RUN --cluster C --env NAME -- <cmd>`, then run
  `flower env freeze NAME` and `flower env replay NAME --run RUN --cluster C --fresh`. Steps use a recipe with
  `--env NAME` (or `environment: NAME`). See [the reference](REFERENCE.md#environments).

## 7. What it is not (yet)

- **One machine per project.** Runs are files in that project's `.flower/`. Other people see them through the
  UI (over an ssh tunnel), not through a shared server.
- **No authentication of actors.** Actors are names given with `--actor` / `FLOWER_ACTOR`. Approvals record
  who answered, but nothing checks it.
- **No merging of runs across machines.** Two clones of a project have independent runs. `flower compare`
  takes run directories, so their results can still be compared.
