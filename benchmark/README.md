# flower benchmarks

Real-science workflows used to test flower end to end on this machine. The `.flower/` project
in this folder holds the runs and is gitignored.

| benchmark | what | status |
|---|---|---|
| [`si-dos-fermi`](si-dos-fermi/README.md) | ABACUS DOS of Si, with E_F from Fermi-Dirac integration of the DOS and a k-mesh convergence check | E_F = 6.8301 eV (midgap) at 24³; re-run after the move to shell steps agrees to 1e-6 |
| [`si-valence`](si-valence/README.md) | Si valence-band top: Δso, HH/LH/SO masses, Luttinger γ's, strain HH–LH splitting with 7 methods (QE, ABACUS, PySCF, DFTB+, tight binding, EPM, k·p), all on a remote workstation, each code in a frozen environment recipe | run succeeded (8 min); results in the README |
| [`ttg-twistons`](ttg-twistons/README.md) | Theory of Turkel *et al.*, Science 2022 (twisted trilayer graphene): continuum model, Hartree–Fock (ours and the authors'), the authors' relaxation, corrugation | 10/15 claims reproduced, the rest explained |
| [`si-nte`](si-nte/README.md) | Si negative thermal expansion, Rignanese *et al.* PRB 1996: QE DFPT + quasi-harmonic, two pseudopotentials | 16/18 (15/18 with PseudoDojo); a rerun from a fresh clone agrees to 1e-6 (`flower compare`) |
| [`s22`](s22/README.md) | S22 interaction energies, Jurečka *et al.* PCCP 2006: PySCF MP2/CBS + ΔCCSD(T), 72 jobs | running (heavy CCSD(T) items) |
| [`water-diffusion`](water-diffusion/README.md) | Water self-diffusion and viscosity, Tazi *et al.* JPCM 2012: OpenMM, Yeh–Hummer | TIP4P/2005 reproduced; SPC/E D −4 % |
| [`j1j2-chain`](j1j2-chain/README.md) | Frustrated spin-1/2 chain, Eggert PRB 1996: exact diagonalization (QuSpin) up to L = 32, J2crit | 6/6, J2crit = 0.241167 as in the paper |
| [`ising-3d`](ising-3d/README.md) | 3d Ising critical point, Ferrenberg–Xu–Landau PRE 2018: Wolff Monte Carlo (numba), histogram reweighting, FSS, L ≤ 64 | K_c = 0.2216505(51) (0.8σ); ν = 0.607(11), not at the paper's precision; a kernel bug caught by exact enumeration first |
| [`delta-sssp`](delta-sssp/README.md) | Δ test of SSSP efficiency vs WIEN2k, Lejaeghere *et al.* Science 2016 / Prandini *et al.* 2018: 71 crystals × 7 volumes with QE, plus cutoff and smearing studies | average Δ 0.56 vs 0.44 meV/atom (median 0.35); the gap traced to cutoffs and one pseudopotential file (Au) |
| [`heisenberg-2d`](heisenberg-2d/README.md) | 2D Heisenberg antiferromagnet, Sandvik PRB 1997: SSE quantum Monte Carlo (operator loops, numba), L = 4…16, finite-size scaling | 7/7: E∞ = −0.669436(31) vs −0.669437(5); Table II 21/21 |
| [`bose-hubbard-1d`](bose-hubbard-1d/README.md) | 1D Bose–Hubbard Mott–superfluid KT point, Kühner–White–Monien PRB 2000: DMRG (TeNPy), L ≤ 512, Luttinger K from Γ(r) | t_c = 0.294 vs 0.297(10); the fit-interval drift reproduced for short intervals only |
