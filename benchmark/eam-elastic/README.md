# Benchmark: elastic constants of Cu, Al and Ni from 51 EAM potentials (LAMMPS molecular dynamics)

**Paper:** S. M. Rassoulinejad-Mousavi, Y. Mao and Y. Zhang, "Evaluation of copper, aluminum, and nickel
interatomic potentials on predicting the elastic properties", J. Appl. Phys. 119, 245102 (2016),
[arXiv:1605.09237](https://arxiv.org/abs/1605.09237). Tables 1–3 give C11, C12 and C44 at 300 K for 60
embedded-atom potentials (17 Cu, 24 Al, 19 Ni) from the NIST and LAMMPS repositories, each against experiment.
The paper's protocol: about 1.4 million atoms, NPT at 300 K, then uniaxial tension at a constant strain rate of
10⁻³/ps under NVT (and shear at the same rate), with C from "the slope of the linear part" of the stress–strain
curve (fit range not stated, R² > 0.999).

**Method here:** LAMMPS 2023.08.02 (the frozen `envs/lammps` recipe, conda-forge) on a remote workstation
([`elastic.py`](elastic.py)), 16 384 atoms. After NPT at 300 K the box is fixed at the thermal lattice constant,
and each (metal, potential) is measured two ways:
- **the linear elastic constants**, from time-averaged stresses at ±1 % normal and shear strain (central
  differences, so third-order terms cancel; statistical errors ±0.1–0.3 GPa);
- **the paper's protocol**, constant-rate tension and shear, with slopes fitted on 0–0.5 %, 0–1 % and 0–2 %
  strain.

A paper row is reproduced when its C11, C12 and C44 are all within 5 % of this run's values from the paper's
protocol (0–1 %). The paper's table is parsed from its text layer ([`tables.py`](tables.py)); the potential files are
fetched by exact name from the LAMMPS repository or NIST ([`fetch.py`](fetch.py)): 45 of the 53 names exist, giving 51
(metal, potential) pairs and 48 complete paper rows to test.

## Result (run `eam-elastic-20261004-235527-038f`): 30 of 48 rows reproduced

| claim | this run | |
|---|---|---|
| rows with C11, C12, C44 all within 5 % of the paper's protocol run here | **30 / 48** | ✗ |
| the potentials ranked as in the paper, by C11 and C12 (Spearman ρ, per metal) | Cu 0.90 / 0.97, Al 0.98 / 0.98, Ni 0.95 / 0.82 | ✓ |
| ... by C44 | Cu 0.91, Al 0.45, Ni 0.67 | ✗ |

The `analysis` step's `report.md` has every row (paper / linear / paper's protocol); its `parity.png` plots them.

**The protocol, not the potentials, is the main finding.** One-sided tension measures C11 + ½C111·ε: the
third-order constants of these metals are large and negative, so the paper's C11 and C12 are systematically below
the linear elastic constants: by a median 2.8 % (C11) and 3.5 % (C12), and by up to 19–23 % for the softest
potentials. C44 is not affected (median 0.0 %), because shear has no such first-order term. The paper's own numbers
follow its protocol: for 30 rows all three constants agree with it within 5 %, many within 1 % (e.g. Cu-Zr.eam.fs:
paper 159.7 / 120.7 / 78.5 GPa, this run's protocol 159.5 / 121.3 / 79.4, linear 169.3 / 123.1 / 78.8). The paper's
ranking of the potentials against experiment therefore carries a protocol bias of a few percent, larger than the
differences between the best potentials it recommends.

**The 18 rows not reproduced:**
- **Entries that cannot be right in the table itself.** NiAlH_jea (both files, Al): C44 is printed equal to C11
  (58.26 and 58.98), a copied column; this run gives C44 = 10.6 GPa, and C11, C12 also differ by 30–60 %. AlCu.eam.alloy
  (Al): C44 51.1 against 27.4 by both methods (C11 and C12 agree within 5 %). Farkas_Nb-Ti-Al_1996 (Al): C44 91.8
  against 53 (C11, C12 within 5 %).
- **C44 of Ni, systematically lower in the paper** by 8–14 % for five potentials (Mishin-Ni-Al-Co-2013,
  Mishin-Ni-Co-2013, Ni99, FeCuNi, Fe-Ni), while C11, C12 of the same rows agree and C44 of Cu and Al agrees. The
  paper does not give enough of its shear procedure to locate this.
- **Within 5–9 % on one constant** (Cu_smf7, Cu.set, Al99, Al_zhou, CoAl, FeNiCr_Bonny_2013, NiAlH_jea for Ni), and
  Al.set (the softest Al potential, 7–16 %): consistent with a fit range that differs from the 0–1 % used here; for
  Cu.set the paper's C11 (138.5) sits between this run's linear value (136.6) and its 0–1 % slope (128.4).

## How it ran (one flower run, started with `flower start`)

`paper` (tables, 60 rows; 57 complete — the paper's own text has a row with no name, one without C11 and one with
only C11) → `potentials` (45 files, 15 from LAMMPS and 30 from NIST) → `smoke` (two Cu potentials, which showed the
protocol bias and sized the campaign) → `elastic` (51 items, 8 MPI ranks each, 4 at a time within the remote's
32-core budget, 19 h of compute in about 5 h) → `analysis`. `analysis-preview` read `${elastic.partial}` while the
campaign ran; the comparison against the paper's protocol rather than the linear values was chosen from the method
section and the smoke runs, before the campaign finished.

What the study found:
- **A potential file LAMMPS 2023 rejects.** NIST's FeNiCr.eam.alloy (Bonny 2011) has `INF` and `NAN` at r = 0 of its
  density and pair tables; LAMMPS stopped without a message unless run through `mpirun` (a diagnostic step,
  `diag-fenicr`, found it). `elastic.py` now writes a copy with those entries extrapolated from the next two (r = 0 is
  never sampled) and records how many it replaced.
- **flower:** BUGS #60 (an edit of the step being rerun waited for approval, and the waiting copy later parked the
  run) and #61 (`--stage-in` paths typed from the current directory), both fixed. A `cpus` budget also holds the
  quick steps on that cluster: documented, with the remedy (a second cluster entry without a budget).
- The first LAMMPS check script had the wrong expected energy (LJ units normalize `pe` per atom); it now compares
  with an exact lattice sum. OpenMPI's notices name the host, so the check keeps them out of its recorded output.

## Reproduce

[`protocol.yaml`](protocol.yaml), [`expected.json`](expected.json), [`PROTOCOL.md`](PROTOCOL.md):
`flower run eam-elastic/protocol.yaml -y --inputs my-machines.json`, then `flower compare RUN eam-elastic/expected.json`.

## Files
- [`tables.py`](tables.py): Tables 1–3 from the paper's text layer.
- [`fetch.py`](fetch.py): the potential files by exact name, LAMMPS repository then NIST, with sha256.
- [`elastic.py`](elastic.py): one (metal, potential): NPT, the linear constants, the paper's rate protocol.
- [`analysis.py`](analysis.py): the comparison, `report.md`, `parity.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits.
