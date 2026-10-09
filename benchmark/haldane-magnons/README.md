# Benchmark: magnons of the open S = 1 Heisenberg chain by DMRG

**Paper:** E. S. Sørensen and I. Affleck, "Large-scale numerical evidence for Bose condensation in the S = 1
antiferromagnetic chain in a strong field", Phys. Rev. Lett. 71, 1633 (1993),
[arXiv:cond-mat/9306036](https://arxiv.org/abs/cond-mat/9306036). One of the first DMRG studies: the lowest state
of each total S^z = M of open chains up to L = 100 is the end spins' triplet plus m = M − 1 magnons that behave as
free fermions in a box, Eq. (8): E_{m+1} − E_1 = mΔ + (πv)²/(2Δ(L−1)²) Σ n_i², n_i = 1..m. Claims: Table I
(L = 100 energies), Δ = 0.4107(1) and v = 2.49(1) from the fit, the 2- and 3-magnon fits (Σn² = 4.80(6) and
14(2), consistent with 5 and 14), the magnon profiles of Figs. 1–3 and the parities 1−, 2+, 3−, 4+.

(The PDF's text layer drops a Δ from Eq. (2): the dispersion is ε(k) = Δ + v²(k − π)²/(2Δ), which gives the
paper's v = 2.49 from its coefficient 74.7.)

**Method here:** TeNPy 1.1.1 finite DMRG with S^z conserved ([`haldane.py`](haldane.py)), the lowest state of
each M = 0..4 from a Néel product state with M sites raised; checked against exact diagonalization of L = 10
(all sectors agree to 2·10⁻¹⁴), converged in the bond dimension at L = 100 (χ = 256: energies move by 10⁻⁸ for
M ≤ 2 and 3·10⁻⁶ for M = 4 from χ = 128); then L = 20, 24, …, 100 and 120, 140, 160 at χ = 256, with ⟨S^z_i⟩, the
bond energies and the parity ⟨ψ|P|ψ⟩ of each state. [`analysis.py`](analysis.py) fits c + a/(L−1)² + b/(L−1)³.

## Result (run `haldane-magnons-20261007-035056-06a4`): 12 of 18 claims

| claim | paper | this run | |
|---|---|---|---|
| Table I, L = 100: E(1−), E(2+), E(3−) | −138.940086, −138.522461, −138.08557 | −138.940086, −138.522464, −138.085598 | ✓ ✓ ✓ |
| Table I: E(4+) | −137.603 | **−137.620734** | ✗ |
| Δ (E₂ − E₁, 40 ≤ L ≤ 100) | 0.4107(1) | 0.41033 (to L = 160: 0.41039) | ✗ |
| v | 2.49(1) | 2.521 (coefficient 76.4 vs 74.7(4)) | ✗ |
| 2 magnons: constant, coefficient, Σn² | 0.823(1), 359(5), 4.80(6) | 0.8213, 365, 4.78 | ✓ ✓ ✓ |
| 3 magnons: coefficient, Σn² | 1030(150), 14(2) | 946, 12.4 | ✓ ✓ |
| Fig. 1 (1 magnon): ⟨S^z⟩ and bond energies vs free fermion, max deviation / peak | agreement | 0.098, 0.088 | ✓ ✓ |
| Fig. 2 (2 magnons) | good agreement | 0.227, 0.230 | ✗ ✓ |
| Fig. 3 (3 magnons) | "not as spectacular" | 0.404, 0.394 | ✗ ✗ |
| parities of the lowest S^z = 1..4 states | −, +, −, + | −, +, −, + (|⟨P⟩| = 1.000) | ✓ |

What the misses show:
- **The paper's 4+ energy is not converged.** This run's lowest S^z = 4 state lies 0.018 below Table I, with the
  same parity (+), converged in χ (3·10⁻⁶) and variational: the paper's value is an upper bound its DMRG (81 states
  kept) did not reach; it says itself that DMRG "works progressively worse for states with higher magnetization".
- **Δ:** the raw gaps agree with the paper's (E₂ − E₁ at L = 100: 0.417622 vs 0.417625 from Table I); the
  difference is the extrapolation. This run's 0.41033–0.41039 and the paper's 0.4107(1) lie on either side of the
  value later work settled on (White & Huse 1993: 0.41050(2)), the paper's 2σ above it and this run's 1–2·10⁻⁴
  below. v follows the fitted coefficient (76.4 vs 74.7).
- **The magnon profiles** have the free-fermion shape (one hump, two, three) but deviate from the formula in a box
  of length L − 1 by 23 % (2 magnons) and 40 % (3 magnons) of their peak, in the bulk as much as near the ends:
  the magnons keep further from the end spins than free particles would. The paper's agreement was by eye; the
  10 % threshold here is stricter. The bond energies follow Δ × ⟨S^z⟩ as the paper says, within 9 % for one magnon.

## How it ran (one flower run, started with `flower start`, grown with `flower add`)

`paper` (PDF sha256; Table I and the fit numbers checked against its text) → `check` (DMRG vs exact
diagonalization) → `chi` (χ = 64, 128, 256 at L = 100) → `levels` (24 lengths, 4 cores each: 61 minutes, the
longest L = 160 at 29 minutes) → `analysis`.

This was the first study on the simplified flower: every step printed its outputs as its last line (no
`$FLOWER_OUTPUTS` redirection), settings went through `--set`, and `--follow` was used where a result was needed
at once. What it found:
- `flower add` said "queued again" of a step that had never run; it now says "queued".
- In the analysis (not flower): the parities were first divided by that of the S^z = 0 state, which DMRG returns
  as a mixture of the degenerate 0+ singlet and 1− triplet (⟨P⟩ = 0); and the first fit included L = 20–36, where
  the three-term form leaves residuals of 3·10⁻⁴. Both were fixed by editing `analysis.py` and
  `flower rerun RUN analysis`; the record keeps all three analysis attempts.

## Reproduce

[`protocol.yaml`](protocol.yaml) (the TeNPy and analysis recipes pinned), [`expected.json`](expected.json),
[`PROTOCOL.md`](PROTOCOL.md): `flower run haldane-magnons/protocol.yaml --follow --inputs my-machines.json`, then
`flower compare RUN haldane-magnons/expected.json`.

## Reproduced on a Slurm cluster (run `haldane-magnons-20261008-220637-153b`)

The protocol, run on an SCNet cluster (Slurm, a CPU partition, CentOS 7) with
`flower run haldane-magnons/protocol.yaml --machine remote=<that machine>`, gives the same physics: 12 of 18
claims; Δ = 0.41033145380 (2·10⁻¹² from this run's), v and Σn² to 10⁻⁹, the 120 energies (24 lengths × 5 states)
within 2.8·10⁻⁸, the parities identical. `flower compare RUN expected.json --atol 1e-9 --ignore sz --ignore
max_trunc_err --ignore seconds` leaves only the parity of the S^z = 0 state, which is a mixture of the degenerate
singlet and triplet that DMRG settles on differently each time (as is its ⟨S^z⟩ profile). 33 jobs over 6.5 hours,
most of it waiting for the user's queue limit (about 10 jobs) and with TeNPy's pure-Python build.

What it took (all in `tests/core/BUGS.md`): the compute nodes have no internet, so environments now install on the
login node; that node killed the install when it ran detached, so it was installed with `flower env replay` (an
attached ssh session); TeNPy 1.1.1 has no wheel for CentOS 7's glibc, so the recipe falls back to its pure-Python
build (re-frozen; the protocol pins the new version); a run started with `--machine` could not be rerun (#65); jobs
past the queue limit failed instead of waiting (#67).

## Files
- [`paper.py`](paper.py): the paper and its numbers, checked against the PDF text.
- [`haldane.py`](haldane.py): DMRG of one length (all sectors), and the exact-diagonalization check.
- [`analysis.py`](analysis.py): the fits, profiles and parities, `report.md`, `magnons.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`.
