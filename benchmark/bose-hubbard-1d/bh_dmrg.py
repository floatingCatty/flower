"""Ground state of the 1D Bose-Hubbard chain at density 1 by finite DMRG (TeNPy), as Kuhner, White and Monien:
H = -t sum_i (b+_i b_i+1 + h.c.) + (U/2) sum_i n_i (n_i - 1), U = 1, open ends, at most n_max bosons per site.

  python3 bh_dmrg.py L T [CHI] [N_MAX]
Writes gamma.npz (r, Gamma(r) = <b+_{c-r/2} b_{c+r/2}>, pairs centred on the middle of the chain) and prints JSON:
energy per site, the largest truncation error, the entanglement entropy at the centre, sweeps, seconds.
"""
import json
import sys
import time

import numpy as np
from tenpy.algorithms import dmrg
from tenpy.models.hubbard import BoseHubbardChain
from tenpy.networks.mps import MPS


def main(L, t, chi, nmax):
    t0 = time.time()
    M = BoseHubbardChain({"L": L, "n_max": nmax, "t": t, "U": 1.0, "mu": 0.0, "bc_MPS": "finite",
                          "conserve": "N"})
    psi = MPS.from_product_state(M.lat.mps_sites(), [1] * L, bc="finite")
    params = {"trunc_params": {"chi_max": chi, "svd_min": 1e-10}, "mixer": True, "max_sweeps": 40,
              "min_sweeps": 6, "max_E_err": 1e-10, "max_S_err": 1e-6,
              "chi_list": {0: min(32, chi), 2: min(96, chi), 4: chi}}
    info = dmrg.run(psi, M, params)
    c = L // 2
    rs = np.arange(1, min(L // 2, 160) + 1)
    i = c - rs // 2
    j = i + rs
    g = np.array([psi.expectation_value_term([("Bd", int(a)), ("B", int(b))]) for a, b in zip(i, j)]).real
    np.savez("gamma.npz", r=rs, gamma=g, L=L, t=t, chi=chi, nmax=nmax)
    eps = max(info["sweep_statistics"]["max_trunc_err"]) if info.get("sweep_statistics") else None
    return {"L": L, "t": t, "chi": chi, "n_max": nmax, "E_per_site": float(info["E"]) / L,
            "max_trunc_err": float(eps) if eps is not None else None,
            "S_center": float(psi.entanglement_entropy()[c - 1]),
            "sweeps": len(info["sweep_statistics"]["E"]) if info.get("sweep_statistics") else None,
            "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    L, t = int(sys.argv[1]), float(sys.argv[2])
    chi = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    nmax = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    print(json.dumps(main(L, t, chi, nmax)))
