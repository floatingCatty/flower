# Benchmark: the Si valence-band top with seven methods

At Γ, the top of the Si valence band is Γ8 (heavy and light holes, degenerate) and Γ7 (the split-off
band), which lies Δso below. This benchmark computes, with spin-orbit coupling in every method and at
the experimental a0 = 5.431 Å:
- **the split-off gap Δso**;
- **HH, LH and SO effective masses** along [100] and [111]: from the band curvature at
  |k| = 0.005, 0.01 and 0.02 (2π/a0), fitted as E = E0 − ħ²k²/2m + ck⁴. They give the **Luttinger
  parameters** γ1, γ2 and γ3;
- **the HH–LH splitting at Γ under 1 % biaxial (001) tension**, with ε∥ = 0.01 and
  ε⊥ = −2C12/C11 ε∥.

All calculations run on a remote workstation over ssh (`scheduler: none`), each code in its own frozen
environment recipe in [`../envs/`](../envs/). The comparison with experiment runs locally.

| method | step | environment | what it is |
|---|---|---|---|
| Quantum ESPRESSO 7.5 | `qe` | `qe` | PBE, plane waves (60 Ry), PseudoDojo fully-relativistic pseudopotential `Si_dojo_soc.upf` |
| ABACUS 3.9 | `abacus` | `abacus` | the same pseudopotential, plane-wave basis, `nspin 4` + `lspinorb` |
| ABACUS 3.9 (LCAO) | `abacus-lcao` | `abacus` | the same pseudopotential with its 6 bohr DZP orbital basis `Si_dojo_6au.orb` (added later; see *Developed inside the run* below) |
| PySCF 2.14 | `pyscf` | `pyscf` | PBE, all-electron cc-pVDZ, two-component GKS with X2C1E spin-orbit, 4³ k-mesh. Bands at arbitrary k are built by hand ([`run_pyscf.py`](run_pyscf.py)) and checked against the SCF at Γ |
| DFTB+ 25.1 | `dftb` | `dftbplus` | SCC-DFTB with the pbc-0-3 Slater–Koster set, plus on-site spin-orbit ξp = 21.0 meV taken **from the `atom-soc` step**, an all-electron fully-relativistic Si atom computed with QE `ld1.x` |
| tight binding sp3d5s* | `models[0]` | `pyscf` | Boykin, Klimeck & Oyafuso, PRB 69, 115201 (2004), Si parameters. It reproduces that paper's gap and masses exactly. Strain uses (d0/d)² scaling only |
| empirical pseudopotential | `models[1]` | `pyscf` | Cohen–Bergstresser 1966 form factors, spline-interpolated for strain. Spin-orbit μ is **fitted** to Δso = 44.1 meV |
| k·p 6-band | `models[2]` | `pyscf` | Luttinger–Kohn + Bir–Pikus with experimental γ1..3, Δso, b and d. This is the reference for the strain splitting |

## Run

```bash
cd benchmark
../.venv/bin/flower run si-valence/plan.yaml --inputs remote-inputs.json   # {"host": "<ssh alias>", ...}
```

Before the first calculation on each code, the plan's generated steps `env-qe-remote`,
`env-abacus-remote`, `env-pyscf-remote` and `env-dftbplus-remote` check the environment on the remote,
and install it from its frozen recipe if it is missing.

## Result (run `si-valence-20261003-161718-510f`, 2026-10-03, succeeded in 8 min 08 s)

On a 32-core workstation, steps ran in parallel (`max_jobs: 4`):
- **Environment steps:** 5–12 s each. qe, pyscf and dftbplus were installed by the run, abacus was
  already present.
- **QE:** 24 s.
- **ABACUS and PySCF:** about 8 min each.
- **The models:** 4 s each.

| method | Δso (meV) | m_hh[100] | m_lh[100] | m_so | m_hh[111] | m_lh[111] | γ1 | γ2 | γ3 | HH–LH @1% (meV) | E1–E3 @1% (meV) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| QE (PBE, PW) | 47.8 | 0.260 | 0.189 | 0.225 | 0.660 | 0.131 | 4.57 | 0.36 | 1.53 | 109 | 145 |
| ABACUS (PBE, PW) | 47.8 | 0.260 | 0.189 | 0.225 | 0.656 | 0.131 | 4.57 | 0.36 | 1.53 | 109 | 145 |
| ABACUS (PBE, LCAO 6au) ⚠ | 46.2 | −11.0 | 0.106 | 0.223 | 11.1 | 0.108 | 4.69 | 2.39 | 2.30 | 195 | 228 |
| PySCF (PBE, all-e X2C) | 49.3 | 0.246 | 0.179 | 0.213 | 0.617 | 0.124 | 4.83 | 0.39 | 1.61 | 116 | 152 |
| DFTB+ (pbc-0-3) | 31.5 | 0.319 | 0.132 | 0.192 | 0.682 | 0.108 | 5.34 | 1.10 | 1.94 | 108 | 131 |
| Tight binding sp3d5s* | 47.2 | 0.276 | 0.214 | 0.246 | 0.734 | 0.144 | 4.15 | 0.26 | 1.40 | 180 | 214 |
| Empirical pseudopot. | 44.1 | 0.279 | 0.185 | 0.228 | 0.702 | 0.132 | 4.49 | 0.45 | 1.54 | 183 | 214 |
| k·p 6-band (exp. par.) | 44.1 | 0.277 | 0.201 | 0.233 | 0.718 | 0.139 | 4.29 | 0.34 | 1.45 | 100 | 133 |
| **experiment** | 44.1 | 0.277 | 0.201 | 0.233 | 0.718 | 0.139 | 4.29 | 0.34 | 1.45 | — | — |

Notes on the experimental row:
- Δso = 44.1 meV, and γ1..3 = 4.285 / 0.339 / 1.446 (cyclotron resonance).
- The experimental masses follow from those γ's: m_hh[100] = 1/(γ1 − 2γ2), and so on.
- There is no direct measurement of the 1 % splitting, so the k·p row, which uses the experimental
  b = −2.10 eV, serves as the reference.

### What the comparison shows

- **ABACUS LCAO with the 6 bohr DZP basis gets Δso right (46.2 meV) but the heavy hole wrong.** Its
  HH band is flat near Γ, even rising slightly, so the HH mass is meaningless and the strain splitting
  is inflated to 195 meV. Running the same LCAO calculation without spin-orbit gives the same flat HH
  band. So the cause is the basis, not ABACUS's spin-orbit implementation: two s, two p and one d
  orbital per atom, with a 6 bohr cutoff, cannot carry the remote-band couplings that set the HH
  curvature. Δso, an on-site atomic-like quantity, is fine. The comparison flags this row and leaves it
  out of the mass and strain charts.

- **QE and ABACUS agree to 0.01 meV in Δso,** and to < 1 % in every mass. With the same
  pseudopotential and plane waves, two independent codes give the same physics.
- **PBE overestimates Δso by about 9 %** (47.8 meV vs 44.1 meV). It gives hole masses about 5–10 %
  too light, γ's correspondingly too large. All-electron PySCF with X2C spin-orbit lands in the same
  place (49.3 meV); the small residual difference comes from the basis (cc-pVDZ) and the treatment of
  the core.
- **DFTB+ gives Δso = 31.5 meV,** which is exactly (3/2)ξp, the atomic splitting: in a minimal sp
  basis the Γ8/Γ7 states are pure Si 3p, so the bulk splitting equals the atomic one. Its LH masses are
  far too light (0.13 vs 0.20), and γ2 is 3× too large. Its strain response (108 meV) is close to DFT.
- **Tight binding reproduces the masses by construction** (they are fitted). With simple (d0/d)²
  scaling, without the model's on-site strain terms, it overestimates the strain splitting by about
  80 %.
- **The empirical pseudopotential** gets masses within about 5 % from three 1966 form factors
  (its Δso is fitted). Its strain splitting (183 meV) is far off: interpolating the form factors in q
  is not a strain model.
- **Strain, about 1 % biaxial tension:** the three DFT codes and DFTB+ give 108–116 meV, against
  100 meV from k·p with the experimental deformation potential. PBE's shear deformation potential is
  about 10 % too large, the same direction as its Δso error.

Charts (in the run's `compare` step, and in the flower UI): `dso.png`, `masses.png`, `strain.png`;
the table is also written to `comparison.md`.

## Developed inside the run

The LCAO step was added after the run had finished, using only flower:
1. **Add the step.** I added it to `plan.yaml` and an LCAO mode to `si_valence.py`, then ran
   `flower rerun RUN abacus-lcao --follow`. That picked the new step up as a recorded amendment
   (generation 2), ran it on the remote machine, and streamed it back: 26 s, of which ABACUS took 23 s.
2. **Fix the parser.** The first result was nonsense. The parser accepted the SCF's `istate.info`,
   which has 512 k-points, because it only required "at least" the 7 requested ones. After the fix it
   reads `BANDS_1.dat` and requires exactly 7. Rerun: 28 s.
3. **Diagnose the physics.** `flower remote exec` (logged) reran the same LCAO calculation without
   spin-orbit, in the step's own directory. The heavy-hole band was still flat, so the basis is the
   cause.
4. **Add it to the comparison.** I edited the `compare` step's inputs and `compare.py`, then ran
   `flower rerun RUN compare --follow`: amendment generation 3, 2.6 s.

All of this, failures included, is in the run's log (`flower log si-valence-20261003-161718-510f`).

## Files
- [`plan.yaml`](plan.yaml): the workflow.
- [`si_valence.py`](si_valence.py): geometry, k-points, the QE / ABACUS / DFTB+ / `ld1.x` drivers, and
  the analysis (standard library only).
- [`run_pyscf.py`](run_pyscf.py), [`models.py`](models.py): PySCF; tight binding, EPM, k·p.
- [`compare.py`](compare.py): the table and charts, run locally.
- `Si_dojo_6au.orb`: the ABACUS numerical-orbital basis for it (6 bohr, 2s2p1d).
- `Si_dojo_soc.upf`: PseudoDojo v0.4 fully-relativistic Si (M. J. van Setten et al., Comput. Phys.
  Commun. 226, 39 (2018); CC BY 4.0).
