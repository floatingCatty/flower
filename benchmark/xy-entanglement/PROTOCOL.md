# Reproduce the entanglement entropy of spin blocks in critical and non-critical XY/XX chains (Vidal, Latorre, Rico, Kitaev, PRL 90, 227902 (2003), arXiv:quant-ph/0211074): the log2(L)/3 and log2(L)/6 laws of Eqs. (14)-(15), the saturation of Eq. (16) and the anisotropy dependence of Eq. (18), by free-fermion correlation matrices

Exported from run `xy-entanglement-20261005-132320-e713`: the steps behind `analysis`.

## Reproduce

```bash
flower run protocol.yaml -y
flower compare RUN expected.json      # RUN: the id the first command prints
```

## Needs

- software: envs/analysis (frozen recipes, installed by the run)

## Steps

| step | what it establishes | took |
|---|---|---|
| `check` | The method against exact diagonalization (open 12-spin chains, four (gamma, h) points, blocks of 1-6 spins) and the infinite-chain correlation matrix against the middle of a 4000-spin chain. Read ed_vs_majorana_max (below 1e-8) and infinite_vs_long_chain_max (below 1e-6). | 4m54s |
| `curves` | S_L of the infinite chain for one (gamma, h): every L up to 200 for the critical and XY curves, or a few large blocks (500, 1000, 2000 spins) where the entropy saturates off criticality. Read L and S. | 11 items, 2m47s in total |
| `analysis` | The paper's laws against this run's curves: the 1/3 and 1/6 slopes of S_L in log2 L (Eqs. 14, 15), the constant k2 (printed as pi/3), the saturation near a = 1 (Eq. 16) and the anisotropy dependence at the critical field (Eq. 18). Read n_reproduced of n_claims and report.md. | 11s |
