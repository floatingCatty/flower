# Benchmark: surface energies, work functions and relaxations of fcc metal surfaces

**Paper:** N. E. Singh-Miller and N. Marzari, "Surface energies, work functions, and surface relaxations of low
index metallic surfaces from first principles", Phys. Rev. B 80, 235407 (2009),
[arXiv:0801.1077](https://arxiv.org/abs/0801.1077). PBE slabs in Quantum ESPRESSO for the (111), (100) and (110)
surfaces of Al, Pd, Pt and Au (Ti(0001) is left out here): Table I (a0, B), Table II (layer relaxations of
13-layer slabs), Table III (surface energies, Fiorentini–Methfessel), Table IV (work functions), and the text's
statements on convergence with slab thickness.

**Method here:** the paper's own pseudopotentials (its Ref. 83, from the QE library; Pt and Au are now named
`*.pbe-nd-rrkjus.UPF`, with the valence configurations the paper states but a nonlinear core correction the old
names did not advertise), its cutoff (32/512 Ry), Marzari–Vanderbilt smearing 0.02 Ry and 16 Å of vacuum.
[`surf.py`](surf.py) writes the inputs (ASE), [`case.sh`](case.sh) runs pw.x, pp.x and average.x on a remote
workstation, [`conv.py`](conv.py) turns the convergence study into proposed settings, [`analysis.py`](analysis.py)
compares with the tables. Surface energies: the bulk energy is the slope of the unrelaxed E(N) over N = 6–13;
work functions: plane-averaged electrostatic potential in the middle of the vacuum minus the Fermi energy.

## The gate: a person signs off the production settings

The study is the first benchmark built around a `gate` step. `conv` (41 rows: bulk equations of state at k =
12–24 and 48 Ry, 5–7-layer slabs of Al(111), Pd(100), Pt(110) at in-plane k = 8–24 and 12/16/20 Å of vacuum)
feeds `conv-report`, whose proposal is the gate's message; production (7 hours of the workstation) starts only
after the answer. What the reviewer saw and decided:

- the cutoff and the vacuum were converged (12 Å as good as 16 Å to 1e-4 eV/atom); the reviewer chose the paper's
  16 Å anyway, by rejecting with `vacuum=16`: the report was redone with the override and the gate asked again;
- the slab k-mesh met the criterion only at the largest mesh computed (24 × 24; Al(111) still 0.007 eV/atom off
  at 20 × 20). The first proposal did not say so; `conv.py` now flags a choice at the edge of the study;
- Pt and Au lattice constants came out larger than the paper's (Au by 0.019 Å), which the gate message showed
  before any production work.

## Result (run `metal-surfaces-20261006-184156-1093`): 75 of 97 claims

| | reproduced | |
|---|---|---|
| Table I: a0, B | 7 of 8 | Au a0 4.179 vs 4.16 Å (the pseudopotential, see above); B within 5 % throughout |
| Table III: surface energies (unrelaxed, relaxed; J/m²) | 26 of 36 | Pd, Pt(111)/(100), Au(100)/(110) within 0.015 eV/atom |
| Table IV: work functions | **11 of 12** | within 0.03 eV except Au(110) (4.986 vs 5.04) |
| Table II: relaxations d12, d23, d34 | 27 of 36 | all of Pd(111)/(110), Pt and Au(100)/(110) within 0.5 % |
| convergence statements | 4 of 5 | Pd(110) σ(N ≥ 6) within 0.007 of N = 13, not 0.005 |

What the misses show:
- **Al oscillates with slab thickness.** σ(N) of Al(110) runs 0.71–0.76 eV/atom over N = 8–13 and Al(111)
  0.29–0.32 (quantum size effects), so a 13-layer value carries about ±0.02; the Al(110) misses (0.740 vs 0.70) are
  of that size. The Al(111) relaxations (+1.31, +0.44, +0.92 %) disagree with the paper's own (+1.04, −0.54, +0.19)
  but agree with the all-electron FLAPW values the paper quotes in the same table (+1.35, +0.54, +1.06).
- **Au(111):** this run's relaxations (+0.98, −0.35, −0.09 %) are close to the LDA values the paper lists
  beside its own (+0.8, −0.3), not to its −0.04, −1.86, −1.40; σ is 0.331 vs 0.35 eV/atom, with a0 0.019 Å
  larger (the pseudopotential file).
- Pt(110) and Pd(110) surface energies are 0.016–0.027 eV/atom above the paper (the J/m² values pass).

## How it ran (one flower run, started with `flower start`)

`sources` (the arXiv PDF with sha256; Tables I–IV transcribed and every number checked against the text layer;
the four pseudopotentials with sha256 and their valence configurations checked) → `conv-in` → `conv` (41 cases,
4 cores each, within the workstation's 32-core budget) → `conv-report` → **`settings` (gate)** → `slab-in` →
`slab` (29 cases: per face a 4–13-layer unrelaxed series and a relaxed 13-layer slab, plus Pd(100) relaxed at
5–15 layers; 7 h 20 min) → `analysis`.

What the study found in flower:
- **BUGS #63:** `${step.dir}` of a step with an `environment:` (run as a job on the implicit `local` cluster)
  pointed at the attempt's unused `work/` directory, so the next step did not find the pseudopotentials.
- **BUGS #64:** `flower reject --text "vacuum=16" --note "…"` kept only the note, so the reworked report read no
  override and the gate asked the same question again.
- `flower add` could not create a gate (the gate was written into plan.yaml by hand); `flower add --gate` now does.
- The agent ran a quick local test of pw.x and pp.x beside the run, against the project's own rule: writing a
  step cost more than running the command. Steps are cheaper now (printed outputs, `kind` optional).
- Fan-out items showed as `slab[0]` … `slab[28]` in `flower status`; their names show now.

## Reproduce

[`protocol.yaml`](protocol.yaml) (the recipe versions pinned under `environments:`), [`expected.json`](expected.json),
[`PROTOCOL.md`](PROTOCOL.md): `flower run metal-surfaces/protocol.yaml --follow --inputs my-machines.json`; the run
stops at the `settings` gate for your sign-off (`flower approve RUN settings#a1`); then
`flower compare RUN metal-surfaces/expected.json`.

## Files
- [`sources.py`](sources.py): the paper, Tables I–IV checked against its text, the pseudopotentials.
- [`surf.py`](surf.py): pw.x inputs for the convergence study and the production slabs.
- [`case.sh`](case.sh): one case on the remote (pw.x, pp.x, average.x), checkpointed per input.
- [`conv.py`](conv.py): the convergence report and the gate's proposal (overrides from a rejection's note).
- [`analysis.py`](analysis.py): the claims, `report.md`, `surfaces.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits (the gate).
