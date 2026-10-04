# python3 from this environment; a numba-compiled Metropolis simulation of the 2d Ising model at high temperature
# gives the nearest-neighbour correlation tanh(K) of the high-temperature expansion (up to O(tanh^3 K) = 0.001).
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
python3 - <<'PY'
import numpy as np, numba, scipy
from numba import njit

@njit(cache=False)
def run(L, K, sweeps, seed):
    np.random.seed(seed)
    s = np.ones((L, L), np.int64)
    acc = 0.0
    for t in range(sweeps):
        for _ in range(L * L):
            i = np.random.randint(L); j = np.random.randint(L)
            h = s[(i + 1) % L, j] + s[i - 1, j] + s[i, (j + 1) % L] + s[i, j - 1]
            if np.random.random() < np.exp(-2.0 * K * s[i, j] * h):
                s[i, j] = -s[i, j]
        if t >= sweeps // 4:
            e = 0.0
            for i in range(L):
                for j in range(L):
                    e += s[i, j] * (s[(i + 1) % L, j] + s[i, (j + 1) % L])
            acc += e / (2 * L * L)
    return acc / (sweeps - sweeps // 4)

K = 0.1
nn = run(32, K, 4000, 1)
ref = np.tanh(K)            # leading term of the high-temperature series; the next is O(tanh^3 K)
assert abs(nn - ref) < 0.006, (nn, ref)
print("numba", numba.__version__, "numpy", np.__version__, "scipy", scipy.__version__,
      "2d Ising <s s> at K=0.1: %.4f (tanh K %.4f) OK" % (nn, ref))
PY
