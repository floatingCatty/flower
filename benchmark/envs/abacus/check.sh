# abacus 3.9.0 and MPI from this environment, plus python3 for the benchmark's helper script.
set -e
v=$(abacus --version 2>&1 | head -1); echo "$v"; echo "$v" | grep -q "v3.9.0"
[ "$(command -v mpirun)" = "$FLOWER_ENV_PREFIX/bin/mpirun" ]; mpirun --version 2>&1 | head -1
[ "$(mpirun -np 2 echo mpi-ok | sort -u)" = "mpi-ok" ] && echo "mpirun -np 2: OK"
python3 -c 'import sys; print("python3", sys.version.split()[0])'
