# python3 from this environment; numba compiles; h5py, scipy, matplotlib import.
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
python3 - <<'PY'
import numpy, scipy, h5py, matplotlib, numba, sympy
from numba import jit
@jit(nopython=True)
def f(n):
    s = 0.0
    for i in range(n):
        s += i
    return s
assert f(10) == 45.0
print("numpy", numpy.__version__, "scipy", scipy.__version__, "numba", numba.__version__, "h5py", h5py.__version__,
      "matplotlib", matplotlib.__version__, "sympy", sympy.__version__, "numba jit OK")
PY
