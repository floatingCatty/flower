# Benchmark: the critical point of the 3d Ising model by cluster Monte Carlo

**Paper:** A. M. Ferrenberg, J. Xu and D. P. Landau, "Pushing the limits of Monte Carlo simulations for the
three-dimensional Ising model", Phys. Rev. E 97, 043301 (2018), [arXiv:1806.03558](https://arxiv.org/abs/1806.03558).
Wolff cluster Monte Carlo at K₀ = 0.221654 for L = 16…1024, single-histogram reweighting, the maxima of
logarithmic derivatives of the magnetization and of cumulant derivatives (ν), their locations (K_c), and jackknife
errors that keep the cross-correlations between estimates from the same data. Results: **K_c = 0.221654626(5),
ν = 0.629912(86)**.

**Method here:** the same procedure at a much smaller scale. The Wolff kernel is in numba (the frozen
`envs/numba` recipe) on a remote workstation, for L = 8…64 with 4–8 independent chains per size and 10⁵–4·10⁵
measurements each ([`wolff.py`](wolff.py)). The analysis ([`fss.py`](fss.py)) uses the maxima of
d ln⟨|m|⟩/dK, d ln⟨m²⟩/dK, dU₄/dK and dU₂/dK for ν, with the correction exponent ω = 0.83 fixed. K_c comes from
the locations of those maxima plus the specific heat and the finite-lattice susceptibility. The jackknife leaves
one block out of every size at once. The claim tested is agreement within this run's (much larger) error bars.

## Result (run `ising-3d-20261004-012818-42b9`)

| | paper (L = 16…1024) | this run (L = 8…64, 9.6·10⁶ measurements) | |
|---|---|---|---|
| K_c | 0.221654626(5) | **0.2216505(51)** | 0.8σ ✓ |
| ν | 0.629912(86) | 0.607(11), and 0.55–0.62 depending on the smallest L in the fits | 2.1σ, not a test at the paper's precision |
| U₄ at the paper's K_c | (critical value ≈ 0.4655, Hasenbusch) | 0.482 (L = 8) → 0.468 (L = 64) | approaching it ✓ |

K_c is reproduced to 5·10⁻⁶, a thousand times less precisely than the paper, which used L up to 1024. Every
quantity's own K_c estimate lies within 1–2σ of the paper's, except the specific heat. Its peak at these sizes is
dominated by the analytic background, so it is left out of the mean. ν is consistent with the paper but is not
pinned down by lattices this small: corrections to scaling (ω = 0.83, fixed) leave it moving with the fit range.

**The paper's combination of estimates did not carry over.** The paper combines its estimates with the jackknife
covariance matrix (weights G⁻¹1/(1ᵀG⁻¹1), after Weigel and Janke). Here the weights came out as 5.1 and −3.2:
d ln⟨|m|⟩/dK and d ln⟨m²⟩/dK give almost perfectly correlated estimates, so the weighting extrapolates on their
small differences. It gives ν = 0.626(4), which looks like a match, and K_c = 0.2216601(17), which looks like
3σ off. Neither error is reliable, so the report shows these values but does not count them, and uses the plain
mean with its jackknife error (correlations included). Going from 20 to 100 jackknife blocks did not stabilise
the weights. The analysis was restructured so that 100 blocks cost 2 minutes instead of hours: the moments are
summed per block on a fine K grid, so each leave-one-out estimate is a subtraction.

## A bug the workflow caught before the campaign

The first smoke test gave U₄ ≈ 0.58 at the paper's K_c, where the critical value is about 0.465, and an energy
per spin of −1.09 instead of about −0.99: too ordered. A `verify` step then compared the kernel with exact
enumeration of all 2²⁷ states at L = 3. Wolff was off by z = 130; an independent Metropolis sampler agreed
(|z| ≈ 1). A `rngcheck` step showed numba's `randint` is uniform. A diagnostic step showed that two Wolff
variants (flip on the fly, and mark-then-flip) shared the bias. The common cause was the measurement schedule:
measuring each time the flipped spins reach N makes the measurement time depend on the trajectory, which
over-samples the states right after large clusters. With a fixed number of clusters between measurements
(chosen once during thermalization), Wolff agrees with exact enumeration (|z| ≈ 1). The production step `mc` now
`needs: verify`, so no campaign can start on an unverified kernel.

## How it ran (one flower run, started with `flower start`)

`paper` → `env-numba-remote` (a new frozen recipe) → `verify` (exact enumeration, Metropolis reference) →
`mc-smoke` + `fss-smoke` (L ≤ 16, the pipeline end to end) → `mc` (the campaign) → `fss`. The remote's cores were
shared with the Δ-test run through the project-wide cpu budget (`cpus:` on both runs' cluster, same host). The Δ
run's budget was lowered from 28 to 24 with `flower sync` so this run could start.

What this benchmark found in flower (tests/core/BUGS.md #46):

* a failed step's outputs (the verdict of a check that exits 1) were invisible: `flower logs` now shows them.

## Files
- [`wolff.py`](wolff.py): Wolff kernel, checkpointed chains, the `check` mode (exact enumeration, Metropolis).
- [`rngcheck.py`](rngcheck.py): uniformity of numba's integer draws.
- [`fss.py`](fss.py): reweighting, maxima, fits, jackknife, report and figure.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits (the cluster, `needs: verify`).
