# flower

**Long computational work, recorded as you go, and re-runnable as a protocol.**

A coding agent (or a person) doing research over many sessions leaves a repository that is hard to follow: scripts,
half-finished runs, quick checks whose results someone relied on. flower keeps that work in a form a person can
read and check, and turns it, once it works, into a protocol anyone re-runs with one command.

- **A study is a plan file of steps.** Each step is a shell command with a description of what it establishes, the
  outputs it declares, and where it runs (this machine, a workstation over ssh, a Slurm cluster).
- **A run is that plan plus its history.** An append-only journal records every step, attempt, result, plan edit
  and decision, and who made it. `flower ui` shows it; any new session picks it up with `flower status`.
- **A finished run is a protocol.** `flower export RUN STEP` keeps the steps behind a result and their values;
  `flower run protocol.yaml --follow` reproduces it and `flower compare RUN expected.json` says whether it did.

```text
$ flower status heisenberg-2d-20261004-172331-d4e1
Reproduce the ground-state parameters of the 2D S=1/2 Heisenberg antiferromagnet by stochastic series …
  plan generation 10 (3f162f34de60)  ·  89/89 done

     NODE                KIND                    TIME  RESULT / ACTIVITY
  ✓  paper               shell                     1s  outputs: sha256_pdf="1f61e4eadc4a345fb165a8272de533a8…
  ✓  env-numba-remote    shell@remote              5s  process 1717043 completed · outputs: env="numba", …
  ✓  env-quspin-remote   shell@remote              5s  process 1726865 completed · outputs: env="quspin", …
  ✓  env-analysis-local  shell@local              12s  process 3475 completed · outputs: env="analysis", …
  ✓    ed-twist          shell@remote              4s  process 1727427 completed · outputs: E0_per_site=-0.70178…
  ✓    fits-preview      shell@local               2s  process 16826 completed · outputs: n_claims=7, …
  ✓      verify          shell@remote             14s  process 1727971 completed · outputs: E={"qmc": -0.70177…
  ✓          sse         shell@remote              0s  80/80 item(s) succeeded
  ✓            fits      shell@local               2s  process 16704 completed · outputs: n_claims=7, n_reproduced=7
```

## Install

```bash
pip install "git+https://github.com/floatingCatty/flower"   # Python ≥ 3.9; dependency: pyyaml
```

Not on PyPI yet: `pip install flower` installs Celery Flower, an unrelated tool.

## Two ways in

**Grow a study** (how an agent works):
```bash
flower init                                         # .flower/ (gitignored) + the agent instructions
flower start "Reproduce Table 2 of <paper>"         # a run right away, with an empty draft plan
flower add RUN fetch --description "Downloads the data; n is the number of records." \
    --out n:integer --follow -- python3 fetch.py   # fetch.py prints {"n": ...} as its last line
flower rerun RUN fetch --follow                     # after editing the code or the step in plan.yaml
flower status RUN                                   # what is done, running, failed, waiting for you
```

**Reproduce a study** (anyone, from a clone):
```bash
flower run j1j2-chain/protocol.yaml --follow --inputs my-machines.json
flower compare RUN j1j2-chain/expected.json         # 24 step(s) agree within rtol 1e-06, 0 differ
```

## A plan

```yaml
flower: 1
id: si-bands
title: Silicon band structure
nodes:                                              # hpc: a machine of `flower remote list`, set up once
  - id: relax
    description: Relaxes the cell; energy and converged are the result.
    cluster: hpc
    environment: qe                                 # a frozen software recipe in envs/qe/
    resources: {nodes: 1, ntasks: 32, time: "04:00:00"}
    stage_in: [relax.in]
    run: |
      mpirun pw.x -in relax.in > relax.out
      python3 parse.py relax.out                    # prints {"energy": ..., "converged": ...}
    outputs: {energy: number, converged: boolean}

  - id: bands
    description: Band structure at the relaxed geometry; read bands.png.
    cluster: hpc
    environment: qe
    when: "${relax.outputs.converged}"
    stage_in: [{from: "remote:${relax.outputs.job_dir}/out", to: out, mode: link}]
    run: mpirun pw.x -in bands.in > bands.out && python3 plot.py
    files: {plot: bands.png}

  - id: sign-off
    kind: gate
    message: "Bands done (${bands.summary}). Publish?"
```

Two step kinds: `shell` (a command, the default) and `gate` (a person's decision). Plus `foreach` fan-out, `when`
conditions, retries keyed on failure class, timeouts, and caching of unchanged steps.

## Commands

| | |
|---|---|
| set up | `init` |
| grow | `start`, `add`, `rerun` |
| run a plan | `run` |
| follow and read | `status`, `show`, `log`, `ui` |
| decide | `approve`, `reject`, `cancel` |
| deliver | `export`, `compare` |
| machines and software | `remote add\|list\|check\|login\|exec`, `env new\|freeze\|replay\|check\|show`, `plan validate\|show` |

Every command takes `--json` and prints `{ok, data, error, next}`, and returns at once; `--follow` waits.
`status` and `--follow` exit 0 succeeded, 1 failed, 3 still running or waiting for a decision; 2 is an error. Details: [docs/REFERENCE.md](docs/REFERENCE.md). How a team adopts
it: [docs/USING.md](docs/USING.md).

## What it handles for you

- **Nothing holds a process open.** Local steps run under detached supervisors and Slurm jobs park until they
  finish; a background driver advances the run, and any `flower status` can pick it up after a crash or reboot.
- **Machines set up once.** `flower remote add narval user@host` probes the scheduler, cores, partitions, accounts
  and scratch; agents read `flower remote list` instead of asking, plans just say `cluster: narval`, and
  `--login` / `flower remote login` cover passwords and second factors with one shared connection.
- **HPC and remote machines.** Submission is idempotent (a write-ahead intent, a deterministic job name, an in-job
  duplicate guard): a crash at any point re-attaches instead of submitting twice. Failures are typed (`timeout`,
  `oom`, `node_fail`, `preempted`, `lost`, …). Machines without a batch system run steps as detached processes
  over ssh. A shared `cpus:` budget keeps several runs from oversubscribing one host.
- **Software environments.** An agent explores a machine through the logged `flower remote exec` and writes a
  small recipe (`envs/<name>/setup.sh`, `activate.sh`, `check.sh`); `flower env freeze` hashes it and
  `flower env replay --fresh` proves it installs from scratch. A changed recipe never reuses old results, and an
  exported protocol pins the versions it was made with.
- **Changing a running plan.** Edit the plan file and `flower rerun RUN`: every edit applies at once and is
  recorded with who made it; a changed finished step runs again, and history is never rewritten.

## Tested by reproducing papers

Each study ran inside one flower run, from fetching the paper to the table of claims, on a shared workstation and
a remote machine over ssh ([`benchmark/`](benchmark/README.md)):

| paper | methods | outcome |
|---|---|---|
| Eggert, PRB 1996 | J1–J2 chain, exact diagonalization (QuSpin) | 6/6; reproduced again from a fresh clone as a protocol |
| Sandvik, PRB 1997 | 2D Heisenberg, SSE quantum Monte Carlo | 7/7, E∞ = −0.669436(31) vs −0.669437(5) |
| Ferrenberg, Xu, Landau, PRE 2018 | 3D Ising, Wolff Monte Carlo, finite-size scaling | K_c within 0.8σ; ν not at the paper's precision |
| Kühner, White, Monien, PRB 2000 | 1D Bose–Hubbard, DMRG (TeNPy) | t_c = 0.294 vs 0.297(10) |
| Motta *et al.*, PRX 2017 | hydrogen chains, PySCF RHF/UHF/CCSD(T)/FCI | exact R_e, E_0, E_∞; Table II 66/70 |
| Lejaeghere *et al.*, Science 2016 | Δ test, 71 crystals with Quantum ESPRESSO | Δ 0.56 vs 0.44 meV/atom; the gap traced |
| Rignanese *et al.*, PRB 1996 | Si thermal expansion, DFPT + quasi-harmonic | 16/18; a fresh-clone rerun agrees to 1e-6 |
| Turkel *et al.*, Science 2022 | twisted trilayer graphene, continuum model + Hartree–Fock | 10/15, the rest explained |
| Jurečka *et al.*, PCCP 2006 | S22 interaction energies, PySCF MP2/CCSD(T) | campaign with memory and scratch failures handled mid-run |
| Tazi *et al.*, JPCM 2012 | water diffusion, OpenMM MD | TIP4P/2005 reproduced |

What they found in flower is recorded in [`tests/core/BUGS.md`](tests/core/BUGS.md), each now a regression test.

## Quality

`pytest tests` runs the core (journal, plan, templates, engine, crash injection, CLI, UI) and HPC suites (a fake
Slurm with injected faults, crash windows during submit, poll and retrieve, ssh). The design record and the
research behind it are in [`context/notes/`](context/notes/).

License: Apache-2.0.
