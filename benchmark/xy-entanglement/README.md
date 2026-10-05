# Benchmark: entanglement entropy of critical spin chains (free fermions)

**Paper:** G. Vidal, J. I. Latorre, E. Rico and A. Kitaev, "Entanglement in quantum critical phenomena",
Phys. Rev. Lett. 90, 227902 (2003), [arXiv:quant-ph/0211074](https://arxiv.org/abs/quant-ph/0211074). The von
Neumann entropy S_L of a block of L spins in the ground state of infinite XY chains: logarithmic growth with the
central charge at criticality, saturation away from it. The tested statements: Eq. (14) S_L = log₂(L)/3 + k₁(a) for
the XX chain; Eq. (15) S_L = log₂(L)/6 + k₂ with k₂ ≈ π/3 for the critical Ising chain; Eq. (16) the saturated
entropy near the critical point grows as log₂(1/|1 − a|)/6; Eq. (18) at the critical field, S_L(γ = 1) − S_L(γ)
→ −log₂(γ)/6.

**Method here:** [`ent.py`](ent.py): the Jordan–Wigner Majorana form of the chain; the ground state's Majorana
correlation matrix is the orthogonal polar factor of the coupling matrix, taken mode by mode in Fourier space for the
infinite chain; S_L from the eigenvalues of the block's 2L × 2L correlation matrix. Its `check` compares the method
with exact diagonalization of open 12-spin chains at five (γ, h), blocks of 1–6 spins (agreement 4·10⁻¹⁴), and the
infinite chain with the middle of a 4000-spin chain (6·10⁻¹³). [`analysis.py`](analysis.py) fits the laws on
20 ≤ L ≤ 200, with the XX chain's Fermi-surface oscillation cos(2k_F L)/L as a fitted correction, and evaluates the
saturation both from blocks of 2000 spins and from the exact half-chain entanglement spectrum of the transverse Ising
chain (Peschel's corner-transfer-matrix formula).

## Result (run `xy-entanglement-20261005-132320-e713`): 6 of 7 claims reproduced

| claim | paper | this run | |
|---|---|---|---|
| Eq. (14) XX, h = 0: slope in log₂ L | 1/3 | **0.3333** (k₁ = 1.0474) | ✓ |
| Eq. (14) XX, h = 0.5: slope; k₁ depends on the field | 1/3 | 0.3334 (k₁ = 0.9781) | ✓ |
| Eq. (15) critical Ising: slope | 1/6 | 0.1690 | ✓ |
| Eq. (15) critical Ising: k₂ | π/3 = 1.0472 | **0.678** | ✗ |
| saturated S (L = 2000) = the corner-transfer-matrix formula | | to 1.8·10⁻¹⁰ | ✓ |
| Eq. (16) a → 1: slope of saturated S in log₂(1/|1 − a|) | 1/6 | **0.1667** (1 − a → 10⁻⁸) | ✓ |
| Eq. (18) S_L(1) − S_L(γ), γ = 0.25, 0.5, 2 | 0.3333, 0.1667, −0.1667 | 0.3333, 0.1666, −0.1667 | ✓ |

**k₂ ≈ π/3 is the XX constant.** The paper's text (the glyphs ≈ and π recovered from the PDF's bytes, which
pdftotext drops) prints k₂ ≈ π/3 for the critical Ising chain. This run's XX constant at zero field is
k₁ = 1.0474 = π/3 to 2·10⁻⁴, while the critical Ising constant is 0.678, as it must be from S_Ising(L) ≈
½ S_XX(2L) (which gives (k₁ + 1/3)/2 ≈ 0.69). The paper's π/3 belongs to Eq. (14).

**Eq. (16) is asymptotic.** Over a = 0.98–0.995 the saturated entropies give a slope of 0.180, and the exact
formula shows why: the slope is 0.172 between 1 − a = 10⁻² and 10⁻³ and reaches 1/6 only as a → 1.

## How it ran (one flower run, started with `flower start`)

`paper` (PDF, sha256, the equation lines of its text layer) → `check` → `curves` (11 items: XX at two fields,
critical Ising, three anisotropies, and five off-critical fields at blocks of 500–2000 spins) → `analysis`, all on
this machine within minutes.

What the run found in its own code: the first `entropy_of` kept the eigenvalues ν > 0 of the block's correlation
matrix. At zero field the XX chain splits into two decoupled Majorana chains, odd blocks carry exact zero modes, and
rounding put them on either side of zero: S_L of some odd blocks came out one bit too high or too low (the fitted slope
was 0.311). It now takes the upper half of the symmetric spectrum, and the check includes that case (`flower rerun
RUN check` re-ran the curves and the analysis below it).

## Reproduce

[`protocol.yaml`](protocol.yaml), [`expected.json`](expected.json), [`PROTOCOL.md`](PROTOCOL.md):
`flower run xy-entanglement/protocol.yaml -y`, then `flower compare RUN xy-entanglement/expected.json`.

## Files
- [`ent.py`](ent.py): the Majorana method, its check, curves and large blocks.
- [`analysis.py`](analysis.py): the fits, the corner-transfer-matrix saturation, `report.md`, `entropy.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`.
