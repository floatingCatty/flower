# Python 3.11 with numpy, scipy, numba, h5py, matplotlib (conda-forge) into $FLOWER_ENV_PREFIX, pinned to the
# exact package files (conda-explicit.txt). For the authors' released TTG Hartree-Fock scripts (numba, h5py)
# and our continuum / Hartree-Fock code.
set -euo pipefail
CONDA=$(command -v micromamba || command -v mamba || command -v conda || true)
for c in "$HOME/softwares/miniconda3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda"; do
  if [ -z "$CONDA" ] && [ -x "$c" ]; then CONDA="$c"; fi
done
[ -n "$CONDA" ] || { echo "no conda, mamba or micromamba on this host" >&2; exit 1; }
"$CONDA" create -y -p "$FLOWER_ENV_PREFIX" --file "$FLOWER_ENV_DIR/conda-explicit.txt"
