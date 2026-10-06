# Benchmark: the A24 noncovalent interaction energies at CCSD(T)/CBS

**Paper:** J. Řezáč and P. Hobza, "Describing noncovalent interactions beyond the common approximations: how
accurate is the 'gold standard', CCSD(T) at the complete basis set limit?", J. Chem. Theory Comput. 9, 2151 (2013),
[doi:10.1021/ct400057w](https://doi.org/10.1021/ct400057w). 24 small dimers (hydrogen-bonded, mixed, dispersion-bound)
with benchmark interaction energies; the paper adds corrections beyond CCSD(T)/CBS (higher excitations, core
correlation, relativity) of up to about 0.1 kcal/mol. The paper is not open; the geometries and the reference values
come from Psi4's A24 database file (tag v1.9.1, sha256 recorded by the run), as for the S22 benchmark.

**Method here:** PySCF (the frozen `envs/pyscf` recipe) on a remote workstation ([`a24.py`](a24.py)),
counterpoise corrected, frozen core: CCSD(T)/CBS = HF/aug-cc-pVQZ + MP2 correlation extrapolated aug-cc-pVTZ→QZ
(X⁻³, density fitting) + [CCSD(T) − MP2]/aug-cc-pVTZ (no density fitting). 72 calculations (24 dimers × 3 levels,
each the dimer and both monomers in the dimer basis). [`analysis.py`](analysis.py) adds the parts up.

## Result (run `a24-20261005-072811-bb36`): 2 of 2 claims

| claim | | this run | |
|---|---|---|---|
| every dimer within 0.1 kcal/mol of the reference | 24 | **24/24** | ✓ |
| mean absolute deviation | < 0.05 | **0.009 kcal/mol** (mean signed −0.005) | ✓ |

The run's `report.md` lists every dimer with its parts. Twenty dimers agree within 0.010 kcal/mol, three
ethene/ethyne dimers within 0.014, and the formaldehyde dimer (9) is the one outlier at −0.100 (its CCSD(T) − MP2 part is
small, −0.003, so the difference sits in the MP2/CBS part). The agreement is closer than the size of the corrections
the paper adds beyond CCSD(T)/CBS, which suggests the database values are close to the paper's CCSD(T)/CBS level
itself; the paper's text was not available to check which of its columns Psi4 carries.

The CCSD(T) − MP2 correction matters: up to +0.43 kcal/mol (the ethyne dimer, D2h), larger than the whole interaction
energy of several dimers. The CCSD(T) calculations took 34 h of compute (up to 3.4 h for one dimer on 8 cores); the
MP2 parts, with density fitting, 20 minutes in all.

## How it ran (one flower run, started with `flower start`)

`refs` (the database file, its sha256, 24 dimers split into fragments) → `smoke` (timing; cancelled once the
campaign was under way) → `calc` (72 items, 8 cores each, 4 at a time within the remote's 32-core budget) → `analysis`.

What the study found:
- **A geometry parser that dropped atoms.** The database writes argon as `AR`; the parser matched element symbols
  as `Ar` only, so the argon dimers (20, 21) ran as single molecules and failed. The parser now reads both and
  refuses a dimer with an empty fragment (`diag-ar` found it).
- **BUGS #62 in flower:** adding an output to the finished data step and running `flower rerun RUN refs --only`
  superseded its whole downstream (72 calculations queued again, including finished CCSD(T) results) despite
  `--only`. Fixed: `--only` now limits a superseded step to its own items. The results could not have been kept anyway,
  since the data file staged into every calculation had changed.
- `flower add --tmpdir job` (new): the third study whose PySCF scratch needed to stay off a small `/tmp`.
- A quick diagnostic step waited behind the campaign's `cpus` budget; a second cluster entry for the same host without
  a budget (`remote-quick`) ran it at once, as the reference now describes.

## Reproduce

[`protocol.yaml`](protocol.yaml), [`expected.json`](expected.json), [`PROTOCOL.md`](PROTOCOL.md):
`flower run a24/protocol.yaml --follow --inputs my-machines.json`, then `flower compare RUN a24/expected.json`.

## Files
- [`a24.py`](a24.py): the database parser and one dimer at one level.
- [`analysis.py`](analysis.py): CCSD(T)/CBS from the parts, `report.md`, `a24.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits.
