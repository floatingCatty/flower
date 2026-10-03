# Benchmark: reproducing the theory of a Science paper (twisted trilayer graphene)

**Paper:** S. Turkel *et al.*, "Orderly disorder in magic-angle twisted trilayer graphene", Science 376, 193
(2022), [doi:10.1126/science.abk1895](https://doi.org/10.1126/science.abk1895) (arXiv:2109.12631). An STM paper
whose theory has four parts. This workflow redoes each of them and checks every theoretical claim against
the published figures:

| part | paper | how it is redone here |
|---|---|---|
| corrugation model (Fig. 1B) | sum of cosines of the local bilayer disregistry | the authors' MATLAB recipe ported to Python ([`corrugation.py`](corrugation.py)), with a quantitative check of the lattice of maxima |
| continuum model, single particle (Fig. 2E SP1/SP2, Fig. 3F, Fig. 4D/E, fig. S14B) | mirror-symmetric TTG, heterostrain on the middle layer, displacement field | **our own vectorised implementation** ([`ttg.py`](ttg.py)), checked against the authors' script to 0.0002 meV ([`check_sp.py`](check_sp.py)) |
| Hartree–Fock at charge neutrality (Fig. 2E HF) | projected HF, gate-screened Coulomb, ε = 10 | **our own implementation** ([`ttg_hf.py`](ttg_hf.py)), and **the authors' released code** run unchanged except for its cluster paths ([`authors_hf.py`](authors_hf.py)) |
| structural relaxation (Fig. 3G/H, fig. S10) | configuration-space relaxation of incommensurate trilayers (Zhu *et al.*, PRB 101, 224107) | the authors' Julia code (too large to rewrite), driven by [`relax.jl`](relax.jl); the local-twist analysis is our own ([`relax_analysis.py`](relax_analysis.py)) |

The authors' code and data come from their Harvard Dataverse record
([doi:10.7910/DVN/QWIFR1](https://doi.org/10.7910/DVN/QWIFR1), version 1.1, CC0), downloaded by the first
step and checked against the server's MD5 sums. The supplementary materials (with the model parameters) are
not openly available; the parameters were taken from the released scripts.

## How it was built: inside one flower run, from the first step

`flower start`/`add`/`rerun --follow` were made for this benchmark, after the first attempt drifted into
running things by hand (see `tests/core/BUGS.md` #21). Everything after that, including every failed attempt,
is in the run's log (`flower log RUN`): the environments were explored with `flower remote exec`, frozen
(`envs/moire-py`, `envs/julia`) and replayed from scratch; every computation, probe and fix is a step or a
recorded amendment.

## Run

```bash
cd benchmark
../.venv/bin/flower run ttg-twistons/plan.yaml --inputs remote-inputs.json   # {"host": "<ssh alias>", ...}
```

Single-particle and Hartree–Fock steps run locally (seconds to minutes); the relaxations (hours at N = 54)
and the authors' Hartree–Fock run on the remote machine.

## Result (run `ttg-twistons-20261003-171249-8e40`): 10 of 15 claims reproduced

| fig. | claim | paper | this run | |
|---|---|---|---|---|
| 1B | AtA corrugation is hexagonal, AtB honeycomb | — | AtA 1.05 maxima/moiré cell, 6 neighbours; AtB 2.05, 3 neighbours | ✓ |
| 2E | SP1 severely underestimates VHS separation and width | ≈ 0 | 1.2 / 1.8 meV | ✓ |
| 2E | SP2 (+30 % velocity) gives the separation | ≈ 18 meV | 18.2 meV | ✓ |
| 2E | SP2 widths ≈ 6× smaller than experiment | ≈ 4 meV | 4.3 meV | ✓ |
| 2E | the authors' released HF code gives their HF numbers | ≈ 16 / 19 meV | 17.1 / 17.8 meV | ✓ |
| 2E | our independent HF of the scheme as described | ≈ 16 meV | ≈ 62 meV | ✗ |
| 2E | experimental numbers re-derived from the raw spectrum | 18 / 23 meV | 20.9 / 29.0 *samples* | ✗ (axis) |
| S14B | the displacement field barely changes separation and widths | no change | 17.4–18.2 / 4.3 meV for D = 0–40 meV | ✓ |
| 3F | 1.8° flat bands split by ≈ 40 meV | ≈ 40 meV | 48 meV | ✓ |
| 3F | 0.55 % heterostrain broadens the flat-band peaks | attenuated | FWHM 12 vs 3 meV | ✓ |
| 4D/E | flat-band resonance at ν_P = 2.4 | 2.4 | peaks aligned within 3 meV for ν_P ∈ [1.6, 3.7] | ✓ |
| 3G/H | relaxation gives universal AtA stacking | all AtA | AtA domains ≈ 58 % of AAA sites | ✗ |
| 3H | trimodal local twist (plaquette / soliton / twiston) | 1.46 / 1.53 / 1.575° | domains at 1.60°; walls not resolved by our estimators | ✗ |
| S10 | θ_I locks to the smaller twist for δθ ≲ 0.5° | 1.5° | locks near the mean twist | ✗ |

**What the failures mean.**
* *Hartree–Fock.* The authors' own code, run unchanged except for its cluster paths, reproduces the paper.
  Our independent implementation of the scheme as the text and code describe it does not: its Dirac-band
  self-energies agree with theirs within 2–4 meV, but its flat-band self-energy is ≈ +20 meV on both bands
  (theirs −6 and +4) and the flat bands split by ≈ 60 meV. Imposing C2zT, their active-band choice,
  first-shell Hartree or their 13 plain iterations does not close the gap (step `hf-probe`, 9 variants).
  The remaining difference is somewhere in the flat-band Hartree/Fock bookkeeping and is not resolved here.
* *Experimental reference.* The raw spectrum has no energy axis; the paper's 18/23 meV correspond to a
  0.8 mV sample spacing (a ±160 mV sweep), so the paper's numbers are used as the reference.
* *Relaxation.* The authors' Julia code ran at the paper's resolution (N = 54, GSFE ×10, 1 h 41 min), plus a
  δθ scan and a physical-GSFE run at N = 36. The relaxed structure has large AtA triangles whose local twist
  is the *mean* of the two twists, separated by out-of-phase walls. Our nearest-neighbour AAA estimator is
  unreliable at the walls, and the curl-based estimator of the authors' `plot_relax.m` gives implausible
  values with the sign conventions we inferred. Inconclusive rather than refuted.

Each of these was investigated with more steps in the same run (`hf-probe`, `authors-hf-inspect`, the
relaxation estimators); the run log has every attempt.
