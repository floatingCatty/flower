# Benchmark: self-diffusion and viscosity of rigid water models (molecular dynamics)

**Paper:** S. Tazi, A. Boţan, M. Salanne, V. Marry, P. Turq and B. Rotenberg, "Diffusion coefficient and shear
viscosity of rigid water models", J. Phys.: Condens. Matter 24, 284117 (2012),
[arXiv:1204.2501](https://arxiv.org/abs/1204.2501). MD of SPC/E, TIP4P/2005 and Dang–Chang water at several box
sizes; the Yeh–Hummer relation D_PBC(L) = D₀ − ξ k_BT/(6πηL) gives the size-independent diffusion coefficient
D₀ and the shear viscosity η.

**Method here:** OpenMM 8.6.1 on the CPU of a remote workstation (the frozen `envs/openmm` recipe), both
non-polarizable models built from the paper's Table 1 (TIP4P/2005's M site as a virtual site), N = 128–2048
molecules at 0.998 g/cm³ and 300 K, Nosé–Hoover NVT, PME with the long-range LJ correction, 2 ns per run (2 fs
time step with rigid water; the paper used 1 fs and 1–10 ns), several seeds for the small boxes. D from the
oxygen mean squared displacement (10–100 ps), then a weighted Yeh–Hummer fit ([`yh_fit.py`](yh_fit.py)).
The polarizable Dang–Chang model is not reproduced.

## What it tested in flower

* **Checkpoint/restart across retries** (`$FLOWER_STATE_DIR`, added for this benchmark): one 2-ns run was
  killed hard (its whole session, SIGKILL) after 150 ps; flower reported it *lost*, retried it, and the retry
  continued from the checkpoint (150 → 250 → … frames) instead of starting over.
* A retry keeps its cluster slot and goes before fresh items (fixed after this run showed the opposite).
* New `inputs:` (ssh host and options, given with `-i`, never written to the plan file) and a new `clusters:`
  entry reached the running draft through `flower add`.

## Result

See the run's `yh` step (`report.md`, `yh.png`); the table is copied here when the run completes.
