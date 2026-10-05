# lmp and mpirun from this environment, the EAM pair styles, and a 2-rank MPI run: the energy per atom of a perfect
# fcc Lennard-Jones crystal (density 1.0, cutoff 2.5) must equal the same lattice sum done directly in Python.
set -e
[ "$(command -v lmp)" = "$FLOWER_ENV_PREFIX/bin/lmp" ]
[ "$(command -v mpirun)" = "$FLOWER_ENV_PREFIX/bin/mpirun" ]
lmp -h | grep -q "eam/alloy" && lmp -h | grep -q "eam/fs"
d=$(mktemp -d)
cat > "$d/in.check" <<'IN'
units lj
lattice fcc 1.0
region box block 0 8 0 8 0 8
create_box 1 box
create_atoms 1 box
mass 1 1.0
pair_style lj/cut 2.5
pair_coeff 1 1 1.0 1.0 2.5
thermo_modify norm no
run 0
variable e equal pe/atoms
print "E_ATOM ${e}"
IN
(cd "$d" && mpirun --oversubscribe -np 2 lmp -in in.check -log none -screen screen.txt >/dev/null)
e=$(awk '/^E_ATOM/{print $2}' "$d/screen.txt")
python3 - "$e" <<'PY'
import itertools, math, sys
a = (4 / 1.0) ** (1 / 3)                      # fcc at reduced density 1.0
basis = [(0, 0, 0), (.5, .5, 0), (.5, 0, .5), (0, .5, .5)]
s = 0.0
for i, j, k in itertools.product(range(-3, 4), repeat=3):
    for b in basis:
        r = a * math.dist((0, 0, 0), (i + b[0], j + b[1], k + b[2]))
        if 0 < r < 2.5:
            s += 4 * (r ** -12 - r ** -6)
ref, e = s / 2, float(sys.argv[1])
assert abs(e - ref) < 1e-9, (e, ref)
print("lammps OK: LJ fcc E/atom %.10f = lattice sum %.10f" % (e, ref))
PY
lmp -h | grep -m1 "Large-scale"
rm -rf "$d"
