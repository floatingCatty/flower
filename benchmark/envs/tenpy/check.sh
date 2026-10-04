# python3 from this environment; TeNPy's finite DMRG on a 6-site Bose-Hubbard chain (open ends, density 1, at most
# 3 bosons per site, t = 0.3, U = 1) gives the ground-state energy of an exact diagonalization of the same model.
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
python3 - <<'PY'
import itertools, numpy as np, scipy, tenpy
from tenpy.models.hubbard import BoseHubbardChain
from tenpy.networks.mps import MPS
from tenpy.algorithms import dmrg
L, nmax, t, U = 6, 3, 0.3, 1.0
M = BoseHubbardChain({"L": L, "n_max": nmax, "t": t, "U": U, "mu": 0., "bc_MPS": "finite", "conserve": "N"})
psi = MPS.from_product_state(M.lat.mps_sites(), [1] * L, bc="finite")
info = dmrg.run(psi, M, {"trunc_params": {"chi_max": 100, "svd_min": 1e-12}, "mixer": True, "max_sweeps": 30})
states = [s for s in itertools.product(range(nmax + 1), repeat=L) if sum(s) == L]
idx = {s: k for k, s in enumerate(states)}
H = np.zeros((len(states), len(states)))
for k, s in enumerate(states):
    H[k, k] = U / 2 * sum(n * (n - 1) for n in s)
    for i in range(L - 1):
        for a, b in ((i, i + 1), (i + 1, i)):
            if s[a] > 0 and s[b] < nmax:
                s2 = list(s); s2[a] -= 1; s2[b] += 1
                H[idx[tuple(s2)], k] += -t * np.sqrt(s[a] * (s[b] + 1))
e_ed = np.linalg.eigvalsh(H)[0]
assert abs(info["E"] - e_ed) < 1e-8, (info["E"], e_ed)
print("tenpy", tenpy.__version__, "numpy", np.__version__, "scipy", scipy.__version__,
      "Bose-Hubbard L=6: DMRG %.10f = ED %.10f OK" % (info["E"], e_ed))
PY
