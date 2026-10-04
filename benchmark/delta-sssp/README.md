# Benchmark: the Δ test of the SSSP efficiency pseudopotentials

**Papers:** K. Lejaeghere *et al.*, "Reproducibility in density functional theory calculations of solids",
Science 351, aad3000 (2016), the Δ test: the equations of state of 71 elemental crystals, compared with the
all-electron code WIEN2k. G. Prandini, A. Marrazzo, I. E. Castelli, N. Mounet and N. Marzari, "Precision and
efficiency in solid-state pseudopotential calculations", npj Comput. Mater. 4, 72 (2018),
[arXiv:1806.05609](https://arxiv.org/abs/1806.05609): the SSSP libraries. The claim tested: **SSSP efficiency
1.1 has an average Δ of 0.44 meV/atom** against WIEN2k.

**Method here:** Quantum ESPRESSO 7.5 (the frozen `envs/qe` recipe) on a remote workstation. SSSP 1.1 PBE
efficiency, each element at its *recommended* cutoffs. The paper used 200 Ry for every element, so this tests
the library as it is used. The structures are the Δ package's CIFs (pinned commit), reduced to primitive cells
except the antiferromagnets. Magnetism follows the Δ protocol: Fe, Co, Ni FM; Cr, Mn AFM; O₂ AFM between
molecules. The seven volumes are 94–106 % of the WIEN2k V₀. The k-points use a spacing of 0.1 Å⁻¹ (2π included),
with Marzari–Vanderbilt smearing of 0.01 Ry. The fits and Δ follow the package's `eosfit.py` and `calcDelta.py`,
ported to Python 3. The port is checked against the package's own script on its published data, every run.

## Result (run `delta-sssp-20261004-004529-cbce`)

All 71 elements. Every fit has its minimum inside 96–104 % of the WIEN2k volume, so no extra volumes were
needed.

| | average Δ (meV/atom) | elements |
|---|---|---|
| this run: SSSP 1.1 efficiency at its recommended cutoffs | **0.56** (median 0.35; 83 % of the elements below 1) | 71 |
| paper: SSSP efficiency 1.1 at 200 Ry | 0.44 | 71 + others, excluding rare-earth nitrides |
| published QE + SSSP efficiency entry (Castelli, Δ package history; an earlier SSSP) | 0.46 | 70 |

The Δ formula here agrees with the package's own `calcDelta.py` to 5·10⁻⁴ meV/atom on its published data, and
this check runs every time.

**Partly reproduced.** The library is as precise as claimed for most elements (median 0.35 meV/atom), but the
average comes out 0.12 meV/atom higher. Three follow-up studies in the same run show where the gap comes from:

| element | Δ at the recommended cutoff | at 1.5× the cutoff | smearing 0.002 Ry, k-spacing 0.07 Å⁻¹ | published QE + SG15 / SSSP entries |
|---|---|---|---|---|
| Au | 3.09 | 3.15 | 3.15 | 0.28 / 0.27 |
| Cr (AFM, 1.2 μB per atom in both runs) | 2.14 | 3.62 | | 21.1 / 3.62 |
| N | 1.97 | 1.97 | | 1.35 / 0.70 |
| O | 1.54 | 1.16 | | 0.38 / 1.99 |
| F | 1.43 | 1.43 | | 0.87 / 0.53 |
| Fe | 1.33 | 1.20 | | 4.67 / 0.56 |
| Hg | 1.07 | 1.07 | 0.88 | 0.08 / 0.55 |
| W | 0.94 | 0.57 | | / 0.39 |
| Pt, Cd, Cu | 2.24, 1.86, 1.19 | | 2.25, 1.69, 1.25 | / 2.05, 2.03, 0.98 |

* **Cutoff:** it matters for O, W, Fe and Ta, which move toward the published values at 1.5×, and Cr moves away.
  The paper's 200 Ry would remove this part of the gap. Au, N, F and Hg do not move at all.
* **Smearing and k-points:** at the paper's 0.002 Ry with a denser mesh, the metals change by at most
  0.2 meV/atom.
* **Au, the largest contributor:** Δ is 3.1 at any cutoff, smearing or k-mesh. SSSP 1.1's Au file is SG15 Au
  regenerated with ONCVPSP 3.3.1 (dated 2018-06-27), whereas the published SG15 calculation with the original
  file gives 0.28. The difference therefore follows the file, not this protocol. The SSSP authors' own value for
  that file could not be checked: their verification archive returns 403, and their website now serves a newer
  library generation. Without Au the average is 0.53.

Na is the slowest element: three atoms with a 20³ mesh, and 7 volumes took about 14 h on 4 cores. It reached its
8 h limit with 4 of 7 volumes done and was continued from its checkpoints (`flower rerun --keep-state`, below).

## How it ran (one flower run, started with `flower start`)

`sources` (the Δ package at a pinned commit, SSSP 1.1 efficiency and its cutoffs, the SSSP paper; sha256 of
each) → `env-ase-local` (a new frozen `ase` recipe: ASE + spglib) → `structures` (71 × 7 pw.x inputs, the
fan-out list sorted by estimated cost) → `env-qe-remote` → `eos-smoke` (Si, Fe) + `delta-smoke` → `eos`
(71 elements, 4 cores each, checkpointed per volume, `--retry 2 --on-failure continue`) → `delta`. A
`delta-preview` step (`${eos.partial}`) showed the results while the campaign ran. The cluster's cpu budget was
raised from 16 to 28 cores mid-run with `flower sync`, once the water benchmark had released the machine.

What this benchmark found in flower (tests/core/BUGS.md #44, #45, #47–#51):

* `remote exec` / `env` could not use the local machine (`--cluster local`) before a step needed it; the
  `local` cluster is now always available to them;
* the bash hook named the most recently touched run, not the run the command was working in; it now prefers
  the run whose run directory or plan directory holds the working directory.
* a checkpointed step that reached its time limit could only start over (a rerun gets a fresh
  `$FLOWER_STATE_DIR`): `flower rerun --keep-state` continues it, which is how Na finished;
* raising a foreach step's `timeout` would have re-run all 70 finished elements, although the timeout is not
  part of what a step does. This was tested on a toy run before touching this one. Such edits now apply as
  settings-only replacements;
* `flower add --in elements='[...]'` passed the list as a string; JSON values now keep their type;
* `sync` treated flower's own newer environment-step script as a plan edit (#47), and items appended to a running
  foreach waited for the whole step (#51; found on the S22 run, which shares this machine).

## Files
- [`sources.py`](sources.py): pinned downloads with hashes.
- [`structures.py`](structures.py): CIF → pw.x inputs (primitive cells, magnetism, k-mesh by spacing).
- [`eos.sh`](eos.sh): the seven pw.x runs of one element, resumable; V and E per atom.
- [`delta.py`](delta.py): Birch–Murnaghan fits, Δ against WIEN2k and against the published QE + SSSP entry, the
  port check, report and figure.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits (the cluster, the k/smearing inputs).
