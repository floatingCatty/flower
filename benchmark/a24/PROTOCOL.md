# Reproduce the A24 benchmark interaction energies of 24 small noncovalent dimers (Rezac and Hobza, JCTC 9, 2151 (2013)) at the CCSD(T)/CBS level: MP2/CBS from aug-cc-pVTZ/QZ plus a CCSD(T) correction in aug-cc-pVTZ, counterpoise corrected

Exported from run `a24-20261005-072811-bb36`: the steps behind `analysis`.

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
- software: envs/analysis, envs/pyscf (frozen recipes, installed by the run)

## Steps

| step | what it establishes | took |
|---|---|---|
| `refs` | The A24 database file of Psi4 (tag v1.9.1, sha256): the 24 dimer geometries split into fragments, and the reference interaction energies (kcal/mol) of Rezac and Hobza. Read n (24 when intact) and names. | 1s |
| `calc` | One dimer at one level, counterpoise corrected, frozen core: HF and MP2 correlation interaction energies in aug-cc-pVTZ or QZ (density fitting), or CCSD(T)-MP2 in aug-cc-pVTZ. The parts of CCSD(T)/CBS that the analysis adds up. | 72 items, 1d10h in total |
| `analysis` | CCSD(T)/CBS per dimer from this run's parts (HF/aQZ + MP2 correlation extrapolated aTZ-aQZ + [CCSD(T)-MP2]/aTZ) against the A24 references: every dimer within 0.1 kcal/mol, and the mean absolute deviation below 0.05. Read n_within of n_complete, mad, and report.md. | 2s |
