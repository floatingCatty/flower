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

## Result (run `water-diffusion-20261003-192445-c1a8`): TIP4P/2005 reproduced, SPC/E close but outside the bars

| | paper D₀ (10⁻⁹ m²/s) | this run | paper η (cP, from D(L)) | this run | |
|---|---|---|---|---|---|
| SPC/E | 2.97 ± 0.05 | 2.86 ± 0.03 | 0.64 ± 0.02 | 0.79 ± 0.06 | ✗ (D −4 %, η +23 %) |
| TIP4P/2005 | 2.49 ± 0.06 | 2.45 ± 0.03 | 0.83 ± 0.07 | 0.77 ± 0.07 | ✓ |
| experiment | 2.3 | | 0.896 | | |

D_PBC is linear in 1/L for both models (χ²/dof 0.19 and 0.45 over N = 128–2048; `yh.png`). Qualitatively:
SPC/E diffuses faster and is less viscous than real water (✓, as the paper says); TIP4P/2005 is closer to
experiment in D (✓) but not in η here (our two η values coincide within their bars). The SPC/E differences are
plausibly protocol: a 2 fs time step (the paper: 1 fs), our real-space cutoff min(1 nm, 0.45 L) and Nosé–Hoover
coupling (1 ps⁻¹), and 2 ns runs (the paper: 1–10 ns); the paper's own Green–Kubo η for SPC/E (0.68) is also
above its D(L) estimate. The Dang–Chang model and Green–Kubo viscosities were not reproduced.

The Yeh–Hummer fit was first run as a preview on 21 of the 22 MD runs (`yh-preview`, `${md.partial}`) while the
largest box was still running; the final fit moved D₀(TIP4P/2005) from 2.43 to 2.45.
