# python3 from this environment; numpy, scipy and a headless matplotlib figure.
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
python3 - <<'PY'
import numpy, scipy, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
p, _ = curve_fit(lambda x, a: a * x, numpy.arange(5.0), 2 * numpy.arange(5.0))
assert abs(p[0] - 2) < 1e-9
plt.plot([0, 1]); plt.savefig("/dev/null", format="png")
print("numpy", numpy.__version__, "scipy", scipy.__version__, "matplotlib", matplotlib.__version__, "OK")
PY
