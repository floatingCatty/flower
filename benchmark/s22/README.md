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

## Result

See the run's `report` step (`s22_report.md`, `s22.png`); the table is copied here when the run completes.
