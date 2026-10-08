# TeNPy (tensor networks: DMRG) on Python 3.11 + numpy/scipy with OpenBLAS (conda-forge),
# pinned: the exact conda package files (conda-explicit.txt) and the exact pip versions, installed without
# dependency resolution (requirements.txt).
set -euo pipefail
CONDA=$(command -v micromamba || command -v mamba || command -v conda || true)
for c in "$HOME/softwares/miniconda3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda"; do
  if [ -z "$CONDA" ] && [ -x "$c" ]; then CONDA="$c"; fi
done
[ -n "$CONDA" ] || { echo "no conda, mamba or micromamba on this host" >&2; exit 1; }
"$CONDA" create -y -p "$FLOWER_ENV_PREFIX" --file "$FLOWER_ENV_DIR/conda-explicit.txt"
PIP="$FLOWER_ENV_PREFIX/bin/pip"
if ! "$PIP" install --no-deps --no-cache-dir --only-binary=:all: -r "$FLOWER_ENV_DIR/requirements.txt"; then
  # No wheel fits this host: TeNPy publishes Linux wheels for glibc >= 2.28 only, so an older one (CentOS 7: 2.17)
  # gets the source package. Installed against the numpy above, without Cython, it is TeNPy's pure-Python
  # fallback: the same 1.1.1 code without one compiled speed-up (its shipped C++ no longer builds with numpy 2.4).
  "$PIP" install --no-deps --no-cache-dir --no-build-isolation -r "$FLOWER_ENV_DIR/requirements.txt"
fi
