# Reproduce the ground-state parameters of the 2D S=1/2 Heisenberg antiferromagnet by stochastic series expansion QMC and finite-size scaling: E = -0.669437(5), M = 0.3070(3), rho_s = 0.175(2), chi = 0.0625(9), c = 1.673(7), and the per-size data of Table II (Sandvik, PRB 56, 11678 (1997), arXiv:cond-mat/9707123)

Exported from run `heisenberg-2d-20261004-172331-d4e1`: the steps behind `fits`.

## Reproduce

```bash
flower run protocol.yaml --inputs my-inputs.json
flower compare RUN expected.json      # RUN: the id the first command prints
```

## Inputs

- `host`: ssh alias of the remote machine
- `ssh_options`: extra ssh/rsync options, e.g. ["-F", "/path/ssh_config"]

## Needs

- machines: remote (see `clusters:` in protocol.yaml)
- software: envs/analysis, envs/numba, envs/quspin (frozen recipes, installed by the run)

## Steps

| step | what it establishes | took |
|---|---|---|
| `paper` | Downloads the paper and parses its Table II (E, S(pi,pi), C(L/2,L/2) for L = 4..16, with errors), the per-size data every later comparison uses. Read table2_rows: 7 means the table was found intact. | 1s |
| `ed-twist` | Exact diagonalization of the 4x4 lattice with a twist on every x bond: an independent value of the spin stiffness rho_s(4) for the verify step. It gives 0.2783, not the paper's quoted 0.0484 (a different finite-size normalization). | 4s |
| `verify` | Checks the SSE kernel before any campaign: at 4x4 its energy and S(pi,pi) must match exact diagonalization (the paper's Table I) and its stiffness the ed-twist value, every sampled configuration must be periodic with whole windings. ok=true means the kernel can be trusted. | 14s |
| `sse` | The campaign: SSE quantum Monte Carlo at beta = 8L for L = 4..16, 8-16 independent chains of 250000 sweeps per size. Each item's E, S, C, rho, chi are bin averages for one chain; the fits step combines them. | 80 items, 6h15m in total |
| `fits` | Compares every size with the paper's Table II and repeats the paper's unconstrained fits (L >= 6) to get E, M, rho_s, chi and c in the infinite system. Read report.md: n_reproduced of n_claims, with z-scores. | 2s |
