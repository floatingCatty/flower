# Reproduce the low-lying spectrum of the open S=1 Heisenberg chain by DMRG (Sorensen and Affleck, PRL 71, 1633 (1993), arXiv:cond-mat/9306036): the L=100 energies of Table I, the gap and spin-wave velocity from the finite-size fit, the free-fermion magnon states with 1-3 magnons

Exported from run `haldane-magnons-20261007-035056-06a4`: the steps behind `analysis`.

## Reproduce

```bash
flower run protocol.yaml --follow --inputs my-inputs.json
flower compare RUN expected.json      # RUN: the id the first command prints
```

## Inputs

- `host`: string
- `ssh_options`: array

## Needs

- machines: remote (see `clusters:` in protocol.yaml)
- software: envs/analysis, envs/tenpy (frozen recipes, installed by the run; `environments:` in protocol.yaml pins their versions)

## Steps

| step | what it establishes | took |
|---|---|---|
| `paper` | The paper (arXiv v1, sha256) and its numbers: Table I (L = 100 energies of the lowest M = 1..4 states) and the fit results, each checked against the PDF text. n_checked should be 12. | 1s |
| `check` | DMRG against exact diagonalization: the lowest energy of every S^z sector M = 0..4 of an open L = 10 chain. max_diff should be below 1e-9. | 13s |
| `chi` | Convergence in the bond dimension: the lowest M = 0..4 energies of the L = 100 chain at chi = ${item}. E should change by far less than the paper's last digit (1e-6) between the two largest chi. | 3 items, 21m19s in total |
| `levels` | The lowest M = 0..4 states of the open chain of length ${item} at chi = 256 (converged at L = 100, see chi): energies, <S^z_i>, bond energies and parity. The analysis fits the gaps E_M - E_1 against 1/(L-1)^2. | 24 items, 6h36m in total |
| `analysis` | The claims: Table I at L = 100, the gap and velocity from E_2 - E_1 against 1/(L-1)^2 (L <= 100), the 2- and 3-magnon fits (sum n^2 = 5 and 14 for free fermions), the magnon profiles of Figs. 1-3 and the parities. Read n_reproduced of n_claims and report.md. | 4s |
