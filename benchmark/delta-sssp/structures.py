"""pw.x inputs for the Delta test: every elemental crystal at 94, 96, ..., 106 % of the WIEN2k equilibrium volume.

FLOWER_INPUTS {"wien2k": path to delta/WIEN2k.txt (the Delta package beside it), "sssp_json": path (the pseudopotentials in sssp/ beside it),
               "kspacing": k-point spacing in 1/A (2pi included), "degauss": Marzari-Vanderbilt smearing in Ry,
               "elements": optional list, "ecut_scale": optional factor on the recommended cutoffs (default 1)}
Writes inputs/<El>/<El>_<pct>.in and the pseudopotential beside them; prints {"items": [...]} for the fan-out.

Structures: the package's primCIFs reduced to primitive cells with spglib (some are conventional), except O, Cr
and Mn, whose antiferromagnetic order needs the conventional cells (CIFs). Magnetism as the Delta protocol prescribes: Fe, Co, Ni ferromagnetic; Cr, Mn
antiferromagnetic (Cr: corner vs centre of the cubic cell; Mn: alternating (001) planes of the fcc cell); O
antiferromagnetic between the O2 molecules. The cell is scaled isotropically, fractional positions fixed.
"""
import json
import math
import os
import shutil
from pathlib import Path

import numpy as np
import spglib
from ase import Atoms
from ase.data import atomic_masses, atomic_numbers
from ase.geometry import get_distances
from ase.io import read

SCALES = [0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.06]
FM = {"Fe", "Co", "Ni"}
AFM = {"O", "Cr", "Mn"}


def wien2k(path):
    out = {}
    for ln in open(path):
        p = ln.split()
        if p and not ln.startswith("#"):
            out[p[0]] = (float(p[1]), float(p[2]), float(p[3]))
    return out


def spins(el, atoms):
    """+1 / -1 per atom (0 if not magnetic)."""
    n = len(atoms)
    if el in FM:
        return [1] * n
    if el == "Cr":       # bcc in its simple-cubic two-atom cell
        return [1 if np.allclose(p, 0, atol=1e-3) else -1 for p in atoms.get_scaled_positions() % 1.0]
    if el == "Mn":       # fcc four-atom cell, (001) planes alternate
        return [1 if abs(p[2] % 1.0) < 1e-3 or abs(p[2] % 1.0 - 1) < 1e-3 else -1 for p in atoms.get_scaled_positions()]
    if el == "O":        # the two O2 molecules point opposite ways
        _, d = get_distances(atoms.positions, cell=atoms.cell, pbc=True)
        first = [0] + [j for j in range(1, n) if d[0, j] < 1.5]
        return [1 if i in first else -1 for i in range(n)]
    return [0] * n


OBSOLETE = {"Cmca": "Cmce", "Cmma": "Cmme", "Abm2": "Aem2", "Aba2": "Aea2", "Ccca": "Ccce"}   # e-glide renaming


def read_cif(path):
    """ASE's reader; space-group symbols renamed in 2002 (e.g. Cmca -> Cmce) are translated first."""
    import io
    import re
    text = open(path).read()
    m = re.search(r"_symmetry_space_group_name_H-M\s+'([^']*)'", text)
    if m and m.group(1).replace(" ", "") in OBSOLETE:
        text = text.replace(m.group(0), "_symmetry_space_group_name_H-M '%s'" % OBSOLETE[m.group(1).replace(" ", "")])
    return read(io.StringIO(text), format="cif")


def kmesh(cell, spacing):
    rec = 2 * math.pi * np.linalg.inv(np.asarray(cell)).T
    return [max(1, math.ceil(np.linalg.norm(b) / spacing)) for b in rec]


def pw_input(el, atoms, sp, pseudo, lib, scale_cell, kpts, degauss, ecut_scale=1.0):
    magnetic = any(sp)
    labels = [el + ("1" if s > 0 else "2") if el in AFM else el for s in sp]
    species = sorted(set(labels))
    mass = atomic_masses[atomic_numbers[el]]
    nat = len(atoms)
    sysl = ["  ibrav = 0", "  nat = %d" % nat, "  ntyp = %d" % len(species),
            "  ecutwfc = %.1f" % (lib["cutoff_wfc"] * ecut_scale), "  ecutrho = %.1f" % (lib["cutoff_rho"] * ecut_scale),
            "  occupations = 'smearing'", "  smearing = 'mv'", "  degauss = %g" % degauss]
    if magnetic:
        sysl.append("  nspin = 2")
        for i, s in enumerate(species, 1):
            sysl.append("  starting_magnetization(%d) = %s" % (i, "-0.5" if s.endswith("2") and el in AFM else "0.5"))
    cell = np.asarray(atoms.cell) * scale_cell
    lines = ["&control", "  calculation = 'scf'", "  prefix = 'x'", "  outdir = './tmp'", "  pseudo_dir = './'",
             "  disk_io = 'none'", "/", "&system"] + sysl + ["/", "&electrons",
             "  conv_thr = %.1e" % (1e-10 * nat), "  mixing_beta = 0.3", "  electron_maxstep = 300", "/",
             "ATOMIC_SPECIES"]
    lines += ["  %s %.4f %s" % (s, mass, pseudo) for s in species]
    lines.append("CELL_PARAMETERS angstrom")
    lines += ["  %.10f %.10f %.10f" % tuple(v) for v in cell]
    lines.append("ATOMIC_POSITIONS crystal")
    lines += ["  %s %.10f %.10f %.10f" % (lab, *p) for lab, p in zip(labels, atoms.get_scaled_positions())]
    lines += ["K_POINTS automatic", "  %d %d %d 0 0 0" % tuple(kpts)]
    return "\n".join(lines) + "\n"


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    delta = Path(I["wien2k"]).parent
    ref = wien2k(I["wien2k"])
    lib = json.loads(Path(I["sssp_json"]).read_text())
    elements = I.get("elements") or sorted(ref)
    items = []
    for el in elements:
        atoms = read_cif(delta / ("CIFs" if el in AFM else "primCIFs") / (el + ".cif"))
        assert set(atoms.get_chemical_symbols()) == {el}, el
        if el not in AFM:   # some "primCIFs" are conventional cells (fcc with 4 atoms): reduce them
            lat, pos, num = spglib.find_primitive((atoms.cell[:], atoms.get_scaled_positions(), atoms.numbers),
                                                  symprec=1e-3)
            if len(num) < len(atoms):
                atoms = Atoms(numbers=num, cell=lat, scaled_positions=pos, pbc=True)
        nat = len(atoms)
        v0 = ref[el][0]                                        # A^3 per atom, WIEN2k
        base = (v0 * nat / atoms.get_volume()) ** (1 / 3)      # the cell at the WIEN2k volume
        kpts = kmesh(np.asarray(atoms.cell) * base, I["kspacing"])
        sp = spins(el, atoms)
        if el in AFM and (sum(sp) != 0 or len(set(sp)) != 2):
            raise SystemExit("%s: no antiferromagnetic arrangement found: %s" % (el, sp))
        d = Path("inputs") / el
        d.mkdir(parents=True, exist_ok=True)
        pseudo = lib[el]["filename"]
        shutil.copy(Path(I["sssp_json"]).parent / "sssp" / pseudo, d / pseudo)
        for s in SCALES:
            (d / ("%s_%03d.in" % (el, round(100 * s)))).write_text(
                pw_input(el, atoms, sp, pseudo, lib[el], base * s ** (1 / 3), kpts, I["degauss"],
                         float(I.get("ecut_scale") or 1.0)))
        items.append({"el": el, "nat": nat, "kpts": kpts, "ecutwfc": lib[el]["cutoff_wfc"] * float(I.get("ecut_scale") or 1.0),
                      "magnetic": "FM" if el in FM else "AFM" if el in AFM else "",
                      "cost": nat ** 3 * kpts[0] * kpts[1] * kpts[2] * lib[el]["cutoff_wfc"] ** 1.5})
    items.sort(key=lambda x: -x["cost"])                       # the most expensive first
    return {"items": items, "n": len(items)}


if __name__ == "__main__":
    print(json.dumps(main()))
