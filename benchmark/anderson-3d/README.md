# Benchmark: the 3D Anderson transition by the transfer-matrix method

**Paper:** K. Slevin and T. Ohtsuki, "Corrections to scaling at the Anderson transition", Phys. Rev. Lett. 82,
382 (1999), [arXiv:cond-mat/9812065](https://arxiv.org/abs/cond-mat/9812065). The localization length λ of electrons
on L × L bars of the 3D Anderson model, from the transfer-matrix method to 0.05–0.1 %, and a finite-size-scaling
fit with corrections to scaling (an irrelevant variable and non-linear scaling variables). For the box distribution
of disorder (Table II): **W_c = 16.54 [16.53, 16.56], ν = 1.57 [1.55, 1.59], Λ_c = 0.576 [0.574, 0.578]**; without
corrections, on L ≥ 8 near W_c (Table III): W_c = 16.514, ν = 1.58.

**Method here:** [`tm.py`](tm.py): the transfer matrix at E = 0 with periodic boundaries, all L² columns propagated
by a numba kernel and re-orthonormalized by QR every 8 slices; Λ = 1/(γ_min L). Its `check` compares the kernel with
a dense numpy transfer matrix on the same potentials (agreement 10⁻¹⁵) and the symplectic pairing of the transfer
matrix's singular values. The campaign: L = 4, 5, 6, 8, 10, 12, 14 (the paper's sizes) at 13 disorders W = 15…18
(the paper's range), each to 0.1 %, 1.3–20 million slices per point. [`fss.py`](fss.py) is the paper's fit,
Eqs. (2)–(4), with its box-distribution form (n_R = 3, n_I = 1, m_R = 2, m_I = 0, 12 parameters), 95 % intervals
from 200 fits to resampled data; it is first checked on synthetic data from its own model (`fss-selftest`).

## Result (run `anderson-3d-20261005-021220-0e6e`): 6 of 6 claims reproduced

| | paper | this run (91 points) | |
|---|---|---|---|
| W_c, with corrections (Table II) | 16.54 [16.53, 16.56] | **16.549** [16.525, 16.576] | ✓ |
| ν, with corrections | 1.57 [1.55, 1.59] | **1.579** [1.559, 1.604] | ✓ |
| Λ_c, with corrections | 0.576 [0.574, 0.578] | **0.5748** [0.5709, 0.5777] | ✓ |
| W_c, no corrections, L ≥ 8, W ∈ [16, 17] (Table III) | 16.514 [16.507, 16.522] | 16.543 [16.513, 16.565] | ✓ |
| ν, no corrections (Table III) | 1.58 [1.53, 1.63] | 1.594 [1.516, 1.698] | ✓ |
| goodness of fit Q | 0.5 | 0.81 (χ² 68 for 79 dof) | ✓ |

The intervals here are 1.1–1.7 times as wide as the paper's: 91 points against its 224, at the same 0.1 % accuracy.

## The bug a diagnostic step caught

The first campaign gave a fit with ν = 0.61 and Q = 0. Its data had outliers far beyond their stated 0.1 %:
L = 14 at W = 15 came out at Λ = 0.688 against about 0.83 expected from L = 12. A trace step (`trace-l14`,
[`tmtrace.py`](tmtrace.py)) printed the block series of that point for its own seed and for another: with its own
seed Λ sat at 0.688 with abnormally small scatter, with the other at 0.837 with normal scatter. A disorder average
that does not fluctuate means the disorder was periodic: the first `tm.py` reseeded its random generator every few
slices with a number drawn from that same generator, a random map on 2³¹ seeds, which falls into a cycle after a few times 10⁴ calls (about √(π·2³¹/8)),
so every run's potential eventually repeated along the bar, some after a few hundred thousand slices. The kernel
now draws from one stream per run, seeded once.

The first version also stopped each run as soon as its estimated error dipped below the target, which stops the
runs whose error was underestimated by chance (runs to the same nominal accuracy took 0.3 to 9 million slices).
Each run's length is now fixed from a pilot of 20 blocks before it continues.

## How it ran (one flower run, started with `flower start`)

`paper` (Tables II and III with their intervals) → `check` (the kernel against a dense calculation) → `smoke`
(timing at L = 8, 12) → `fss-selftest` → `tm` (91 points, one core each, within a 24-core budget on a shared
96-core machine whose load reached 117–167 during the run: 85 core-hours in 5.8 hours) → `fss`. After the fixes,
`flower rerun RUN check` re-ran the check and the whole campaign below it. A settings-only edit raised the
campaign's time limit to 12 h and retried timeouts while it ran, without restarting anything.

What it showed about flower: a `cpus` budget holds every step on that cluster, so the one-minute fit self-test
queued behind 24 hour-long transfer-matrix runs; a second entry for the same host without a budget (`here`) took
the quick steps, and the reference now says so.

## Reproduce

[`protocol.yaml`](protocol.yaml), [`expected.json`](expected.json), [`PROTOCOL.md`](PROTOCOL.md):
`flower run anderson-3d/protocol.yaml -y`, then `flower compare RUN anderson-3d/expected.json`.

## Files
- [`tables.py`](tables.py): Tables II and III from the paper's text layer.
- [`tm.py`](tm.py): the transfer-matrix kernel, its check, and one (L, W) point.
- [`tmtrace.py`](tmtrace.py): block series of fixed-length runs (the diagnostic above).
- [`fss.py`](fss.py): the scaling fit with corrections, its self-test, `report.md`, `fss.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits.
