# flower benchmarks

Real-science workflows used to test flower end to end on this machine. The `.flower/` project
in this folder holds the runs and is gitignored.

| benchmark | what | status |
|---|---|---|
| [`si-dos-fermi`](si-dos-fermi/README.md) | ABACUS DOS of Si, with E_F from Fermi-Dirac integration of the DOS and a k-mesh convergence check | compute nodes pass; agent review blocked by local `claude` auth |
| [`si-valence`](si-valence/README.md) | Si valence-band top: Δso, HH/LH/SO masses, Luttinger γ's, strain HH–LH splitting with 7 methods (QE, ABACUS, PySCF, DFTB+, tight binding, EPM, k·p), all on a remote workstation, each code in a frozen environment recipe | run succeeded (8 min); results in the README |
