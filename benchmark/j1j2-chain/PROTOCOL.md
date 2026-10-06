# Reproduce the exact-diagonalization results for the frustrated spin-1/2 J1-J2 Heisenberg chain: spin correlations at r=L/2 for L<=32 (Table I) and the critical coupling J2c/J = 0.241167(5) where the marginal operator vanishes (Eggert, PRB 54, R9612 (1996), arXiv:cond-mat/9602026)

Exported from run `j1j2-chain-20261004-000321-0850`: the steps behind `analysis`.

## Reproduce

```bash
flower run protocol.yaml --follow --inputs my-inputs.json
flower compare RUN expected.json      # RUN: the id the first command prints
```

## Inputs

- `host`: ssh alias of the remote machine
- `ssh_options`: extra ssh/rsync options, e.g. ["-F", "/path/ssh_config"]

## Needs

- machines: remote (see `clusters:` in protocol.yaml)
- software: envs/analysis, envs/quspin (frozen recipes, installed by the run)

## Steps

| step | what it establishes | took |
|---|---|---|
| `paper` | Downloads the paper and parses its Table I (<Sz(0)Sz(L/2)> for L = 8..32 at five J2) from the text layer; table1_rows should be 13. | 1s |
| `fitscan` | Tries 144 fitting choices the paper does not state on the paper's own Table I, and keeps the one that best reproduces its Table II (best_err_* about 1 %); the analysis uses it. | 8s |
| `sectors` | Exact diagonalization of every symmetry block at one small L (8..20): which block holds the ground state, the lowest excited singlet and the lowest triplet (identified by <S^2>). | 7 items, 3m28s in total |
| `rule` | Checks that those blocks follow one rule for L = 0 and one for L = 2 (mod 4) at every scanned size, and hands the rule to the big diagonalizations. Fails if any size breaks it. | 13s |
| `ed` | Lanczos at one L: ground-state energy and <Sz(0)Sz(L/2)> at nine J2, and the singlet and triplet levels on a J2 grid (result.json). E0_per_site_J2_0 approaches the Bethe-ansatz -0.4431 from below. | 13 items, 1h06m in total |
| `analysis` | Compares with the paper: Table I to the printed digits, Table II and Figs. 3-4 (with the fit choices from fitscan), and J2crit where the splitting falls as 1/L^3. Read report.md: n_reproduced of n_claims. | 4s |
