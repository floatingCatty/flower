"""pw.x inputs for the metal-surface study (Singh-Miller & Marzari 2009): fcc bulk equations of state and slabs.

  python3 surf.py conv          FLOWER_INPUTS {"tables": tables.json, "pseudo": dir}: the convergence study
  python3 surf.py production    FLOWER_INPUTS {"tables", "pseudo", "settings": {...}, "a0": {el: A}}: all slabs

Each case is a directory cases/<name>/ with numbered pw.x inputs (run in order by case.sh) and its pseudopotential;
a slab input `*.slab.in` also gets its electrostatic potential averaged over planes (pp.x, average.x). Prints
{"items": [{"case", "kind", "n_inputs", "cost"}], ...}, most expensive first.

The paper's settings: PBE, 32/512 Ry, Marzari-Vanderbilt smearing 0.02 Ry, 16 A of vacuum, slabs built at the
computed bulk lattice constant with one atom per layer ((111) ABC, (100) and (110) AB stacking). The slab k-mesh is
given as kslab, the divisions along a side of length a0/sqrt(2) (the (111) and (100) cells); a longer side gets
proportionally fewer (the (110) cell, a0/sqrt(2) x a0: kslab x kslab/sqrt(2)).
"""
import json
import math
import os
import shutil
import sys
from pathlib import Path

from ase.build import bulk, fcc100, fcc110, fcc111
from ase.io import write

BUILD = {"111": fcc111, "100": fcc100, "110": fcc110}


def pw_input(path, atoms, el, pseudo, kpts, s, calc="scf", prefix="x"):
    data = {"control": {"calculation": calc, "prefix": prefix, "outdir": "./tmp", "pseudo_dir": "./",
                        "tprnfor": True, "forc_conv_thr": 1e-4, "etot_conv_thr": 1e-6, "disk_io": "low"},
            "system": {"ecutwfc": s["ecutwfc"], "ecutrho": s["ecutrho"], "occupations": "smearing",
                       "smearing": "mv", "degauss": s["degauss"]},
            "electrons": {"conv_thr": 1e-10 * len(atoms), "mixing_beta": 0.3,
                          "mixing_mode": "local-TF" if len(atoms) > 1 else "plain"},
            "ions": {"ion_dynamics": "bfgs"}}
    if calc == "scf":
        del data["ions"]
    with open(path, "w") as f:
        write(f, atoms, format="espresso-in", input_data=data, pseudopotentials={el: pseudo}, kpts=kpts)


def slab(el, face, n, a0, vacuum):
    return BUILD[face](el, size=(1, 1, n), a=a0, vacuum=vacuum / 2, periodic=True)


def slab_kpts(atoms, a0, kslab):
    side = a0 / math.sqrt(2)
    return tuple(max(1, round(kslab * side / v)) for v in atoms.cell.lengths()[:2]) + (1,)


def new_case(root, name, el, pseudo_dir, pseudo):
    d = root / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    shutil.copy(Path(pseudo_dir) / pseudo, d / pseudo)
    return d


def eos_case(root, name, el, a0, k, s, pseudo_dir, pseudo):
    d = new_case(root, name, el, pseudo_dir, pseudo)
    for i, f in enumerate([0.97, 0.98, 0.99, 1.0, 1.01, 1.02, 1.03]):
        pw_input(d / f"{i:02d}.bulk.in", bulk(el, "fcc", a=a0 * f), el, pseudo, (k, k, k), s)
    return {"case": name, "kind": "eos", "el": el, "k": k, "ecutwfc": s["ecutwfc"], "n_inputs": 7, "cost": 7}


def slab_case(root, name, el, face, Ns, a0, kslab, s, pseudo_dir, pseudo, calc="scf"):
    d = new_case(root, name, el, pseudo_dir, pseudo)
    cost = 0
    for N in Ns:
        at = slab(el, face, N, a0, s["vacuum"])
        k = slab_kpts(at, a0, kslab)
        pw_input(d / f"{N:02d}.slab.in", at, el, pseudo, k, s, calc=calc)
        cost += N ** 2 * k[0] * k[1] / 256 * (8 if calc == "relax" else 1)
    return {"case": name, "kind": "relax" if calc == "relax" else "series", "el": el, "face": face, "N": list(Ns),
            "a0": a0, "kslab": kslab, "vacuum": s["vacuum"], "n_inputs": len(Ns), "cost": round(cost, 1)}


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    T = json.loads(Path(I["tables"]).read_text())
    pdir = I["pseudo"]
    root = Path("cases")
    items = []
    if sys.argv[1] == "conv":
        s = dict(T["settings"])
        for el in T["pseudo"]:
            a0 = T["table1"][el]["a0"]
            for k in (12, 16, 20, 24):
                items.append(eos_case(root, f"eos-{el}-k{k}", el, a0, k, s, pdir, T["pseudo"][el]))
            hi = dict(s, ecutwfc=48, ecutrho=768)
            items.append(eos_case(root, f"eos-{el}-k16-ecut48", el, a0, 16, hi, pdir, T["pseudo"][el]))
        for el, face in (("Al", "111"), ("Pd", "100"), ("Pt", "110")):
            a0 = T["table1"][el]["a0"]
            for k in (8, 12, 16, 20, 24):
                items.append(slab_case(root, f"slab-{el}{face}-k{k}", el, face, (5, 6, 7), a0, k, s, pdir,
                                       T["pseudo"][el]))
            for vac in (12.0, 20.0):
                items.append(slab_case(root, f"slab-{el}{face}-k16-vac{vac:g}", el, face, (5, 6, 7), a0, 16,
                                       dict(s, vacuum=vac), pdir, T["pseudo"][el]))
    else:
        s, a0s = I["settings"], I["a0"]
        for el in T["pseudo"]:
            for face in ("111", "100", "110"):
                items.append(slab_case(root, f"series-{el}{face}", el, face, range(4, 14), a0s[el], s["kslab"], s,
                                       pdir, T["pseudo"][el]))
                items.append(slab_case(root, f"relax-{el}{face}-13", el, face, (13,), a0s[el], s["kslab"], s, pdir,
                                       T["pseudo"][el], calc="relax"))
        for N in (5, 7, 9, 11, 15):            # Fig. 1: Pd(100) relaxations against slab thickness
            items.append(slab_case(root, f"relax-Pd100-{N:02d}", "Pd", "100", (N,), a0s["Pd"], s["kslab"], s, pdir,
                                   T["pseudo"]["Pd"], calc="relax"))
    items.sort(key=lambda x: -x["cost"])
    print(json.dumps({"items": items, "n": len(items), "n_inputs": sum(x["n_inputs"] for x in items),
                      "total_cost": round(sum(x["cost"] for x in items), 1)}))


if __name__ == "__main__":
    main()
