# Benchmark: the S22 noncovalent interaction energies (quantum chemistry at scale)

**Paper:** P. Jurečka, J. Šponer, J. Černý and P. Hobza, "Benchmark database of accurate (MP2 and CCSD(T)
complete basis set limit) interaction energies of small model complexes, DNA base pairs, and amino acid pairs",
Phys. Chem. Chem. Phys. 8, 1985 (2006), [doi:10.1039/B600027D](https://doi.org/10.1039/B600027D). 22 hydrogen-bonded,
dispersion-bound and mixed complexes with CCSD(T)/CBS reference interaction energies.

**Method here:** PySCF 2.14 (the frozen `envs/pyscf` recipe, installed on this machine by the workflow).
Geometries and the paper's reference values from the psi4 database file (tag v1.9.1, sha256 recorded).
Counterpoise-corrected, frozen-core: E(CCSD(T)/CBS) = E_HF(aug-cc-pVTZ) + E_corr,MP2(CBS, aDZ→aTZ, X⁻³) +
[CCSD(T) − MP2](cc-pVDZ), the same composite structure as the paper (which extrapolated aTZ→aQZ for most
complexes and used varying small bases for the CCSD(T) correction). Compared with the paper (BIND_S220) and the
revised S22B values (Marshall, Burns & Sherrill 2011).

**Scale:** 66 calculations (22 complexes × 3 levels, each three fragments), on a shared 96-core machine.

## What it tested in flower

* A 66-item foreach with `--retry`, `--on-failure continue` and a report with `--trigger all_done`, so partial
  results are reported while failures are investigated.
* Real failure modes of quantum chemistry on a shared machine: PySCF's multi-GB integral files filled `/tmp`
  (the job now keeps them in its own directory, `tmpdir: job`), and MP2/aug-cc-pVTZ for the largest complexes
  ran out of memory at 14 GB (re-run as `calc-big` on a one-at-a-time cluster with 60 GB).
* Changing a setting that does not affect results (`tmpdir`) mid-campaign, while six jobs ran, without
  re-running the 30 finished calculations: `tmpdir` is outside the cache key and `rerun --cached` applies an
  edit without interrupting running work.
* `flower status` collapses a 66-item step into one line (`--items` lists them all).

## Result (run `s22-20261003-191332-a8fe`): 20 of 22 within tolerance, MAE 0.23 kcal/mol

Interaction energies in kcal/mol. A complex counts as reproduced when |error vs 2006| ≤ max(0.3, 5 % of the
reference).

| # | atoms | paper 2006 | S22B | this run CCSD(T)/CBS | MP2/CBS | dCCSD(T) | error vs 2006 | verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | 8 | -3.17 | -3.13 | -2.97 | -3.12 | 0.14 | 0.20 | **reproduced** |
| 2 | 6 | -5.02 | -4.99 | -4.60 | -4.84 | 0.24 | 0.42 | **differs** |
| 3 | 10 | -18.61 | -18.75 | -17.85 | -18.07 | 0.23 | 0.76 | **reproduced** |
| 4 | 12 | -15.96 | -16.06 | -15.42 | -15.47 | 0.05 | 0.54 | **reproduced** |
| 5 | 24 | -20.65 | -20.64 | -20.01 | -20.04 | 0.03 | 0.64 | **reproduced** |
| 6 | 25 | -16.71 | -16.93 | -16.32 | -17.05 | 0.74 | 0.39 | **reproduced** |
| 7 | 30 | -16.37 | -16.66 | -16.06 | -16.22 | 0.17 | 0.31 | **reproduced** |
| 8 | 10 | -0.53 | -0.53 | -0.49 | -0.49 | 0.00 | 0.04 | **reproduced** |
| 9 | 12 | -1.51 | -1.47 | -1.44 | -1.58 | 0.15 | 0.07 | **reproduced** |
| 10 | 17 | -1.50 | -1.45 | -1.47 | -1.80 | 0.33 | 0.03 | **reproduced** |
| 11 | 24 | -2.73 | -2.65 | -3.08 | -4.90 | 1.81 | -0.35 | **differs** |
| 12 | 20 | -4.42 | -4.25 | -4.58 | -6.79 | 2.22 | -0.16 | **reproduced** |
| 13 | 24 | -10.12 | -9.80 | -9.82 | -10.97 | 1.16 | 0.30 | **reproduced** |
| 14 | 28 | -5.22 | -4.52 | -5.09 | -7.99 | 2.90 | 0.13 | **reproduced** |
| 15 | 30 | -12.23 | -11.73 | -11.98 | -14.68 | 2.70 | 0.25 | **reproduced** |
| 16 | 10 | -1.53 | -1.50 | -1.45 | -1.66 | 0.21 | 0.08 | **reproduced** |
| 17 | 15 | -3.28 | -3.27 | -3.19 | -3.53 | 0.34 | 0.09 | **reproduced** |
| 18 | 16 | -2.35 | -2.31 | -2.30 | -2.65 | 0.35 | 0.05 | **reproduced** |
| 19 | 15 | -4.46 | -4.54 | -4.45 | -5.14 | 0.68 | 0.01 | **reproduced** |
| 20 | 24 | -2.74 | -2.72 | -2.80 | -3.61 | 0.81 | -0.06 | **reproduced** |
| 21 | 28 | -5.73 | -5.63 | -5.66 | -6.96 | 1.30 | 0.07 | **reproduced** |
| 22 | 26 | -7.05 | -7.10 | -6.88 | -7.61 | 0.73 | 0.17 | **reproduced** |

| group | n | MAE vs 2006 | max vs 2006 | MAE vs S22B |
|---|---|---|---|---|
| hydrogen-bonded | 7 | 0.47 | 0.76 | 0.57 |
| dispersion | 8 | 0.17 | 0.35 | 0.21 |
| mixed | 7 | 0.08 | 0.17 | 0.08 |
| all | 22 | 0.23 | 0.76 | 0.28 |

- **Dispersion-bound and mixed complexes agree closely** (MAE 0.17 and 0.08 kcal/mol). Here the ΔCCSD(T)
  correction is large (up to 2.9 kcal/mol for #14), so this is the part of the table that tests the CCSD(T)
  calculations themselves.
- **Hydrogen-bonded complexes are systematically less bound** (all seven errors positive, MAE 0.47). This is the
  expected sign of the smaller basis sets used here: the MP2 correlation is extrapolated from aug-cc-pVDZ→aTZ
  where the paper used aTZ→aQZ, and hydrogen bonds are the most basis-hungry. The two complexes outside the
  tolerance are #2 (the water dimer, 0.42 kcal/mol) and #11 (the stacked benzene dimer, −0.35 kcal/mol, the
  one complex that comes out *more* bound; for π-stacking the ΔCCSD(T) correction is basis-sensitive, and
  cc-pVDZ is the smallest basis used for it here).
- Against the revised S22B values the MAE is 0.28 kcal/mol, close to the agreement with the original paper:
  this run's basis-set error is larger than the difference between the two references.

**How it finished.** The local campaign ran the 66 calculations with `--on-failure continue`; 16 local attempts
failed: 8 with `MemoryError` and 8 at their time limit (MP2/aug-cc-pVTZ and CCSD(T) for the largest complexes,
on a shared 96-core machine). They were
re-run on a remote workstation as an appended foreach (`calc-remote`, 10 items, from 7 minutes to 6.2 hours, the longest
being the CCSD(T) corrections of the largest dimers) and through the one-at-a-time `calc-big` cluster. The `report` step,
with `trigger: all_done`, then combined whichever level finished where; all 22 complexes have all three levels.
