"""Equation-of-state fit for an fcc metal with ASE's EMT potential.

Usage: python eos.py METAL  -> writes outputs.json ($FF_OUTPUTS) with the fitted lattice constant.
"""
import json
import os
import sys

import numpy as np
from ase.build import bulk
from ase.calculators.emt import EMT
from ase.eos import EquationOfState
from ase.units import GPa

GUESS = {"Ag": 4.09, "Al": 4.05, "Au": 4.08, "Cu": 3.61, "Ni": 3.52, "Pd": 3.89, "Pt": 3.92}


def main(metal: str) -> dict:
    a_guess = GUESS[metal]
    volumes, energies = [], []
    for a in np.linspace(0.95 * a_guess, 1.05 * a_guess, 9):
        atoms = bulk(metal, "fcc", a=a)
        atoms.calc = EMT()
        volumes.append(atoms.get_volume())
        energies.append(atoms.get_potential_energy())
        print(f"a={a:.4f} Å  E={energies[-1]:.5f} eV", flush=True)
    eos = EquationOfState(volumes, energies)
    v0, e0, b = eos.fit()
    a0 = (4 * v0) ** (1 / 3)  # fcc primitive cell holds 1 atom: V = a^3/4
    out = {"metal": metal, "a0": round(float(a0), 4), "bulk_modulus_GPa": round(float(b / GPa), 1),
           "e_bulk_per_atom": round(float(e0), 5),
           "summary": f"{metal}: a0 = {a0:.3f} Å, B = {b / GPa:.0f} GPa (EMT)"}
    return out


if __name__ == "__main__":
    result = main(sys.argv[1])
    with open(os.environ.get("FF_OUTPUTS", "outputs.json"), "w") as fh:
        json.dump(result, fh, indent=2)
    print(result["summary"])
