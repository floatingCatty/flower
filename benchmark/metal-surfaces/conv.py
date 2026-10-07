"""The convergence study turned into production settings, for a person to sign off (the `settings` gate).

  python3 conv.py     FLOWER_INPUTS {"conv": [conv step outputs], "cases": [conv-in items], "tables": tables.json,
                                     "feedback": "..."}

Bulk: a Murnaghan fit of each equation of state gives a0 and B; k_bulk is the smallest k whose a0 and B are within
0.002 A and 1 % of k = 24 for all four metals. Slabs: Fiorentini-Methfessel surface energy of the 7-layer slab
(the bulk energy per atom from the slope of E(N), N = 5-7) and the work function (vacuum potential - Fermi energy);
kslab is the smallest in-plane mesh within 0.005 eV/atom and 0.02 eV of kslab = 24 on all three faces, and the
vacuum the smallest within 0.002 eV/atom and 0.01 eV of 20 A. The cutoff is the paper's 32/512 Ry; the study shows
what 48/768 Ry changes.

A reviewer's text (`${feedback}`, from a rejection of the gate) may override the choice: e.g. "kslab=20 vacuum=20"
(k_bulk must be one of the meshes computed). Writes report.md; prints the outputs JSON: settings, a0 and B per
metal at k_bulk, the production estimate, and `proposal`, the gate's message.
"""
import json
import math
import os
import re
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import curve_fit

RY, BOHR = 13.605693122994, 0.529177210903
GPA = 160.21766208                                  # eV/A^3 -> GPa


def murnaghan(V, E0, V0, B, Bp):
    return E0 + B * V / Bp * ((V0 / V) ** Bp / (Bp - 1) + 1) - B * V0 / (Bp - 1)


def eos(runs):
    V = np.array([r["vol_bohr3"] / r["nat"] for r in runs]) * BOHR ** 3
    E = np.array([r["E_Ry"] / r["nat"] for r in runs]) * RY
    i = int(np.argmin(E))
    p, _ = curve_fit(murnaghan, V, E, p0=(E[i], V[i], 0.5, 5.0), maxfev=20000)
    return {"a0": float((4 * p[1]) ** (1 / 3)), "B": float(p[2] * GPA), "E0": float(p[0]), "Bp": float(p[3])}


def slab(runs):
    N = np.array([r["nat"] for r in runs])
    E = np.array([r["E_Ry"] for r in runs]) * RY
    slope, icpt = np.polyfit(N, E, 1)
    top = int(np.argmax(N))
    phi = [r["Vvac_eV"] - r["Ef_eV"] for r in runs]
    return {"sigma": float((E[top] - N[top] * slope) / 2), "phi": float(phi[top]), "phi_all": phi,
            "ebulk": float(slope), "N": N.tolist()}


def seconds(w):
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?$", w or "")
    return sum(float(x or 0) * f for x, f in zip(m.groups(), (3600, 60, 1))) if m else float("nan")


def cost(N, kx, ky, relax=False):                    # the same unit as surf.py's cost
    return N ** 2 * kx * ky / 256 * (8 if relax else 1)


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    T = json.loads(Path(I["tables"]).read_text())
    rows = {c["case"]: c["runs"] for c in I["conv"] if c}
    metals = list(T["pseudo"])
    B, S = {}, {}
    for name, runs in rows.items():
        m = re.match(r"eos-(\w\w)-k(\d+)(-ecut48)?$", name)
        if m:
            B[(m.group(1), int(m.group(2)), 48 if m.group(3) else 32)] = eos(runs)
        m = re.match(r"slab-(\w\w\d{3})-k(\d+)(?:-vac(\d+))?$", name)
        if m:
            S[(m.group(1), int(m.group(2)), float(m.group(3) or 16))] = slab(runs)
    faces = sorted({k[0] for k in S})
    kb_all = sorted({k[1] for k in B})
    ks_all = sorted({k[1] for k in S})
    missing = [(el, k) for el in metals for k in kb_all if (el, k, 32) not in B] + \
              [(f, k) for f in faces for k in ks_all if (f, k, 16.0) not in S]
    if missing:
        sys.exit(f"convergence rows missing (failed?): {missing}")
    kbmax, ksmax = max(kb_all), max(ks_all)
    bulk_ok = {k: all(abs(B[(el, k, 32)]["a0"] - B[(el, kbmax, 32)]["a0"]) < 0.002 and
                      abs(B[(el, k, 32)]["B"] / B[(el, kbmax, 32)]["B"] - 1) < 0.01 for el in metals) for k in kb_all}
    slab_dev = {k: max(max(abs(S[(f, k, 16.0)]["sigma"] - S[(f, ksmax, 16.0)]["sigma"]),
                           abs(S[(f, k, 16.0)]["phi"] - S[(f, ksmax, 16.0)]["phi"]) / 4) for f in faces) for k in ks_all}
    vacs = sorted({k[2] for k in S})
    vac_ok = {v: all(abs(S[(f, 16, v)]["phi"] - S[(f, 16, max(vacs))]["phi"]) < 0.01 and
                     abs(S[(f, 16, v)]["sigma"] - S[(f, 16, max(vacs))]["sigma"]) < 0.002 for f in faces) for v in vacs}
    s = {"ecutwfc": 32, "ecutrho": 512, "degauss": 0.02,
         "k_bulk": next(k for k in kb_all if bulk_ok[k]),
         "kslab": next(k for k in ks_all if slab_dev[k] < 0.005),
         "vacuum": next(v for v in vacs if vac_ok[v])}
    auto = dict(s)
    fb = (I.get("feedback") or "").strip()
    for key, val in re.findall(r"(\w+)\s*=\s*([\d.]+)", fb):
        if key not in ("kslab", "k_bulk", "vacuum"):
            sys.exit(f"feedback sets {key!r}; only kslab, k_bulk and vacuum can be overridden")
        s[key] = float(val) if key == "vacuum" else int(val)
    if s["k_bulk"] not in kb_all:
        sys.exit(f"k_bulk = {s['k_bulk']} was not computed; choose one of {kb_all}")
    a0 = {el: round(B[(el, s["k_bulk"], 32)]["a0"], 4) for el in metals}
    Bm = {el: round(B[(el, s["k_bulk"], 32)]["B"], 1) for el in metals}

    # the production estimate: seconds per cost unit (surf.py's) measured on the slab rows, 4 cores per job
    cases = {c["case"]: c for c in I["cases"]}
    rate = np.median([sum(seconds(r["wall"]) for r in runs) / cases[n]["cost"]
                      for n, runs in rows.items() if n.startswith("slab-")])
    n = s["kslab"]
    k110 = (n, max(1, round(n / math.sqrt(2))))
    units = 0.0
    for f in ("111", "100", "110"):
        kx, ky = k110 if f == "110" else (n, n)
        units += 4 * (sum(cost(N, kx, ky) for N in range(4, 14)) + cost(13, kx, ky, True))
    units += sum(cost(N, n, n, True) for N in (5, 7, 9, 11, 15))
    core_h = units * rate * 4 / 3600
    est = {"jobs": 29, "core_hours": round(core_h, 1), "wall_hours_32_cores": round(core_h / 32, 1)}

    L = ["# Convergence study (Singh-Miller & Marzari 2009 settings: PBE, 32/512 Ry, MV 0.02 Ry)", "",
         "## Bulk: a0 (A) and B (GPa) from Murnaghan fits, against the k-mesh (k x k x k)", "",
         "| metal | " + " | ".join(f"k={k}" for k in kb_all) + " | 48/768 Ry, k=16 | paper |",
         "|---|" + "---|" * (len(kb_all) + 2)]
    for el in metals:
        L.append(f"| {el} | " + " | ".join("%.4f / %.1f" % (B[(el, k, 32)]["a0"], B[(el, k, 32)]["B"]) for k in kb_all)
                 + " | %.4f / %.1f | %.2f / %d |" % (B[(el, 16, 48)]["a0"], B[(el, 16, 48)]["B"],
                                                     T["table1"][el]["a0"], T["table1"][el]["B"]))
    L += ["", "## Slabs (5-7 layers, paper a0): surface energy of the 7-layer slab (eV/atom) and work function (eV)",
          "", "| in-plane k | " + " | ".join(faces) + " | max dev. from k=%d |" % ksmax, "|---|" + "---|" * (len(faces) + 1)]
    for k in ks_all:
        L.append(f"| {k} | " + " | ".join("%.4f / %.3f" % (S[(f, k, 16.0)]["sigma"], S[(f, k, 16.0)]["phi"])
                                          for f in faces) + " | %.4f |" % slab_dev[k])
    L += ["", "| vacuum (A), k=16 | " + " | ".join(faces) + " |", "|---|" + "---|" * len(faces)]
    for v in vacs:
        L.append(f"| {v:g} | " + " | ".join("%.4f / %.3f" % (S[(f, 16, v)]["sigma"], S[(f, 16, v)]["phi"])
                                            for f in faces) + " |")
    L += ["", "(max dev.: the largest of |d sigma| and |d phi|/4 over the faces; the criterion is 0.005 eV/atom "
          "and 0.02 eV)"]
    Path("report.md").write_text("\n".join(L) + "\n")

    over = {k: (auto[k], s[k]) for k in s if s[k] != auto[k]}
    ks = s["kslab"]
    if all((f, ks, 16.0) in S for f in faces):
        kdev = "sigma within %.4f eV/atom and phi within %.3f eV of k=%d" % (
            max(abs(S[(f, ks, 16.0)]["sigma"] - S[(f, ksmax, 16.0)]["sigma"]) for f in faces),
            max(abs(S[(f, ks, 16.0)]["phi"] - S[(f, ksmax, 16.0)]["phi"]) for f in faces), ksmax)
    else:
        kdev = "not in the study"
    if ks == ksmax and len(ks_all) > 1:           # nothing smaller met the criterion: say how far the next one is
        prev = ks_all[-2]
        worst = max(faces, key=lambda f: abs(S[(f, prev, 16.0)]["sigma"] - S[(f, ksmax, 16.0)]["sigma"]))
        kdev = (f"THE LARGEST MESH COMPUTED, convergence not shown: at k={prev} {worst} is "
                f"{S[(worst, prev, 16.0)]['sigma'] - S[(worst, ksmax, 16.0)]['sigma']:+.4f} eV/atom off "
                f"(the criterion: 0.005)")
    ecut_da = max(abs(B[(el, 16, 48)]["a0"] - B[(el, 16, 32)]["a0"]) for el in metals)
    ecut_dB = 100 * max(abs(B[(el, 16, 48)]["B"] / B[(el, 16, 32)]["B"] - 1) for el in metals)
    prop = ["Production settings proposed by the convergence study" + (" with your overrides" if over else "") + ":",
            f"- cutoff {s['ecutwfc']}/{s['ecutrho']} Ry (the paper's); at 48/768 Ry a0 moves by at most "
            f"{ecut_da:.4f} A and B by {ecut_dB:.1f} %",
            f"- bulk k-mesh {s['k_bulk']}^3: a0 = " + ", ".join(f"{el} {a0[el]:.3f}" for el in metals)
            + " A (paper " + ", ".join(f"{T['table1'][el]['a0']:.2f}" for el in metals) + ")",
            f"- slab k-mesh {ks} x {ks} ((110): {k110[1]} x {k110[0]}): {kdev}",
            f"- vacuum {s['vacuum']:g} A (the paper's: 16 A)",
            "- production: 12 faces x 4-13 layers unrelaxed + 13-layer relaxations + Pd(100) relaxed at 5-15 layers: "
            f"{est['jobs']} jobs, about {est['core_hours']} core-hours, {est['wall_hours_32_cores']} h on 32 cores"]
    if over:
        prop.append("- overridden: " + ", ".join(f"{k} {a} -> {b}" for k, (a, b) in over.items()))
    elif fb:
        prop.append(f"- your text {fb!r} set nothing (write overrides as key=value)")
    print(json.dumps({"settings": s, "auto": auto, "a0": a0, "B": Bm, "estimate": est, "proposal": "\n".join(prop)}))


if __name__ == "__main__":
    main()
