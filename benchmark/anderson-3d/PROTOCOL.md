# Reproduce the critical disorder W_c = 16.54(2) and the localization-length exponent nu = 1.57(2) of the 3D Anderson transition (box distribution), from transfer-matrix localization lengths of L x L bars and finite-size scaling with corrections (Tables II and III of Slevin and Ohtsuki, PRL 82, 382 (1999), arXiv:cond-mat/9812065)

Exported from run `anderson-3d-20261005-021220-0e6e`: the steps behind `fss`.

## Reproduce

```bash
flower run protocol.yaml --follow
flower compare RUN expected.json      # RUN: the id the first command prints
```

## Needs

- machines: here (see `clusters:` in protocol.yaml)
- software: envs/analysis, envs/numba (frozen recipes, installed by the run)

## Steps

| step | what it establishes | took |
|---|---|---|
| `paper` | Downloads the paper (arXiv PDF, sha256) and parses Tables II and III: W_c, Lambda_c and nu with their 95% intervals, for the box, Gaussian and Lorentz distributions. Read box_Wc and box_nu: the claims. | 1s |
| `check` | The numba transfer-matrix kernel against a dense numpy calculation on the same potentials (L = 2 and 3), and the symplectic pairing (s, 1/s) of the transfer matrix's singular values. Read L2_dev, L3_dev (below 1e-9) and L*_pair (below 1e-6). | 23s |
| `fss-selftest` | The scaling fit on synthetic data drawn from its own model (W_c 16.54, nu 1.57, y -2.8, 0.1% noise, the paper's sizes and W grid): it must recover W_c and nu inside its 95% intervals before it is trusted with the campaign. | 3s |
| `tm` | The reduced localization length Lambda = 1/(gamma L) of an L x L bar at disorder W (box distribution, E = 0) to 0.1 % (the paper's accuracy), the data of the finite-size-scaling fit. Read Lambda and rel_err. | 91 items, 3d12h in total |
| `fss` | The finite-size-scaling fit of all 91 points: with corrections to scaling (the paper's box fit, 12 parameters) and without them on L >= 8 near W_c, W_c, nu and Lambda_c with 95% intervals from 200 resampled fits, against Tables II and III. Read n_reproduced of n_claims and report.md. | 8s |
