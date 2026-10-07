"""Low-lying states of the open S = 1 Heisenberg chain by DMRG (Sorensen & Affleck 1993), with TeNPy.

H = sum_i S_i . S_{i+1}, open ends. The lowest state of each total S^z = M sector: M = 1 is the end spins'
triplet (the paper's 1-), M = 2, 3, 4 add m = M - 1 magnons (2+, 3-, 4+).

  python3 haldane.py check                 DMRG against exact diagonalization (L = 10, every M = 0..4)
  python3 haldane.py point L CHI [--profiles]
        the lowest energy for M = 0..4 at length L, bond dimension CHI; --profiles adds <S^z_i> and the bond
        energies <S_i . S_{i+1}> of each state and its parity under i -> L-1-i. Prints one JSON line (the step's
        outputs).
"""
import json
import sys
import time

import numpy as np

MS = (0, 1, 2, 3, 4)


def model(L):
    from tenpy.models.spins import SpinChain
    return SpinChain({"L": L, "S": 1.0, "Jx": 1.0, "Jy": 1.0, "Jz": 1.0, "bc_MPS": "finite", "conserve": "Sz"})


def lowest(L, M, chi, profiles=False):
    """DMRG for the lowest state with total S^z = M (a Neel product state with M of its 'down' sites set to 0)."""
    from tenpy.algorithms import dmrg
    from tenpy.networks.mps import MPS
    mod = model(L)
    sites = mod.lat.mps_sites()
    idx = {int(round(v)): i for i, v in enumerate(np.diag(sites[0].get_op("Sz").to_ndarray()))}   # S^z -> basis index
    st = [1, -1] * (L // 2)
    downs = [i for i, s in enumerate(st) if s == -1]
    for j in np.linspace(0, len(downs) - 1, M).astype(int) if M else []:
        st[downs[j]] = 0
    psi = MPS.from_product_state(sites, [idx[s] for s in st], bc="finite")
    info = dmrg.run(psi, mod, {"mixer": True, "max_E_err": 1e-12, "max_S_err": 1e-7, "min_sweeps": 8,
                               "max_sweeps": 80, "trunc_params": {"chi_max": chi, "svd_min": 1e-14}})
    stats = info["sweep_statistics"]
    out = {"E": float(info["E"]), "sweeps": len(stats["sweep"]), "max_trunc_err": float(np.max(stats["max_trunc_err"][-2:])),
           "dE_last": float(abs(stats["E"][-1] - stats["E"][-2])) if len(stats["E"]) > 1 else None,
           "Sz_total": float(np.sum(psi.expectation_value("Sz")))}
    if profiles:
        out["sz"] = [float(x) for x in psi.expectation_value("Sz")]
        out["bond"] = [float(x) for x in mod.bond_energies(psi)]
        mirror = psi.copy()
        mirror.spatial_inversion()               # site i -> L-1-i: the paper's parity, <psi|P|psi> = +-1
        out["parity"] = float(np.real(psi.overlap(mirror)))
    return out


def ed_lowest(L, M):
    """Exact lowest energy in the S^z = M sector of the open chain (sparse Lanczos)."""
    from itertools import product
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import eigsh
    basis = [s for s in product((1, 0, -1), repeat=L) if sum(s) == M]
    index = {s: i for i, s in enumerate(basis)}
    rows, cols, vals = [], [], []
    for i, s in enumerate(basis):
        diag = sum(s[j] * s[j + 1] for j in range(L - 1))
        rows.append(i); cols.append(i); vals.append(diag)
        for j in range(L - 1):   # (S+_j S-_{j+1} + h.c.)/2, with S+|m> = sqrt(2 - m(m+1))|m+1> for S = 1
            a, b = s[j], s[j + 1]
            for da, db in ((1, -1), (-1, 1)):
                na, nb = a + da, b + db
                if abs(na) <= 1 and abs(nb) <= 1:
                    amp = np.sqrt(2 - a * (a + da)) * np.sqrt(2 - b * (b + db)) / 2
                    t = list(s); t[j], t[j + 1] = na, nb
                    rows.append(index[tuple(t)]); cols.append(i); vals.append(amp)
    H = coo_matrix((vals, (rows, cols)), shape=(len(basis), len(basis))).tocsr()
    return float(eigsh(H, k=1, which="SA")[0][0])


def main():
    if sys.argv[1] == "check":
        L, rows = 10, []
        for M in MS:
            e, d = ed_lowest(L, M), lowest(L, M, 3 ** (L // 2))["E"]   # the exact bond dimension: no truncation
            rows.append({"M": M, "ed": e, "dmrg": d, "diff": abs(e - d)})
        worst = max(r["diff"] for r in rows)
        assert worst < 1e-9, rows
        print(json.dumps({"ok": True, "max_diff": worst, "rows": rows}))
        return
    L, chi = int(sys.argv[2]), int(sys.argv[3])
    t0 = time.time()
    res = {M: lowest(L, M, chi, profiles="--profiles" in sys.argv) for M in MS}
    print(json.dumps({"L": L, "chi": chi, "E": [res[M]["E"] for M in MS],
                      "max_trunc_err": max(r["max_trunc_err"] for r in res.values()),
                      "Sz_total": [round(res[M]["Sz_total"], 6) for M in MS],
                      "seconds": round(time.time() - t0, 1),
                      **({"sz": [res[M]["sz"] for M in MS], "bond": [res[M]["bond"] for M in MS],
                          "parity": [round(res[M]["parity"], 6) for M in MS]}
                         if "--profiles" in sys.argv else {})}))


if __name__ == "__main__":
    main()
