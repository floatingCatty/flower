"""Is numba's np.random.randint(n) uniform on [0, n)? Histogram of 2.7 million draws, n = 27 (and the explicit
two-argument form, and int(n * random()))."""
import json
import numpy as np
from numba import njit


@njit
def draws(n, k, how):
    np.random.seed(1)
    c = np.zeros(n + 2, dtype=np.int64)
    for _ in range(k):
        if how == 0:
            x = np.random.randint(n)
        elif how == 1:
            x = np.random.randint(0, n)
        else:
            x = int(np.random.random() * n)
        c[min(max(x, -1) + 1, n + 1)] += 1      # c[0]: x < 0, c[n+1]: x >= n
    return c


out = {}
for how, name in enumerate(["randint(n)", "randint(0, n)", "int(n*random())"]):
    c = draws(27, 2700000, how)
    inside = c[1:28]
    out[name] = {"below_0": int(c[0]), "above_n_minus_1": int(c[28]), "min": int(inside.min()),
                 "max": int(inside.max()), "chi2_dof": float(((inside - 1e5) ** 2 / 1e5).sum() / 26)}
print(json.dumps(out))
