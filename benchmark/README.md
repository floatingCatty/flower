# flower benchmarks

Real-science workflows used to test flower end to end on this machine. The `.flower/` project
in this folder holds the runs and is gitignored.

| benchmark | what | status |
|---|---|---|
| [`si-dos-fermi`](si-dos-fermi/README.md) | ABACUS DOS of Si, with E_F from Fermi-Dirac integration of the DOS and a k-mesh convergence check | compute nodes pass; agent review blocked by local `claude` auth |
