# Julia 1.10.10 (official binary, sha256-checked) + the packages of the authors' relaxation code (FFTW, Optim,
# LineSearches, Interpolations, TimerOutputs) at the exact versions of Manifest.toml, in a depot inside
# $FLOWER_ENV_PREFIX (nothing touches ~/.julia).
set -euo pipefail
V=1.10.10
SHA=6a78a03a71c7ab792e8673dc5cedb918e037f081ceb58b50971dfb7c64c5bf81
mkdir -p "$FLOWER_ENV_PREFIX"
cd "$FLOWER_ENV_PREFIX"
curl -fsSL -o julia.tgz "https://julialang-s3.julialang.org/bin/linux/x64/${V%.*}/julia-$V-linux-x86_64.tar.gz"
echo "$SHA  julia.tgz" | sha256sum -c -
tar xzf julia.tgz --strip-components=1
rm julia.tgz
mkdir -p project
cp "$FLOWER_ENV_DIR/Project.toml" "$FLOWER_ENV_DIR/Manifest.toml" project/
JULIA_DEPOT_PATH="$FLOWER_ENV_PREFIX/depot" JULIA_PROJECT="$FLOWER_ENV_PREFIX/project" \
  ./bin/julia -e 'using Pkg; Pkg.instantiate(); Pkg.precompile()'
