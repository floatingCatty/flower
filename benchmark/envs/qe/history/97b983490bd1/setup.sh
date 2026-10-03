# Quantum ESPRESSO 7.5 (conda-forge, OpenMPI 5.0.11) into $FLOWER_ENV_PREFIX, pinned to the exact package
# files of the environment explored on the target (conda-explicit.txt: URLs + md5). Needs conda/mamba/micromamba.
set -euo pipefail
CONDA=$(command -v micromamba || command -v mamba || command -v conda || true)
for c in "$HOME/softwares/miniconda3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda"; do
  if [ -z "$CONDA" ] && [ -x "$c" ]; then CONDA="$c"; fi
done
[ -n "$CONDA" ] || { echo "no conda, mamba or micromamba on this host" >&2; exit 1; }
"$CONDA" create -y -p "$FLOWER_ENV_PREFIX" --file "$FLOWER_ENV_DIR/conda-explicit.txt"
