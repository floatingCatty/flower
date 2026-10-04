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

The campaign is still running; the results will be added here.

## How it ran (one flower run, started with `flower start`)

`sources` (the Δ package at a pinned commit, SSSP 1.1 efficiency and its cutoffs, the SSSP paper; sha256 of
each) → `env-ase-local` (a new frozen `ase` recipe: ASE + spglib) → `structures` (71 × 7 pw.x inputs, the
fan-out list sorted by estimated cost) → `env-qe-remote` → `eos-smoke` (Si, Fe) + `delta-smoke` → `eos`
(71 elements, 4 cores each, checkpointed per volume, `--retry 2 --on-failure continue`) → `delta`. A
`delta-preview` step (`${eos.partial}`) showed the results while the campaign ran. The cluster's cpu budget was
raised from 16 to 28 cores mid-run with `flower sync`, once the water benchmark had released the machine.

What this benchmark found in flower:

* `remote exec` / `env` could not use the local machine (`--cluster local`) before a step needed it; the
  `local` cluster is now always available to them;
* the bash hook named the most recently touched run, not the run the command was working in; it now prefers
  the run whose run directory or plan directory holds the working directory.

## Files
- [`sources.py`](sources.py): pinned downloads with hashes.
- [`structures.py`](structures.py): CIF → pw.x inputs (primitive cells, magnetism, k-mesh by spacing).
- [`eos.sh`](eos.sh): the seven pw.x runs of one element, resumable; V and E per atom.
- [`delta.py`](delta.py): Birch–Murnaghan fits, Δ against WIEN2k and against the published QE + SSSP entry, the
  port check, report and figure.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits (the cluster, the k/smearing inputs).
