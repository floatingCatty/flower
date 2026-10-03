# python3, numpy, scipy, pyscf 2.14 from this environment, incl. the periodic X2C spin-orbit module; tiny SCF.
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
OMP_NUM_THREADS=2 python3 - <<'PY'
import numpy, scipy, pyscf
from pyscf import gto, scf
from pyscf.pbc.x2c import x2c1e  # noqa: F401  (periodic spin-orbit)
e = scf.RHF(gto.M(atom="H 0 0 0; H 0 0 0.74", basis="sto-3g", verbose=0)).kernel()
assert abs(e + 1.1167) < 1e-3, e
print("pyscf", pyscf.__version__, "numpy", numpy.__version__, "scipy", scipy.__version__, "H2 SCF OK")
PY
