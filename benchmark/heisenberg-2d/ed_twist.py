"""The spin stiffness of the 4 x 4 Heisenberg lattice from exact diagonalization with a twist phi on every x bond
(S+_i S-_j -> e^{i phi} S+_i S-_j): rho_s = (3/2) (1/N) d^2 E0 / d phi^2, the paper's own check (Sec. III gives
0.04840), and the winding fluctuation it implies at inverse temperature beta, <Wx^2> = beta * (2/3) rho_s.

  python3 ed_twist.py [BETA]     prints JSON
"""
import json
import sys

import numpy as np
from quspin.basis import spin_basis_1d
from quspin.operators import hamiltonian

L = 4
N = L * L


def e0(phi):
    xb, yb = [], []
    for y in range(L):
        for x in range(L):
            i = x + y * L
            xb.append((i, (x + 1) % L + y * L))
            yb.append((i, x + ((y + 1) % L) * L))
    zz = [[1.0, i, j] for i, j in xb + yb]
    pm = [[0.5 * np.exp(1j * phi), i, j] for i, j in xb] + [[0.5, i, j] for i, j in yb]
    mp = [[0.5 * np.exp(-1j * phi), i, j] for i, j in xb] + [[0.5, i, j] for i, j in yb]
    basis = spin_basis_1d(N, pauli=False, Nup=N // 2)
    H = hamiltonian([["zz", zz], ["+-", pm], ["-+", mp]], [], basis=basis, dtype=np.complex128,
                    check_symm=False, check_herm=False, check_pcon=False)
    return float(H.eigsh(k=1, which="SA", tol=1e-13)[0][0])


h = 1e-3
E = [e0(-h), e0(0.0), e0(h)]
d2 = (E[0] - 2 * E[1] + E[2]) / h ** 2
rho = 1.5 * d2 / N
beta = float(sys.argv[1]) if len(sys.argv) > 1 else 32.0
print(json.dumps({"E0_per_site": E[1] / N, "d2E_dphi2": d2, "rho_s": rho, "paper_rho_s_4": 0.04840,
                  "beta": beta, "Wx2_implied": beta * 2 / 3 * rho}))
