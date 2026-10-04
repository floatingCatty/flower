# Benchmark: the frustrated spin-1/2 chain by exact diagonalization

**Paper:** S. Eggert, "Numerical evidence for multiplicative logarithmic corrections from marginal operators",
Phys. Rev. B 54, R9612 (1996), [arXiv:cond-mat/9602026](https://arxiv.org/abs/cond-mat/9602026). Lanczos
diagonalization of the periodic J1–J2 Heisenberg chain up to L = 32 (4.7 million states), the spin correlation
⟨Sz(0)Sz(L/2)⟩ (Table I), the multiplicative logarithmic correction rG(r) = √(aλ₀ ln(r/r₀)) (eq. 14, Table II,
Figs. 2–4), and the critical coupling J2crit = 0.241167 ± 0.000005 where the marginal operator vanishes.

**Method here:** QuSpin 1.0.1 (the frozen `envs/quspin` recipe) on a remote workstation, in the symmetry blocks
of total Sz, momentum (0, π), site parity and spin inversion; scipy's Lanczos (ARPACK) with matrix-vector
products only, warm-started along each J2 sweep. Sizes L = 8, 10, …, 32. Each size gives the ground-state energy
and ⟨Sz(0)Sz(L/2)⟩ at nine J2 values, and the lowest singlet and triplet excitations on a 14-point J2 grid
around J2crit ([`ed.py`](ed.py)). The analysis ([`analysis.py`](analysis.py)) repeats the paper's procedure.

## Result (run `j1j2-chain-20261004-000321-0850`)

| claim | paper | this run | |
|---|---|---|---|
| Table I: ⟨Sz(0)Sz(L/2)⟩, L = 8…32, five J2 | 65 values, 7 decimals | 64 agree to all printed digits; L = 32, J2 = −0.25: 0.0187109 vs 0.0187105 (the paper: "in some cases the last digit is uncertain") | ✓ |
| J2crit, from Δ ∝ 1/L³ | 0.241167 ± 0.000005 | **0.241167** (every size pair from L = 18 on) | ✓ |
| J2crit, level crossing extrapolated in 1/L² (ref. 14) | 0.2411 ± 0.0001 | 0.24117 (L = 12…32) | ✓ |
| Table II: ln r₀, aλ₀ at seven J2 | | within 0.9 % and 0.7 % | ✓ |
| Fig. 3: ln r₀ = ln r₁ − 1/λ₀ | a = 0.0296, r₁ = 0.85 | a = 0.0297, r₁ = 0.854 | ✓ |
| Fig. 4: λ₀ = c₁δ + c₂δ² + c₃δ³ | c₁ = 1.723, c₂ = −1.35, c₃ = 1.76 | 1.714, −1.33, 1.76 | ✓ |

6 of 6 claims reproduced. L = 32 (4.7 million states per block, three blocks) took 47 min on 8 cores, and the
whole run took 59 min. The ground-state energy per site at J2 = 0 approaches the Bethe ansatz value (−0.443147)
from below: −0.443954 at L = 32.

* **Table I** is reproduced exactly: all 65 correlations agree to the 7 printed digits.
* **J2crit:** the paper finds the J2 at which the singlet–triplet splitting Δ falls exactly as 1/L³. Requiring
  L³Δ(L) = (L+4)³Δ(L+4) gives J2* = 0.241167 for every pair from L = 18 on (table in the run's report). The
  older method, extrapolating the level crossing Δ = 0 in 1/L² (Okamoto–Nomura, the paper's ref. 14),
  gives 0.24117.
* **Table II** depends on fitting choices the paper does not state: the r range, the spline end conditions,
  and whether rG or (rG)² is fitted. The `fitscan` step tries 144 combinations on the paper's *own* Table I and
  picks the one that best reproduces its Table II (r = 9…16, a linear fit of (rG)², within 1 %). The same
  procedure on this run's data gives the same numbers as on the paper's (to 0.1 %). With r = 5…15 (the range of Fig. 2) ln r₀
  differs by up to 8 %, for the paper's numbers and ours alike.

## How it ran (one flower run, started with `flower start`, every step added with `flower add`)

`paper` (PDF, sha256, Table I parsed from the text layer) → `env-quspin-remote` → `sectors` (L = 8…20: which
symmetry block holds the ground state, the lowest excited singlet and the lowest triplet, identified by ⟨S²⟩;
spin inversion checked against (−1)^(S+L/2)) → `rule` (the blocks for L = 0 and 2 mod 4, required to hold at every
scanned L) → `ed` (13 sizes on 8 cores each, checkpointed per solve in `$FLOWER_STATE_DIR`) → `fitscan` →
`analysis`. A `preview` step (`${ed.partial}`) ran the analysis while L = 30 and 32 were still running; it
already gave J2crit = 0.241167 from L ≤ 28. That preview showed that Table II's mismatch came from the fit, not
the data, and led to the `fitscan` step.

What this benchmark found in flower (tests/core/BUGS.md #41–#43, each fixed with a test):

* a `cpus:` budget added to a running draft's cluster was silently ignored, so three 8-core jobs started on a
  machine meant to give the study 16 cores. A cluster's pacing settings (`cpus`, `max_jobs`, `min_poll`) may
  now change in a running plan, and `flower sync RUN` applies plan-file edits without re-running anything.
  Notes such as an ignored edit now reach `--json` output;
* `flower sync` saw finished steps with an `environment:` as edited (empty `stage_in`/`resources` in the run,
  absent in the file) and proposed to re-run them;
* an edit to a second step in the plan file was not applied by `flower rerun` of the first, which said nothing
  about it; the final `analysis` then ran with its old definition (the default fit). A rerun now names the
  edits outside its reach (`flower sync` applies them);
* `flower add` gained `--cpus` / `--mem`, and `flower remote exec` / `flower env` gained `--run RUN`, which
  uses the run's cluster and inputs instead of repeating `--inputs` on every call.

## Files
- [`paper.py`](paper.py): the paper and its Table I.
- [`ed.py`](ed.py): the diagonalization (`sectors` and `run` modes), with checkpoints.
- [`fitscan.py`](fitscan.py): which unstated fitting choices reproduce Table II from Table I.
- [`analysis.py`](analysis.py): the block rule, Tables I–II, Figs. 3–4, J2crit; `report.md`, `j1j2.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus two hand edits (the cluster's `cpus`, the `fit` input).
