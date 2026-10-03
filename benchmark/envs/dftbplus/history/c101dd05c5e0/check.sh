# dftb+ from this environment and the pbc-0-3 Si parameters; a real (tiny) run: Si2 at Gamma, SCC.
set -e
[ "$(command -v dftb+)" = "$FLOWER_ENV_PREFIX/bin/dftb+" ] && [ -f "$DFTB_SKDIR/Si-Si.skf" ]
d=$(mktemp -d); trap 'rm -rf "$d"' EXIT; cd "$d"
cat > dftb_in.hsd <<HSD
Geometry = GenFormat {
2 S
Si
1 1 0.0 0.0 0.0
2 1 1.35775 1.35775 1.35775
0.0 0.0 0.0
0.0 2.7155 2.7155
2.7155 0.0 2.7155
2.7155 2.7155 0.0
}
Hamiltonian = DFTB {
  SCC = Yes
  MaxAngularMomentum { Si = "p" }
  SlaterKosterFiles = Type2FileNames { Prefix = "$DFTB_SKDIR/"; Separator = "-"; Suffix = ".skf" }
}
ParserOptions { ParserVersion = 14 }
HSD
OMP_NUM_THREADS=1 dftb+ > out.log 2>&1
grep -q "SCC converged" out.log && echo "dftb+ Si2 SCC: OK"
