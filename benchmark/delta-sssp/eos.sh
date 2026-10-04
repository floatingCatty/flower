#!/bin/bash
# The seven volumes of one element with pw.x: eos.sh DIR NPROC
#   DIR holds <El>_094.in ... <El>_106.in and the pseudopotential (from the `structures` step).
# Each finished volume is kept in $FLOWER_STATE_DIR, so a retry continues where the last attempt stopped.
# Prints {"el", "V": [A^3/atom], "E": [eV/atom], "mag": [total magnetization], "iterations": [...]}.
set -euo pipefail
dir=$1; np=$2
state=${FLOWER_STATE_DIR:-.}
el=$(basename "$dir")
cd "$dir"
for f in "${el}"_*.in; do
  out="$state/${f%.in}.out"
  if [ -f "$out" ] && grep -q "JOB DONE" "$out"; then continue; fi
  mpirun --oversubscribe -np "$np" pw.x -nk "$np" -in "$f" > "$out.tmp" 2>&1 || { tail -20 "$out.tmp" >&2; exit 1; }
  grep -q "convergence has been achieved" "$out.tmp" || { echo "$f: SCF did not converge" >&2; exit 1; }
  mv "$out.tmp" "$out"
  rm -rf tmp
done
V=; E=; M=; I=; sep=
for out in $(ls "$state"/"${el}"_*.out | sort); do
  read -r v e m it < <(awk '
    /number of atoms\/cell/ { nat = $5 }
    /unit-cell volume/      { vol = $4 }
    /^!/                    { e = $5 }
    /total magnetization/   { m = $4 }
    /convergence has been achieved/ { it = $6 }
    END { printf "%.8f %.10f %s %s\n", vol * 0.529177210903^3 / nat, e * 13.605693122994 / nat, (m == "" ? "null" : m), it }
  ' "$out")
  V="$V$sep$v"; E="$E$sep$e"; M="$M$sep$m"; I="$I$sep$it"; sep=", "
done
printf '{"el": "%s", "V": [%s], "E": [%s], "mag": [%s], "iterations": [%s]}\n' "$el" "$V" "$E" "$M" "$I"
