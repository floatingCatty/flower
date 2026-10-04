# Benchmark: the 2D Heisenberg antiferromagnet by stochastic series expansion QMC

**Paper:** A. W. Sandvik, "Finite-size scaling of the ground-state parameters of the two-dimensional Heisenberg
model", Phys. Rev. B 56, 11678 (1997), [arXiv:cond-mat/9707123](https://arxiv.org/abs/cond-mat/9707123). SSE
quantum Monte Carlo of the S = 1/2 square-lattice antiferromagnet at β = 8L for L = 4…16 (Table II), and
extrapolations to L → ∞ constrained by chiral perturbation theory: **E = −0.669437(5), M = 0.3070(3),
ρ_s = 0.175(2), χ⊥ = 0.0625(9), c = 1.673(7)** (Table III).

**Method here:** SSE with operator-loop updates (Sandvik 1999), the successor of the paper's local updates, in
numba (the frozen `envs/numba` recipe) on a remote workstation ([`sse.py`](sse.py)). β = 8L as in the paper,
L = 4…16, 8–16 independent chains of 2.5·10⁵ sweeps per size (2–4·10⁶ sweeps per size; the paper used 10⁷–3·10⁸).
The measured quantities are the energy from the expansion order, S(π, π) averaged over imaginary time, C(L/2, L/2),
winding numbers (ρ_s), and the time-integrated m(2π/L) (χ). The fits ([`fits.py`](fits.py)) are the paper's
unconstrained ones (Eq. 39, L ≥ 6). The tested claims are the extrapolated parameters and the 21 per-size values
of Table II, parsed from the paper's text.

## Result (run `heisenberg-2d-20261004-172331-d4e1`): 7 of 7 claims reproduced

| | paper (constrained fit; unconstrained) | this run (unconstrained) | |
|---|---|---|---|
| Table II: E, S(π,π), C(L/2,L/2), L = 4…16 | 21 values | 21/21 within 3σ (worst \|z\| 1.5) | ✓ |
| E (L → ∞) | −0.669437(5); −0.66943(2) | **−0.669436(31)** | ✓ 0.0σ |
| M from S(π,π) | 0.3070(3); 0.3062(6) | 0.3063(11) | ✓ |
| M from C(L/2,L/2) | 0.3070(3); 0.3068(9) | 0.3099(18) | ✓ 1.6σ |
| ρ_s | 0.175(2); 0.179(4) | 0.1807(10) | ✓ 2.5σ (0.4σ from the unconstrained fit) |
| χ⊥ | 0.0625(9); 0.063(1) | 0.0632(4) | ✓ |
| c = √(ρ_s/χ) | 1.673(7); 1.69(2) | 1.691(7) | ✓ 1.8σ (0.0σ from the unconstrained fit) |

The run's numbers sit closest to the paper's *unconstrained* fits, which are the fits it repeats. The constrained
fit additionally imposes the chiral-perturbation-theory relations between parameters.

## The finite-size stiffness: a normalization the paper does not spell out

The verify step first failed on ρ_s(4): 0.279 here against the paper's quoted 0.04840. To decide which was right,
an `ed-twist` step diagonalized the 4×4 lattice with a twist φ on every x bond, using the QuSpin recipe from
another benchmark. It gives ρ_s = (3/2)(1/N) d²E₀/dφ² = 0.2783, matching the QMC's 0.2790(7). The QMC and its
winding estimator are therefore correct for this definition, and the paper's finite-size ρ_s(L) is normalized
differently. The paper itself notes that definitions differ between groups at finite L. The two agree where it
matters, as L → ∞: this run's ρ_s(L) falls from 0.278 (L = 4) to 0.199 (L = 16) and extrapolates to 0.181.

## How it ran (one flower run, started with `flower start`)

`paper` (PDF, sha256, Table II parsed with its errors) → `ed-twist` (exact 4×4 stiffness) → `verify` (SSE at 4×4
against E and S(π,π) from Table I and the ED stiffness; also checks that every configuration is periodic and every
winding a whole number) → `sse` (80 chains) → `fits`. The campaign shared the remote with the S22 rescue
through the project's cpu budget. This run's budget was raised to 32 with `flower sync`, so its single-core chains
used the 2 cores S22 left free and then the whole machine.

The verify step caught two estimator errors before the campaign: S(π,π) divided by N twice (16× too large), and
the stiffness question above. No new flower issue came up. Mid-run budget changes, `${step.partial}` previews, the
dependence of the campaign on its verification, and typed `--in` values all worked as intended.

## Files
- [`sse.py`](sse.py): SSE with operator loops, estimators, checkpoints, the `check` mode.
- [`ed_twist.py`](ed_twist.py): exact 4×4 stiffness with a twisted boundary.
- [`table2.py`](table2.py): the paper's Table II from its text layer.
- [`fits.py`](fits.py): per-size comparison and the extrapolations; `report.md`, `fits.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits (the cluster, `needs: ed-twist`).
