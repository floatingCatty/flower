# python3 from this environment; the lowest energy over QuSpin's (k = 0, pi; parity; spin inversion) blocks of the
# L=10 Heisenberg ring equals that of a dense diagonalization of the full 1024-dimensional space.
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
python3 - <<'PY'
import numpy as np, scipy, quspin
from quspin.basis import spin_basis_1d
from quspin.operators import hamiltonian
L = 10
bonds = [[1.0, i, (i + 1) % L] for i in range(L)]
ops = [["xx", bonds], ["yy", bonds], ["zz", bonds]]
e_block = min(hamiltonian(ops, [], basis=spin_basis_1d(L, pauli=False, Nup=L // 2, kblock=k, pblock=p, zblock=z),
                          dtype=np.float64, check_symm=False, check_herm=False).eigsh(k=1, which="SA")[0][0]
              for k in (0, L // 2) for p in (1, -1) for z in (1, -1))
full = hamiltonian(ops, [], basis=spin_basis_1d(L, pauli=False), dtype=np.float64, check_symm=False,
                   check_herm=False).toarray()
e_full = np.linalg.eigvalsh(full)[0]
assert abs(e_block - e_full) < 1e-10, (e_block, e_full)
print("quspin", quspin.__version__, "numpy", np.__version__, "scipy", scipy.__version__,
      "L=10 ring E0 = %.10f (block) = %.10f (full) OK" % (e_block, e_full))
PY
