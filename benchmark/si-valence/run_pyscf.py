"""PySCF: all-electron periodic PBE with spin-orbit coupling (X2C1E, two-component GKS), Si at a0.

usage: python run_pyscf.py [kmesh=4] [basis=cc-pvdz]   ->  eig.json (common format, see si_valence.py)
"""
import json
import sys
import time

import numpy as np
from pyscf.pbc import dft, gto

import si_valence as sv


def build(strained: bool, basis: str):
    vecs, frac = sv.lattice(strained)
    cell = gto.Cell()
    cell.unit = "A"
    cell.a = np.array(vecs)
    cart = [np.dot(f, np.array(vecs)) for f in frac]
    cell.atom = [["Si", list(c)] for c in cart]
    cell.basis = basis
    cell.precision = 1e-9
    cell.verbose = 4
    cell.build()
    return cell, vecs


def bands(mf, cell, kpts, kb):
    """Eigenvalues (Ha) at arbitrary k for a converged two-component KGKS(x2c1e) calculation.

    PySCF 2.14 has no KGHF.get_bands, and numint2c.nr_vxc mis-sizes its result when kpts_band is given.
    Si is non-magnetic and the XC kernel is collinear, so the 2c potential is block-diagonal with the
    ordinary (1c) J + Vxc of the total density in both spin blocks; the spin-orbit part lives in the
    X2C1E core Hamiltonian, which PySCF does evaluate at any k."""
    import scipy.linalg
    n = cell.nao_nr()
    dm = np.asarray(mf.make_rdm1())
    dm1 = dm[:, :n, :n] + dm[:, n:, n:]                       # total density matrix per k (1c)
    h = np.asarray(mf.get_hcore(cell, kb))                    # (nkb, 2n, 2n), includes SOC
    vj = np.asarray(mf.with_df.get_jk(dm1, kpts=kpts, kpts_band=kb, with_k=False)[0])
    ni = dft.numint.KNumInt()
    _, _, vxc = ni.nr_rks(cell, mf.grids, mf.xc, dm1, kpts=kpts, kpts_band=kb)
    v1 = vj + np.asarray(vxc)
    s1 = np.asarray(cell.pbc_intor("int1e_ovlp", hermi=1, kpts=kb))
    out = []
    for i in range(len(kb)):
        f = h[i].copy()
        f[:n, :n] += v1[i]
        f[n:, n:] += v1[i]
        s2 = scipy.linalg.block_diag(s1[i], s1[i])
        out.append(scipy.linalg.eigh(f, s2, eigvals_only=True))
    return out


def run(kmesh=4, basis="cc-pvdz"):
    sets, notes = {}, []
    for name, ks in sv.kpoint_sets().items():
        t0 = time.time()
        cell, vecs = build(name == "strained", basis)
        kpts = cell.make_kpts([kmesh] * 3)
        mf = dft.KGKS(cell, kpts).x2c1e()   # spin-orbit coupling through the X2C1E core Hamiltonian
        mf.xc = "pbe"
        mf.collinear = "col"                # non-magnetic: collinear XC kernel is exact here
        mf = mf.density_fit()
        mf.conv_tol = 1e-9
        mf.kernel()
        kb = np.array(ks) * 2 * np.pi / (sv.A0 / sv.BOHR)  # 2pi/a0 units -> bohr^-1
        e_kn = bands(mf, cell, kpts, kb)
        # self-check: Gamma is kpts[0] of the SCF mesh, so the by-hand bands must reproduce its eigenvalues
        dev = float(np.max(np.abs(np.sort(e_kn[0])[:40] - np.sort(mf.mo_energy[0])[:40]))) * sv.HA
        if dev > 1e-4:
            raise SystemExit(f"band reconstruction disagrees with the SCF at Gamma by {dev:.2e} eV")
        sets[name] = {"kpts": ks, "eig": [list(np.asarray(e) * sv.HA) for e in e_kn]}
        notes.append(f"{name}: converged={mf.converged}, E={mf.e_tot:.8f} Ha, Gamma check {dev:.1e} eV, "
                     f"{time.time() - t0:.0f}s")
    sv.write_eig("eig.json", f"PySCF 2.14 (all-electron {basis}, PBE, X2C1E SOC)", sets, nelec=28,
                 notes=f"KGKS + x2c1e, GDF, {kmesh}^3 k-mesh; " + "; ".join(notes))


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 4, sys.argv[2] if len(sys.argv) > 2 else "cc-pvdz")
