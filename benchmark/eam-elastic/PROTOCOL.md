# Reproduce the room-temperature elastic constants C11, C12, C44 of Cu, Al and Ni predicted by the EAM interatomic potentials of Tables 1-3 of Rassoulinejad-Mousavi, Mao and Zhang, J. Appl. Phys. 119, 245102 (2016), arXiv:1605.09237, by LAMMPS molecular dynamics at 300 K

Exported from run `eam-elastic-20261004-235527-038f`: the steps behind `analysis`.

## Reproduce

```bash
flower run protocol.yaml -y --inputs my-inputs.json
flower compare RUN expected.json      # RUN: the id the first command prints
```

## Inputs

- `host`: string
- `ssh_options`: array

## Needs

- machines: remote (see `clusters:` in protocol.yaml)
- software: envs/analysis, envs/lammps (frozen recipes, installed by the run)

## Steps

| step | what it establishes | took |
|---|---|---|
| `paper` | Downloads the paper (arXiv PDF, sha256) and parses Tables 1-3: C11, C12, C44 at 300 K per potential file and each table's experimental reference. Read rows (17 Cu, 24 Al, 19 Ni when intact) and complete (rows with all six numbers). | 1s |
| `potentials` | Fetches the potential files named in Tables 1-3 by exact file name, from the LAMMPS repository (release stable_2Aug2023_update3) or else the NIST repository's Cu, Al and Ni pages, with sha256. Read n_found and missing: only found files can be tested; missing ones are listed, not guessed. | 20s |
| `elastic` | One metal with one potential, 16384 atoms at 300 K: the linear elastic constants by symmetric +-1% strains (C11, C12, C44 with block errors), and the paper's protocol (constant-rate tension and shear, slopes C*_rate_0.5 / _1 / _2 fitted on 0-0.5%, 1%, 2% strain). Compare both with the paper's row for this file. | 51 items, 19h14m in total |
| `analysis` | This run's elastic constants against Tables 1-3: a row is reproduced when C11, C12 and C44 are all within 5% of this run's values from the paper's own protocol; also how far that protocol sits from the linear constants, the order of the potentials, and the rows that are not reproduced. Read n_reproduced of n_rows and report.md. | 6s |
