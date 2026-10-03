"""Atomic-O adsorption energy on an fcc(111) slab with ASE's EMT potential.

Usage: python ads.py --metal Pt --a0 3.92 [--layers 4] [--size 3] [--site fcc]
Writes outputs.json ($FLOWER_OUTPUTS) and final.xyz (relaxed slab + O).

E_ads = E(slab+O) - E(slab) - 1/2 E(O2)   (all relaxed, EMT; a toy model, not DFT)
"""
import argparse
import json
import os

from ase import Atoms
from ase.build import add_adsorbate, fcc111
from ase.calculators.emt import EMT
from ase.constraints import FixAtoms
from ase.io import write
from ase.optimize import BFGS


def relaxed_energy(atoms, fmax=0.05, steps=300, label=""):
    atoms.calc = EMT()
    opt = BFGS(atoms, logfile=None)
    converged = opt.run(fmax=fmax, steps=steps)
    e = atoms.get_potential_energy()
    print(f"{label}: E = {e:.4f} eV after {opt.nsteps} BFGS steps (converged={converged})", flush=True)
    return e, bool(converged), opt.nsteps


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--metal", required=True)
    p.add_argument("--a0", type=float, required=True)
    p.add_argument("--layers", type=int, default=4)
    p.add_argument("--size", type=int, default=3)
    p.add_argument("--site", default="fcc")
    a = p.parse_args()

    o2 = Atoms("O2", positions=[(0, 0, 0), (0, 0, 1.21)], cell=(12, 12, 12), pbc=False)
    e_o2, _, _ = relaxed_energy(o2, label="O2")

    def slab():
        s = fcc111(a.metal, size=(a.size, a.size, a.layers), a=a.a0, vacuum=8.0)
        fixed = [atom.index for atom in s if atom.tag > a.layers // 2]  # tags count from the top layer = 1
        s.set_constraint(FixAtoms(indices=fixed))
        return s

    clean = slab()
    e_slab, conv1, n1 = relaxed_energy(clean, label=f"{a.metal}(111) {a.layers}L clean")
    with_o = slab()
    add_adsorbate(with_o, "O", height=1.2, position=a.site)
    e_slab_o, conv2, n2 = relaxed_energy(with_o, label=f"{a.metal}(111) {a.layers}L + O@{a.site}")
    e_ads = e_slab_o - e_slab - 0.5 * e_o2
    write("final.xyz", with_o)
    out = {"metal": a.metal, "a0": a.a0, "layers": a.layers, "site": a.site, "e_ads": round(e_ads, 4),
           "converged": conv1 and conv2, "bfgs_steps": n1 + n2,
           "summary": f"O on {a.metal}(111) {a.site}, {a.layers} layers: E_ads = {e_ads:.3f} eV"}
    with open(os.environ.get("FLOWER_OUTPUTS", "outputs.json"), "w") as fh:
        json.dump(out, fh, indent=2)
    print(out["summary"])


if __name__ == "__main__":
    main()
