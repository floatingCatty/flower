# python3 from this environment; ASE reads a CIF with symmetry operations (bcc Fe from its two-atom cell) and spglib
# finds its space group.
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
python3 - <<'PY'
import io, ase, spglib, numpy
from ase.io import read
cif = """data_x
_cell_length_a 2.87
_cell_length_b 2.87
_cell_length_c 2.87
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_symmetry_space_group_name_H-M 'I m -3 m'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
Fe1 Fe 0 0 0
"""
a = read(io.StringIO(cif), format="cif")
assert len(a) == 2, len(a)
sg = spglib.get_spacegroup((a.cell[:], a.get_scaled_positions(), a.numbers))
assert sg.startswith("Im-3m"), sg
print("ase", ase.__version__, "spglib", spglib.__version__, "numpy", numpy.__version__, "CIF -> bcc Fe,", sg, "OK")
PY
