# Benchmark: Si DOS and Fermi level (ABACUS)

A real-science test of flower. ABACUS computes the DOS of bulk diamond Si, and the Fermi level is
obtained by solving

    ∫ g(E) f_FD(E; E_F, T) dE = N_e

on that DOS.

| node | kind | what |
|---|---|---|
| `prepare` | shell (calls `abacus_si.py:prepare`) | Writes STRU (a = 10.26 bohr). Reads N_e from the UPF `z_valence` (4 × 2 = 8). |
| `scf` | shell | ABACUS SCF, PBE, LCAO `2s2p1d`, 8³ k-mesh, `out_chg 1` |
| `dos[i]` | shell, foreach | ABACUS nscf from the SCF density, on 8³, 12³, 16³ and 24³ meshes, `out_dos 1` |
| `fermi[i]` | shell, foreach | Fermi-Dirac integration of each DOS (`abacus_si.py:fermi_from_dos`) and a DOS plot |
| `converge` | shell | E_F against k-mesh, a convergence verdict and a plot |

The study first ran with `function` steps and an agent `review` step (which never ran: the local `claude` CLI had
an expired login). When flower was reduced to `shell` and `gate` steps (2026-10-05), the function steps became
shell steps calling the same Python functions, and the review step was dropped.

## Run

```bash
cd benchmark
../.venv/bin/flower run si-dos-fermi/plan.yaml            # review the plan, approve, watch it run
../.venv/bin/flower status                                # or: flower ui
```

Everything takes about 1 minute, running 8 MPI ranks per ABACUS step. The paths to the
ABACUS conda env and the `PP_ORB` directory are plan inputs.

### Remote calculation, local analysis

[`plan-remote.yaml`](plan-remote.yaml) splits the same study across two machines:

| step | where | how |
|---|---|---|
| `prepare` | this machine | writes STRU and counts electrons |
| `env-abacus-remote` | **remote** | generated from `environment: abacus`: runs the frozen recipe [`envs/abacus/`](../envs/abacus/)'s `check.sh`, and installs it with `setup.sh` if missing |
| `scf`, `dos[i]` | **remote**, over `ssh` | `shell` + `cluster: remote` (`scheduler: none`) + `environment: abacus`: a detached ABACUS process with the environment activated |
| `fermi[i]`, `converge` | this machine | read the DOS files fetched back to `${item.local_dir}` |

The structure, pseudopotential, orbital and the input helper are uploaded with `stage_in`, and nothing
stays connected while ABACUS runs. The software comes from the environment recipe `envs/abacus/`:
- **How it was made:** found by exploring the remote with `flower remote exec`; the log is in
  `sessions/`.
- **What it pins:** the exact conda-forge package files (`conda-explicit.txt`, 38 files with md5:
  ABACUS 3.9.0 `cpu_mpi_mpich`, MPICH 4.3.2).
- **How it's frozen and checked:** frozen with `flower env freeze`, and replayed from scratch with
  `flower env replay --fresh` (a new prefix, installed and checked in 4 s).
- **What the remote needs:** only conda, mamba or micromamba, plus `python3`.

To run it, put the connection settings in an inputs file:

```json
{"host": "<ssh alias>", "ssh_options": ["-F", "/path/to/ssh_config"]}
```

```bash
../.venv/bin/flower run si-dos-fermi/plan-remote.yaml --inputs remote-inputs.json
```

`ssh_options` is optional; leave it out when the alias is in `~/.ssh/config`.

**Run on a real remote machine** (`si-dos-fermi-remote-20261003-154251-0de6`, 2026-10-03,
`-i agent_review=false`): it **succeeded in 47 s**.
- **The remote machine:** a 32-core Linux workstation reached over the internet (ssh on a custom port),
  without a batch system.
- **The environment step:** `env-abacus-remote` installed the recipe's prefix (10 s, `how: installed`);
  `scf` and `dos[i]` ran with it activated. An earlier run, with a hand-written `remote_prelude`
  instead of a recipe, took 1 min 06 s with the same results.
- **Where the time goes:** ABACUS itself takes about 5 s per nscf run. Each remote step takes 14–20 s
  in total, which includes uploading the inputs, the 3 s poll interval and fetching the DOS files back.
- **Results:** every number matches the fully local run in the table below (E_F = 6.8301 eV at 24³).

## Numerics (`abacus_si.py`)

* **DOS.** The main DOS is the unbroadened histogram ABACUS writes to `OUT.si/DOS1` (states per
  0.01 eV bin). The Gaussian-broadened `DOS1_smearing.dat` (σ = 0.05 eV) is integrated as a comparison.
* **Charge-neutrality condition.** It is solved as `[N_below − N_e] + n_el(μ) − n_hole(μ) = 0` around a
  split energy. This avoids cancellation, because at 300 K the band tails hold only about 10⁻⁷ carriers.
* **Rounding.** ABACUS prints the bin weights to 6 significant digits, so `N_below` is off by about
  10⁻⁵. That error alone would pull E_F about 0.3 eV towards the VBM. When the split lies in an empty
  gap of the histogram, `N_below` is snapped to N_e.
* **Gap detection.** The gap is the run of exactly-empty bins whose cumulative count is closest to N_e.
  A cumulative-count tolerance is not enough here: at 24³, the weight of the Γ point (4×10⁻⁴) falls
  below such a tolerance.

## Result (run `si-dos-fermi-20261003-133355-ad9d`, `-i agent_review=false`, succeeded in 40 s; the first
run, made before the rename, gave identical numbers)

| k-mesh | E_F (FD, 300 K) | E_F (smeared DOS) | ABACUS E_F | VBM | CBM | gap | E_F − midgap |
|---|---|---|---|---|---|---|---|
| 8³  | 6.8271 | 6.8321 | 7.1066 | 6.401 | 7.271 | 0.87 | −9.0 meV |
| 12³ | 6.8227 | 6.8277 | 7.1066 | 6.401 | 7.261 | 0.86 | −8.4 meV |
| 16³ | 6.8257 | 6.8307 | 7.1066 | 6.401 | 7.271 | 0.87 | −10.4 meV |
| 24³ | 6.8301 | 6.8351 | 7.1066 | 6.401 | 7.261 | 0.86 | −0.9 meV |

**E_F = 6.830 eV (k = 24³). It changes by 4.5 meV from 16³, so it is converged at the 5 meV tolerance.**
This is just below midgap, as expected for intrinsic Si, whose valence band has the heavier DOS mass.
The ±5 meV scatter comes from the 0.01 eV DOS bins, which move the CBM edge by one bin.

* **ABACUS's E_F is not the physical one.** ABACUS reports 7.1066 eV for every k-mesh, and 6.957 eV in
  the SCF. For an insulator with a small Gaussian smearing, ABACUS's bisection accepts the first
  midpoint that lands anywhere in the gap. The number is therefore arbitrary within [VBM, CBM].
* **The gap is larger than plane-wave PBE.** The DOS gap is 0.86–0.87 eV, against about 0.6 eV for
  plane-wave PBE. This is consistent with the SCF eigenvalues (VBM 6.398 eV at Γ, CBM 7.277 eV), and is
  typical of a small LCAO basis. The CBM bin edge depends on the k-mesh.

## What this showed about flower

Worked:
* function, shell and foreach nodes, including a foreach over a previous foreach's `outputs.items`;
* `when:` conditions and typed output contracts;
* per-attempt work directories and passing a directory between nodes (`${scf.outputs.out_dir}`);
* detached driver, `wait`, `status`, `log`, `output` and `report`;
* `rerun` of a single foreach child, with the parent re-collecting and `converge` re-running.

Issues found:
1. **`rerun <foreach-parent>` did not re-execute the children. Fixed.** See `tests/core/BUGS.md` #16.
   `Engine.rerun` marked only the node and its *descendants* stale. Foreach children are upstream of
   their collector, so `rerun fermi` only re-collected the old `fermi[i]` results. It now re-executes
   every item, forced, and then everything downstream.
2. **The cache key of a `function` node ignored the called module's source. Fixed.** See #17. After
   `abacus_si.py` was edited, the nodes still counted as "unchanged", in reruns and in `fork`s. The
   decl hash now includes a content digest of the called module's local package. It still does not
   cover a `shell` node that runs `python ${plan.dir}/abacus_si.py`.
3. **`rerun --detach` is not accepted.** `rerun` already continues in the background by default, but
   `resume` takes `--detach`, so the flags are inconsistent.
4. **The `review` agent failed with `auth` (401).** The standalone `claude` CLI on this machine has an
   expired OAuth token, and a manual `claude -p` with the same scrubbed environment fails the same way.
   This is an environment problem, not flower, which classified it correctly as `auth`. To fix it,
   run `claude` in a terminal, use `/login`, then `flower rerun RUN review`.
