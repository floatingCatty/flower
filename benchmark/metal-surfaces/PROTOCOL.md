# Reproduce the surface energies, work functions and layer relaxations of the (111), (100) and (110) surfaces of Al, Pd, Pt and Au with PBE slabs in Quantum ESPRESSO (Singh-Miller and Marzari, PRB 80, 235407 (2009), arXiv:0801.1077), with a convergence study that a person signs off before the production slabs

Exported from run `metal-surfaces-20261006-184156-1093`: the steps behind `analysis`.

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
- software: envs/analysis, envs/ase, envs/qe (frozen recipes, installed by the run; `environments:` in protocol.yaml pins their versions)

## Steps

| step | what it establishes | took |
|---|---|---|
| `sources` | Downloads the paper and the pseudopotentials it names (Ref. 83), with sha256; transcribes Tables I-IV (PBE, 13-layer slabs) and checks every number against the PDF text and each pseudopotential against the valence configuration the paper states. n_faces should be 12. | 11s |
| `conv-in` | pw.x inputs for the convergence study, at the paper's lattice constants: bulk equations of state at k = 12-24 (and at 48 Ry), and 5-, 6- and 7-layer slabs of Al(111), Pd(100) and Pt(110) at in-plane k = 8-24 and 12, 16, 20 A of vacuum. items: one case per row of the study. | 11s |
| `conv` | One row of the convergence study with pw.x: total energies, Fermi energies and the vacuum potential (pp.x, average.x) of each input. The conv-report step turns the rows into converged settings. | 41 items, 2h12m in total |
| `conv-report` | Turns the convergence rows into settings: a0 and B per metal at each bulk k-mesh, the 7-layer surface energy and work function against the slab k-mesh and the vacuum, and the smallest settings within the criteria. report.md has the tables; proposal is what the settings gate shows. A reviewer's rejection text (e.g. "kslab=20") overrides the choice. | 4s |
| `settings` | A person reads the convergence study and approves the settings for the production slabs (about a day of the workstation), or rejects with overrides such as "kslab=20 vacuum=20", which redoes the report with them and asks again. | 4m15s |
| `slab-in` | pw.x inputs for the production slabs at the signed-off settings and this run's lattice constants: per face a series of unrelaxed slabs (4-13 layers, for the Fiorentini-Methfessel bulk energy and the work function) and a relaxed 13-layer slab; Pd(100) also relaxed at 5-15 layers (the paper's Fig. 1). items: one case each, most expensive first. | 6s |
| `slab` | One production case with pw.x: energies, Fermi energies and vacuum potentials of a series of unrelaxed slabs, or one relaxation (all atoms free, BFGS to 1e-4 Ry/bohr); the outputs and plane-averaged potentials are fetched back to out/. | 29 items, 2d05h in total |
| `analysis` | Surface energies (Fiorentini-Methfessel, bulk energy from the 6-13-layer slope), work functions and layer relaxations of the 13-layer slabs against Tables II-IV, a0 and B against Table I, and the text's convergence statements on Pd. Read n_reproduced of n_claims, by_table, and report.md. | 14s |
