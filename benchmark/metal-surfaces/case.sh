#!/bin/bash
# One case of the metal-surface study with pw.x: case.sh CASE NPROC
#   CASE holds numbered pw.x inputs and the pseudopotential (from surf.py). Each finished input is kept in
#   $FLOWER_STATE_DIR, so a retry continues where the last attempt stopped. A slab input (*.slab.in) also gets its
#   bare + Hartree potential averaged over planes (pp.x plot_num 11, average.x): <name>.avg, whose first point is the
#   middle of the vacuum (the slab is centred in its cell).
# Writes the outputs to out/ (fetched back); prints {"case", "runs": [{"name", "E_Ry", "Ef_eV", "Vvac_eV", "nat", "vol_bohr3",
#   "iterations", "bfgs_steps", "wall"}]}.
set -euo pipefail
case=$1; np=$2
state=${FLOWER_STATE_DIR:-.}
export OMP_NUM_THREADS=1
cd "$case"
mkdir -p ../out
for f in *.in; do
  name=${f%.in}
  out="$state/$name.out"
  if ! { [ -f "$out" ] && grep -q "JOB DONE" "$out"; }; then
    mpirun --oversubscribe --bind-to none -np "$np" pw.x -nk "$np" -in "$f" > "$out.tmp" 2>&1 \
      || { tail -20 "$out.tmp" >&2; exit 1; }
    grep -q "convergence NOT achieved" "$out.tmp" && { echo "$f: SCF did not converge" >&2; exit 1; }
    if grep -q "'relax'" "$f" && ! grep -q "bfgs converged" "$out.tmp"; then echo "$f: relaxation did not converge" >&2; exit 1; fi
    if [[ $f == *.slab.in ]]; then
      printf "&inputpp\n prefix='x', outdir='./tmp', filplot='pot', plot_num=11\n/\n" > pp.in
      mpirun --oversubscribe --bind-to none -np 1 pp.x -in pp.in > pp.out 2>&1 || { tail pp.out >&2; exit 1; }
      printf "1\npot\n1.0\n4000\n3\n3.0\n" | average.x > average.out 2>&1 || { tail average.out >&2; exit 1; }
      mv avg.dat "$state/$name.avg"
      rm -f pot pp.in pp.out average.out
    fi
    rm -rf tmp
    mv "$out.tmp" "$out"
  fi
  cp "$out" ../out/; [ -f "$state/$name.avg" ] && cp "$state/$name.avg" ../out/
done
runs=; sep=
for f in *.in; do
  name=${f%.in}
  vvac=null
  [ -f "$state/$name.avg" ] && vvac=$(awk 'NR == 1 { printf "%.6f", $2 * 13.605693122994 }' "$state/$name.avg")
  runs="$runs$sep$(awk -v name="$name" -v vvac="$vvac" '
    /number of atoms\/cell/ { nat = $5 }
    /unit-cell volume/      { vol = $4 }
    /^!/                    { e = $5 }
    /the Fermi energy is/   { ef = $5 }
    /convergence has been achieved/ { it += $6 }
    /number of bfgs steps/  { bfgs = $6 }
    /PWSCF  *:/             { w = $0 }
    END { n = split(w, t, "CPU"); wall = t[n]; gsub(/WALL| /, "", wall)
          printf "{\"name\": \"%s\", \"E_Ry\": %.10f, \"Ef_eV\": %s, \"Vvac_eV\": %s, \"nat\": %d, \"vol_bohr3\": %.6f, \"iterations\": %d, \"bfgs_steps\": %d, \"wall\": \"%s\"}",
                 name, e, ef, vvac, nat, vol, it, bfgs, wall }' "$state/$name.out")"
  sep=", "
done
printf '{"case": "%s", "runs": [%s]}\n' "$(basename "$case")" "$runs"
