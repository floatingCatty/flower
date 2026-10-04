"""One linear hydrogen chain H_N with spacing R (bohr) in the minimal STO-6G basis, by every deterministic method
of the paper's Tables II and V: RHF, UHF, RCCSD, RCCSD(T), UCCSD, UCCSD(T), and FCI when N <= 12.

  python3 hchain.py N R [fci-only]
Prints the outputs JSON: energies per atom (hartree), convergence flags, and <S^2> of the UHF solution.

UHF starts from an antiferromagnetic guess (spin up on even sites, down on odd) and is followed by a stability
analysis until no lower solution is found: the broken-symmetry state the paper's UHF and UCC columns use (it
coincides with RHF at short R).
"""
import json
import sys
import time

import numpy as np
from pyscf import cc, fci, gto, scf


def chain(n, r):
    atoms = [["H", (0.0, 0.0, i * r)] for i in range(n)]
    return gto.M(atom=atoms, basis="sto-6g", unit="Bohr", spin=n % 2, verbose=0)


def uhf_lowest(mol, n):
    mf = scf.UHF(mol)
    mf.max_cycle = 300
    nao = mol.nao
    da = np.zeros((nao, nao)); db = np.zeros((nao, nao))
    for i in range(n):        # one 1s function per atom in STO-6G
        (da if i % 2 == 0 else db)[i, i] = 1.0
    e = mf.kernel(dm0=(da, db))
    for _ in range(10):       # follow instabilities until the solution is a minimum
        mo, _, stable, _ = mf.stability(return_status=True)
        if stable:
            break
        dm = mf.make_rdm1(mo, mf.mo_occ)
        e = mf.kernel(dm0=dm)
    return mf


def main(n, r, fci_only=False):
    t0 = time.time()
    mol = chain(n, r)
    out = {"N": n, "R": r}
    rhf = scf.RHF(mol)
    rhf.max_cycle = 300
    rhf.kernel()
    out["RHF"] = rhf.e_tot / n
    if n <= 12:
        out["FCI"] = fci.FCI(rhf).kernel()[0] / n
    if not fci_only:
        uhf = uhf_lowest(mol, n)
        out["UHF"] = uhf.e_tot / n
        out["UHF_S2"] = float(uhf.spin_square()[0])
        for name, mf, ccmod in (("R", rhf, cc.CCSD), ("U", uhf, cc.UCCSD)):
            m = ccmod(mf)
            m.max_cycle = 300
            try:
                m.kernel()
                out[name + "CCSD"] = m.e_tot / n
                out[name + "CCSD(T)"] = (m.e_tot + m.ccsd_t()) / n
                out[name + "CCSD_converged"] = bool(m.converged)
            except Exception as exc:       # recorded, not hidden: the paper reports RCC breaking down at large R
                out[name + "CCSD"] = out[name + "CCSD(T)"] = None
                out[name + "CCSD_error"] = str(exc)[:200]
    out["seconds"] = round(time.time() - t0, 1)
    return out


if __name__ == "__main__":
    n, r = int(sys.argv[1]), float(sys.argv[2])
    print(json.dumps(main(n, r, len(sys.argv) > 3 and sys.argv[3] == "fci-only")))
