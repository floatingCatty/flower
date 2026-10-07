"""This run's surfaces against Tables I-IV and the convergence statements of Singh-Miller & Marzari (2009).

  python3 analysis.py     FLOWER_INPUTS {"slab": [slab step outputs], "tables": tables.json,
                                         "a0": {el: A}, "B": {el: GPa}, "settings": {...}}  (from conv-report)

Surface energies by the paper's method (Fiorentini-Methfessel): the bulk energy per atom is the slope of the
unrelaxed E(N) over N = 6-13, sigma(N) = (E_N - N E_bulk) / 2; the relaxed 13-layer slab uses the same E_bulk.
Work function: vacuum potential (plane average, middle of the vacuum) minus the Fermi energy of the same slab.
Relaxations d_ij: the change of the spacing between layers i and j of the relaxed 13-layer slab against the bulk
spacing, in %, averaged over the two surfaces.

Claims: Table I a0 within 0.015 A and B within 5 %; Table III sigma (unrelaxed, relaxed) within 0.015 eV/atom and
J/m^2 within 0.03; Table IV within 0.05 eV; Table II d12, d23, d34 within 0.5 (percentage points); and the text:
the Pd surface energies converged from 6 layers (within 0.005 eV/atom of 13), the Pd(110) work function within
0.1 eV from 8 layers and 0.05 eV from 11, the Pd(100) relaxations converged at 13 layers (within 0.1 of 15).
Writes report.md, analysis.json, surfaces.png; prints the outputs JSON.
"""
import json
import math
import os
import re
from pathlib import Path

import numpy as np

RY = 13.605693122994
J_M2 = 16.0217663                                    # eV/A^2 -> J/m^2
SPACING = {"111": 1 / math.sqrt(3), "100": 1 / 2, "110": 1 / (2 * math.sqrt(2))}     # in units of a0
AREA = {"111": math.sqrt(3) / 4, "100": 1 / 2, "110": 1 / math.sqrt(2)}              # per surface atom, a0^2


def final_z(path):
    """z of the atoms after a pw.x relaxation (the last ATOMIC_POSITIONS block, angstrom)."""
    blocks = re.findall(r"ATOMIC_POSITIONS \(angstrom\)\n(.*?)(?:\n\s*\n|End final)", Path(path).read_text(), re.S)
    return sorted(float(l.split()[3]) for l in blocks[-1].strip().splitlines())


def relaxations(z, d0):
    d = np.diff(z)
    n = len(d)
    return [float(100 * ((d[i] + d[n - 1 - i]) / 2 - d0) / d0) for i in range(3)]


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    T = json.loads(Path(I["tables"]).read_text())
    bulk = {k: I[k] for k in ("a0", "B", "settings")}
    cases = {c["case"]: c for c in I["slab"] if c}
    claims, rows, conv = [], {}, {}

    def claim(name, paper, ours, ok, group):
        claims.append({"claim": name, "paper": paper, "this_run": ours, "ok": bool(ok), "group": group})

    for el in T["table1"]:
        p = T["table1"][el]
        claim(f"Table I a0 {el} (A)", p["a0"], round(bulk["a0"][el], 4), abs(bulk["a0"][el] - p["a0"]) < 0.015, "I")
        claim(f"Table I B {el} (GPa)", p["B"], round(bulk["B"][el], 1), abs(bulk["B"][el] / p["B"] - 1) < 0.05, "I")
    for key, P in T["faces"].items():
        el, face = P["el"], P["face"]
        a0 = bulk["a0"][el]
        ser, rel = cases.get(f"series-{key}"), cases.get(f"relax-{key}-13")
        if not ser or not rel:
            claim(f"{key}: calculations complete", "-", "missing", False, "III")
            continue
        N = np.array([r["nat"] for r in ser["runs"]])
        E = np.array([r["E_Ry"] for r in ser["runs"]]) * RY
        phi = np.array([r["Vvac_eV"] - r["Ef_eV"] for r in ser["runs"]])
        m = N >= 6
        eb = np.polyfit(N[m], E[m], 1)[0]
        sig = (E - N * eb) / 2
        r13 = rel["runs"][0]
        sr = (r13["E_Ry"] * RY - 13 * eb) / 2
        phir = r13["Vvac_eV"] - r13["Ef_eV"]
        su = float(sig[N == 13][0])
        area = AREA[face] * a0 ** 2
        z = final_z(Path(rel["local_dir"]) / "out" / "13.slab.out")
        d = relaxations(z, SPACING[face] * a0)
        rows[key] = {"sigma_u": su, "sigma_r": float(sr), "sigma_r_Jm2": float(sr / area * J_M2), "phi_r": float(phir),
                     "phi_u": float(phi[N == 13][0]), "d": d, "N": N.tolist(), "sigma_N": sig.tolist(),
                     "phi_N": phi.tolist(), "E_bulk": float(eb), "bfgs_steps": r13["bfgs_steps"]}
        R = rows[key]
        claim(f"Table III {el}({face}) sigma unrelaxed (eV/atom)", P["sigma_u"], round(su, 3),
              abs(su - P["sigma_u"]) < 0.015, "III")
        claim(f"Table III {el}({face}) sigma relaxed (eV/atom)", P["sigma_r"], round(R["sigma_r"], 3),
              abs(R["sigma_r"] - P["sigma_r"]) < 0.015, "III")
        claim(f"Table III {el}({face}) sigma relaxed (J/m^2)", P["sigma_r_Jm2"], round(R["sigma_r_Jm2"], 3),
              abs(R["sigma_r_Jm2"] - P["sigma_r_Jm2"]) < 0.03, "III")
        claim(f"Table IV {el}({face}) work function (eV), relaxed", P["phi"], round(phir, 3),
              abs(phir - P["phi"]) < 0.05, "IV")
        for i, (ours, paper) in enumerate(zip(d, P["d"])):
            claim(f"Table II {el}({face}) d{i + 1}{i + 2} (%)", paper, round(ours, 2), abs(ours - paper) < 0.5, "II")
        conv[key] = R
    # the text's convergence statements, on Pd
    for f in ("111", "100", "110"):
        R = conv.get(f"Pd{f}")
        if R:
            N, s = np.array(R["N"]), np.array(R["sigma_N"])
            dev = float(np.max(np.abs(s[N >= 6] - s[N == 13][0])))
            claim(f"Pd({f}): surface energy converged from 6 layers (unrelaxed, |sigma(N) - sigma(13)|, eV/atom)",
                  "< 0.005", round(dev, 4), dev < 0.005, "text")
    R = conv.get("Pd110")
    if R:
        N, p = np.array(R["N"]), np.array(R["phi_N"])
        d8 = float(np.max(np.abs(p[N >= 8] - p[N == 13][0])))
        d11 = float(np.max(np.abs(p[N >= 11] - p[N == 13][0])))
        claim("Pd(110): work function within 0.1 eV from 8 layers and 0.05 eV from 11 (unrelaxed, vs 13)",
              "0.1 / 0.05", f"{d8:.3f} / {d11:.3f}", d8 < 0.1 and d11 < 0.05, "text")
    series = {}
    for N in (5, 7, 9, 11, 13, 15):
        c = cases.get(f"relax-Pd100-{N:02d}") or (cases.get("relax-Pd100-13") if N == 13 else None)
        if c:
            series[N] = relaxations(final_z(Path(c["local_dir"]) / "out" / f"{N:02d}.slab.out"),
                                    SPACING["100"] * bulk["a0"]["Pd"])
    if 13 in series and 15 in series:
        dev = max(abs(a - b) for a, b in zip(series[13], series[15]))
        claim("Pd(100): relaxations converged at 13 layers (|d_ij(13) - d_ij(15)|, %)", "< 0.1", round(dev, 3),
              dev < 0.1, "text")

    n_ok = sum(c["ok"] for c in claims)
    L = [f"# Metal surfaces (Singh-Miller & Marzari, PRB 80, 235407 (2009)): {n_ok} of {len(claims)} claims", "",
         "Settings: " + ", ".join(f"{k} {v}" for k, v in bulk["settings"].items()), ""]
    for g, title in (("I", "Table I: bulk"), ("III", "Table III: surface energies, 13 layers"),
                     ("IV", "Table IV: work functions, 13 layers"), ("II", "Table II: relaxations, 13 layers"),
                     ("text", "Convergence statements")):
        sel = [c for c in claims if c["group"] == g]
        L += [f"## {title}: {sum(c['ok'] for c in sel)} of {len(sel)}", "", "| claim | paper | this run | |",
              "|---|---|---|---|"]
        L += [f"| {c['claim']} | {c['paper']} | {c['this_run']} | {'✓' if c['ok'] else '✗'} |" for c in sel] + [""]
    L += ["## Work functions of the unrelaxed 13-layer slabs", "",
          ", ".join(f"{k} {R['phi_u']:.3f}" for k, R in rows.items()), "",
          "## Pd(100) relaxations against slab thickness (d12, d23, d34 %)", ""]
    L += [f"- N = {N}: " + ", ".join("%+.2f" % x for x in v) for N, v in sorted(series.items())]
    Path("report.md").write_text("\n".join(L) + "\n")
    json.dump({"claims": claims, "faces": rows, "pd100_relaxations": series}, open("analysis.json", "w"), indent=1)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(10, 4))
    for key, R in rows.items():
        N = np.array(R["N"])
        ax[0].plot(N, np.array(R["sigma_N"]) - R["sigma_u"], ".-", ms=4, lw=1, label=key)
        ax[1].plot(N, np.array(R["phi_N"]) - R["phi_u"], ".-", ms=4, lw=1, label=key)
    ax[0].set_ylabel("sigma(N) - sigma(13) (eV/atom), unrelaxed"); ax[1].set_ylabel("phi(N) - phi(13) (eV), unrelaxed")
    for a in ax:
        a.set_xlabel("layers N"); a.axhline(0, color="0.6", lw=0.8)
    ax[0].set_ylim(-0.03, 0.03); ax[1].set_ylim(-0.3, 0.3)
    ax[1].legend(fontsize=7, ncol=2)
    fig.tight_layout(); fig.savefig("surfaces.png", dpi=130)
    by = {g: [sum(c["ok"] for c in claims if c["group"] == g), sum(c["group"] == g for c in claims)]
          for g in ("I", "II", "III", "IV", "text")}
    print(json.dumps({"n_claims": len(claims), "n_reproduced": n_ok, "by_table": by}))


if __name__ == "__main__":
    main()
