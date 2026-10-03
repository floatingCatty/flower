# DFTB+ 25.1 (conda-forge, serial build) into $FLOWER_ENV_PREFIX, pinned to the exact package files
# (conda-explicit.txt), plus the pbc-0-3 Slater-Koster set from the dftbparams GitHub release, checked by sha256.
set -euo pipefail
CONDA=$(command -v micromamba || command -v mamba || command -v conda || true)
for c in "$HOME/softwares/miniconda3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda"; do
  if [ -z "$CONDA" ] && [ -x "$c" ]; then CONDA="$c"; fi
done
[ -n "$CONDA" ] || { echo "no conda, mamba or micromamba on this host" >&2; exit 1; }
"$CONDA" create -y -p "$FLOWER_ENV_PREFIX" --file "$FLOWER_ENV_DIR/conda-explicit.txt"
mkdir -p "$FLOWER_ENV_PREFIX/share/slakos" && cd "$FLOWER_ENV_PREFIX/share/slakos"
curl -sSLfo pbc-0-3.tar.xz https://github.com/dftbparams/pbc/releases/download/v0.3.0/pbc-0-3.tar.xz
echo "15a26b9c427fe48198f46f54b47f6f56b631d559e3bd4bb1653c5d4cff448d0b  pbc-0-3.tar.xz" | sha256sum -c -
tar xf pbc-0-3.tar.xz
