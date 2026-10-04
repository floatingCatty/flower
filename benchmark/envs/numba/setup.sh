# numba 0.68 with numpy 2.4 / scipy 1.17 on Python 3.11 (conda-forge) into $FLOWER_ENV_PREFIX, pinned to the exact package
# files (conda-explicit.txt). For Monte Carlo kernels.
set -euo pipefail
CONDA=$(command -v micromamba || command -v mamba || command -v conda || true)
for c in "$HOME/softwares/miniconda3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda"; do
  if [ -z "$CONDA" ] && [ -x "$c" ]; then CONDA="$c"; fi
done
[ -n "$CONDA" ] || { echo "no conda, mamba or micromamba on this host" >&2; exit 1; }
"$CONDA" create -y -p "$FLOWER_ENV_PREFIX" --file "$FLOWER_ENV_DIR/conda-explicit.txt"
