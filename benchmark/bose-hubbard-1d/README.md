# Benchmark: the Kosterlitz–Thouless point of the 1D Bose–Hubbard model by DMRG

**Paper:** T. D. Kühner, S. R. White and H. Monien, "One-dimensional Bose-Hubbard model with nearest-neighbor
interaction", Phys. Rev. B 61, 12474 (2000), [arXiv:cond-mat/9906019](https://arxiv.org/abs/cond-mat/9906019).
At density 1, the Mott insulator gives way to a superfluid at a Kosterlitz–Thouless point t_c, where the Luttinger
parameter K of the one-body correlation Γ(r) = ⟨b†_i b_{i+r}⟩ ~ r^(−K/2) reaches 1/2. The tested claims are the final
estimate **t_c = 0.297(10)** and Table I: t_c from fits of K over five intervals of r, which drifts from 0.2874
(4 ≤ r ≤ 8) to 0.3107 (48 ≤ r ≤ 64).

**Method here:** finite DMRG with TeNPy (the frozen `envs/tenpy` recipe) on a remote workstation
([`bh_dmrg.py`](bh_dmrg.py)): open chains, U = 1, at most 5 bosons per site, particle number conserved, bond
dimension χ = 300. The scan covers L = 128 and 256 at ten values of t from 0.27 to 0.33, and L = 512 at the five
values nearest t_c. Γ(r) is measured for pairs centred on the middle of the chain. [`kfit.py`](kfit.py) fits
K = −2 d ln Γ / d ln r on each interval. As in the paper, it takes K from the two largest systems: the larger L
gives K_u, the linear extrapolation in 1/L gives K_l, K is their mean and the error is half their difference. t_c is
where K(t) crosses 1/2.

## Result (run `bose-hubbard-1d-20261004-182316-bf14`): the final estimate is reproduced, and the drift only in part

| fit interval | paper (Table I) | this run | difference |
|---|---|---|---|
| 4 ≤ r ≤ 8 | 0.2874(1) | 0.2870 | −0.0004 |
| 8 ≤ r ≤ 16 | 0.2938(1) | 0.2918 | −0.0020 |
| 16 ≤ r ≤ 32 | 0.2968(3) | 0.2942 | −0.0026 |
| 32 ≤ r ≤ 48 | 0.3062(3) | 0.2953 | −0.0109 |
| 48 ≤ r ≤ 64 | 0.3107(100) | 0.2953 | −0.0154 (1.5σ) |
| **t_c** | **0.297(10)** | **0.2942** (16 ≤ r ≤ 32) | ✓ within the paper's error |

`kfit.py` reports "5 of 6", but that count is generous. Its tolerance, |Δ| ≤ max(3σ, 0.005), is effectively a fixed
0.005 because the paper's quoted errors are 10⁻⁴, so the short intervals pass at 4–26σ of the paper's errors. A
plainer reading:
- **The final estimate reproduces:** t_c = 0.294 against 0.297(10).
- **The drift with the fit interval is there,** in the same direction, but weaker. t_c rises from 0.287 to 0.295
  and then stays flat, where the paper's keeps rising to 0.306 and 0.311.
- **The long intervals disagree** (32–48 and 48–64). This run cannot say why. The candidates are its smaller
  systems: L ≤ 512, and r up to 64 is a quarter of the chain or more at L ≤ 256. Others are the bond dimension
  (χ = 300; truncation errors 2–4·10⁻⁶ at every size) and the cutoff of 5 bosons per site. A rerun at L = 1024 and a
  larger χ is the obvious test, at about 6 hours per point on this machine.
- **The ±0.000 entries in the 48–64 column of `report.md`** are at values of t that only have L = 128 and 256, where
  this interval reaches the chain's middle and the error estimate collapses. Those values carry no weight in t_c.

The DMRG cost was 1.3 hours for the ten L = 128 points, 4.8 hours for L = 256 and 6.5 hours for L = 512 (1.5 hours
for the longest point), spread over the remote's cores by the cluster's cpu budget.

## How it ran (one flower run, started with `flower start`)

`smoke` (L = 64 and 128 at t = 0.3, to size the campaign) → `kfit-smoke` (tests the fit script on them) →
`dmrg` (25 points, a foreach that grew as the scan was refined) → `kfit-preview` (the fit on the points finished so
far, while the rest ran) → `kfit`. The plan reached generation 9 through recorded amendments: points added to the
running foreach and settings changed mid-run, without re-running finished points. It shared the remote with
the S22 campaign through the project's cpu budget. No new flower issue came up in this run.

## Files
- [`bh_dmrg.py`](bh_dmrg.py): the DMRG ground state and Γ(r) for one (L, t).
- [`kfit.py`](kfit.py): K per interval and t, t_c per interval, `report.md`, `kfit.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits to the scan.
