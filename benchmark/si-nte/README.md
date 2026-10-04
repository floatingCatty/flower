# Benchmark: silicon's negative thermal expansion from first principles

**Paper:** G.-M. Rignanese, J.-P. Michenaud and X. Gonze, "Ab initio study of the volume dependence of dynamical
and thermodynamical properties of silicon", Phys. Rev. B 53, 4488 (1996), [arXiv:mtrl-th/9509002](https://arxiv.org/abs/mtrl-th/9509002).
Density-functional perturbation theory at several volumes, the quasi-harmonic approximation, and the
anomalous (negative) Grüneisen parameters of the zone-boundary TA modes that make silicon shrink on heating
between about 20 and 120 K.

**Method here:** Quantum ESPRESSO 7.5 (the frozen `envs/qe` recipe, installed and checked on this machine by
the workflow), LDA with the classic norm-conserving `Si.pz-vbc.UPF` (the paper: a Hamann pseudopotential),
the paper's 10 Ha cutoff and 4×4×4 shifted k mesh, DFPT on a 6×6×6 q mesh at five lattice constants →
interatomic force constants → TA(X), TA(L), phonon DOS → quasi-harmonic F(V,T) ([`qha.py`](qha.py)).

## Result (run `si-nte-20261003-182638-4ba2`): 16 of 18 claims reproduced

| claim | paper | this run | expt | |
|---|---|---|---|---|
| static LDA lattice constant (bohr) | 10.189 | 10.213 | 10.26 | ✓ |
| lattice constant with zero-point motion (bohr) | 10.197 | 10.23 | | ✓ |
| TA(X), TA(L) at a = 10.18 (cm⁻¹) | 140.5, 108.6 | 136.0, 103.9 | | ✓ (3–4 % low) |
| TA(X), TA(L) at a = 10.26 (cm⁻¹) | 147.4, 112.9 | 143.1, 108.4 | 149.8, 114.4 | ✓ |
| mode Grüneisen TA(X), TA(L) at a = 10.18 | −2.30, −1.81 | −2.40, −1.99 | | ✓ |
| mode Grüneisen TA(X), TA(L) at a = 10.26 | −1.78, −1.45 | −1.90, −1.63 | −1.4, −1.3 | ✓ |
| S(298.15 K) (J/K/mol) | 19.3 | 19.55 | 18.81 | ✓ |
| H(298.15) − H(0) (kJ/mol) | 3.285 | 3.299 | 3.217 | ✓ |
| negative thermal expansion (K) | 20–120 | 15–122 | 20–120 | ✓ |
| minimum of α (10⁻⁶/K) | | −0.61 at 74 K | ≈ −0.5 near 80 K | ✓ |
| C_P independent of pressure near (K) | 85 | 83 | | ✓ |
| static bulk modulus (Mbar) | 1.039 | 0.910 (20 Ry), **0.944 converged** | ≈ 0.98 | ✗ |

The bulk modulus is converged with the cutoff by 40 Ry (the `ecut-study` step: 0.910 → 0.943 → 0.945 →
0.944 Mbar), so the remaining 9 % is the pseudopotential (von Barth–Car here, Hamann in the paper), not
numerics. Everything that depends on the phonons, which is the point of the paper, agrees.

## Second run: is the bulk modulus the pseudopotential? (`si-nte-20261003-210316-6121`)

The pseudopotential and the cutoff are plan inputs, so the study was re-run with PseudoDojo v0.4 LDA (ONCVPSP,
Hamann 2013) at 40 Ry: `flower run si-nte/plan.yaml -i pseudo=dojo-lda -i ecut_ry=40 --reuse <first run>`. The paper
download was reused from the first run; everything that depends on the pseudopotential was recomputed.

| | paper (Hamann 1989) | vbc, 20 Ry | PseudoDojo LDA, 40 Ry | expt |
|---|---|---|---|---|
| a₀ static (bohr) | 10.189 | 10.213 | **10.193** | 10.26 |
| B₀ converged in cutoff (Mbar) | 1.039 | 0.944 | **0.961** | ≈ 0.98 |
| γ TA(X), TA(L) at a = 10.18 | −2.30, −1.81 | −2.40, −1.99 | −2.57, −2.09 | |
| negative thermal expansion (K) | 20–120 | 15–122 | 5–138 | 20–120 |
| min α (10⁻⁶/K) | | −0.61 at 74 K | −0.79 at 79 K | ≈ −0.5 near 80 K |
| S(298) (J/K/mol), H(298)−H(0) (kJ/mol) | 19.3, 3.285 | 19.55, 3.299 | 19.41, 3.272 | 18.81, 3.217 |

The modern pseudopotential moves a₀ onto the paper's value and raises B₀ by 0.02 Mbar, but both LDA
pseudopotentials give B₀ ≈ 0.95–0.96 Mbar, as LDA usually does for Si; the paper's 1.039 Mbar (taken from a
separate equation-of-state calculation, their ref. [Rignanese95]) is the outlier. 15 of 18 claims reproduce with
either pseudopotential; the zone-boundary anomaly comes out somewhat stronger with PseudoDojo.

## Repeatable from a fresh clone

The plan was run again from a fresh `git clone` (`flower run si-nte/plan.yaml`; QE installed from the frozen
recipe) and compared with the first run: `flower compare si-nte-20261003-182638-4ba2 <fresh run dir> --rtol 1e-6`.
Every quasi-harmonic result (`qha`) and the equation of state agree to 1e-6. Phonon frequencies differ by 1–2 ppm,
and the acoustic frequencies at Γ before the acoustic sum rule differ by 3·10⁻⁴ (2.762 vs 2.763 cm⁻¹, numerical
noise that the sum rule removes).

## How it ran (all inside one flower run, started with `flower start`)

`paper` (arXiv source + PDF, sha256) → `pseudo` (sha256) → `env-qe-here` (the frozen QE recipe, on a second
machine) → `eos` (7 volumes) → `phonons` (5 volumes, ~5 min each) → `ecut-study` (4 cutoffs) → `qha`.
What this benchmark found in flower and its recipes, each fixed with a test (`tests/core/BUGS.md` #24–#27):

* the QE recipe hung on this 96-core host (`ld1.x`, one OpenMP thread per core): the recipe now defaults
  `OMP_NUM_THREADS=1`, and environment checks have their own time limit (`check_timeout`, 10 min);
* `flower cancel` left the payload running when it was under a time limit (`timeout` makes its own process
  group): cancel now signals the job's whole session;
* a shell step's cache key ignored the script it runs, and `rerun` after a plan edit did not re-run unchanged
  foreach items: both fixed;
* `flower add` gained `--foreach` and `--in NAME=VALUE` (step inputs), so a whole study is written without YAML.

## Files
- [`si_nte.py`](si_nte.py): QE driver (scf, EOS series, pw → ph → q2r → matdyn), standard library only.
- [`qha.py`](qha.py): quasi-harmonic thermodynamics, mode Grüneisen parameters, the claims table and figure.
- [`plan.yaml`](plan.yaml): written step by step by `flower add` (and two hand edits), as the run log shows.
