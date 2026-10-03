"""Quasi-harmonic thermodynamics of Si from the QE results, and the comparison with the paper
(Rignanese, Michenaud & Gonze, PRB 53, 4488, 1996). Reads $FLOWER_INPUTS: {"eos": [...], "phonons": [...]},
each item the outputs of a QE step (with `local_dir`). Writes qha.json, report.md and figures.

F(V, T) = E_static(V) [Birch-Murnaghan fit] + F_vib(V, T) [harmonic phonons, fitted in V at each T].
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
from scipy.optimize import curve_fit, minimize_scalar

RY_J = 2.1798723611e-18
NA = 6.02214076e23
KB_RY = 1.380649e-23 / RY_J            # Ry / K
CM_RY = 1 / 109737.31568               # 1 cm^-1 in Ry
RY_BOHR3_GPA = 14710.507848            # 1 Ry/bohr^3 in GPa
BOHR3_M3 = (0.529177210903e-10) ** 3

PAPER = {"a0": 10.18, "a0_precise": 10.1894, "a0_zp": 10.1974, "B_static_Mbar": 1.0387, "B_zp_Mbar": 1.0292,
         "TA_X_a0": 140.46, "TA_L_a0": 108.626, "TA_X_exp_a": 147.37, "TA_L_exp_a": 112.86,
         "g_TA_X_a0": -2.295, "g_TA_L_a0": -1.814, "g_TA_X_exp_a": -1.782, "g_TA_L_exp_a": -1.451,
         "S298": 19.3, "dH298_kJ": 3.285, "nte_range_K": [20, 120], "cp_cross_K": 85.0}
EXPT = {"a0": 10.26, "TA_X": 149.77, "TA_L": 114.41, "g_TA_X": -1.4, "g_TA_L": -1.3, "S298": 18.81,
        "dH298_kJ": 3.217}


def bm(V, E0, V0, B0, Bp):
    x = (V0 / V) ** (2 / 3)
    return E0 + 9 * V0 * B0 / 16 * ((x - 1) ** 3 * Bp + (x - 1) ** 2 * (6 - 4 * x))


def fvib(w_ry, g, T):
    """Harmonic free energy per cell (Ry) from frequencies w (Ry) and DOS weights g (summing to 6 modes)."""
    zp = 0.5 * np.sum(g * w_ry)
    if T <= 0:
        return zp
    x = w_ry / (KB_RY * T)
    return zp + KB_RY * T * np.sum(g * np.log1p(-np.exp(-np.minimum(x, 700))))


def svib(w_ry, g, T):
    x = w_ry / (KB_RY * T)
    ex = np.exp(-np.minimum(x, 700))
    return KB_RY * np.sum(g * (x * ex / (1 - ex) - np.log1p(-ex)))   # Ry/K per cell


def load(items, name="result.json"):
    out = []
    for it in items or []:
        d = Path(it.get("local_dir") or it.get("dir") or "")
        if (d / name).is_file():
            r = json.loads((d / name).read_text())
            r["_dir"] = str(d)
            out.append(r)
    return sorted(out, key=lambda r: r["a"])


def main() -> dict:
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    eos, ph = load(I.get("eos")), load(I.get("phonons"))
    a = np.array([r["a"] for r in eos])
    E = np.array([r["energy_ry"] for r in eos])
    V = a ** 3 / 4
    p0 = [E.min(), V[np.argmin(E)], 0.006, 4.0]
    (E0, V0, B0, Bp), _ = curve_fit(bm, V, E, p0=p0, maxfev=20000)
    study = []
    for it in I.get("ecut_study") or []:
        d = Path(it.get("local_dir") or "")
        if (d / "eos.json").is_file():
            r = json.loads((d / "eos.json").read_text())
            vv, ee = np.array(r["a"]) ** 3 / 4, np.array(r["energy_ry"])
            pp, _ = curve_fit(bm, vv, ee, p0=[ee.min(), vv[np.argmin(ee)], 0.006, 4.0], maxfev=20000)
            study.append({"ecut": r["ecut_ry"], "a0": (4 * pp[1]) ** (1 / 3), "B0_Mbar": pp[2] * RY_BOHR3_GPA / 100,
                          "Bprime": pp[3]})
    out = {"ecut_study": sorted(study, key=lambda r: r["ecut"]), "static": {"a0": (4 * V0) ** (1 / 3), "B0_GPa": B0 * RY_BOHR3_GPA, "B0_Mbar": B0 * RY_BOHR3_GPA / 100,
                      "Bprime": Bp, "fit_rms_mRy": float(1000 * np.sqrt(np.mean((bm(V, E0, V0, B0, Bp) - E) ** 2)))}}
    if not ph:
        return out
    # phonon DOS per volume
    Vp = np.array([r["a"] ** 3 / 4 for r in ph])
    dos = []
    for r in ph:
        d = np.loadtxt(Path(r["_dir"]) / "phdos.dat", comments="#")
        w, g = d[:, 0], d[:, 1]
        keep = w > 0.5
        w, g = w[keep], g[keep]
        g = g * 6 / np.sum(g)                    # normalise to 3 nat = 6 modes per cell
        dos.append((w * CM_RY, g))
    Ts = np.arange(1.0, 801.0, 1.0)
    Vgrid = np.linspace(Vp.min(), Vp.max(), 4001)

    def equilibrium(T, P_gpa=0.0, deg=2):
        fv = np.array([fvib(w, g, T) for w, g in dos])
        c = np.polyfit(Vp, fv, deg)            # the paper: second order in V
        G = bm(Vgrid, E0, V0, B0, Bp) + np.polyval(c, Vgrid) + P_gpa / RY_BOHR3_GPA * Vgrid
        i = int(np.argmin(G))
        r = minimize_scalar(lambda v: bm(v, E0, V0, B0, Bp) + np.polyval(c, v) + P_gpa / RY_BOHR3_GPA * v,
                            bounds=(Vgrid[max(0, i - 2)], Vgrid[min(len(Vgrid) - 1, i + 2)]), method="bounded")
        return r.x, c

    def props(P_gpa=0.0):
        Veq = np.array([equilibrium(T, P_gpa)[0] for T in Ts])
        alpha = np.gradient(Veq, Ts) / Veq / 3              # linear expansion coefficient (1/K)
        S = []
        for T, v in zip(Ts, Veq):
            sv = [svib(w, g, T) for w, g in dos]
            S.append(np.polyval(np.polyfit(Vp, sv, 2), v))
        S = np.array(S)                                      # Ry/K per cell
        Cp = Ts * np.gradient(S, Ts)
        return Veq, alpha, S, Cp

    Veq, alpha, S, Cp = props(0.0)
    # zero-point: T = 0
    V0zp, c0 = equilibrium(0.0)
    d2 = np.polyval(np.polyder(c0, 2), V0zp)
    x = (V0 / V0zp) ** (2 / 3)
    # B_T(0) = V d2F/dV2 at the zero-point volume (numerical second derivative of the static fit + ZP part)
    h = 1e-3 * V0zp
    Fs = lambda v: bm(v, E0, V0, B0, Bp)  # noqa: E731
    d2s = (Fs(V0zp + h) - 2 * Fs(V0zp) + Fs(V0zp - h)) / h ** 2
    zpe_cell = fvib(*dos[int(np.argmin(abs(Vp - V0zp)))], 0.0)
    # the NTE range: where alpha is clearly negative (below 1 % of its minimum); below ~20 K alpha ~ T^3 is
    # numerically zero and its sign is noise
    neg = Ts[alpha < 0.01 * alpha.min()]
    i298 = int(np.argmin(abs(Ts - 298.15)))
    per_mol_atoms = RY_J * NA / 2
    F298 = []
    H = []
    for T, v in ((0.0, V0zp), (Ts[i298], Veq[i298])):
        fv = np.polyval(np.polyfit(Vp, [fvib(w, g, T) for w, g in dos], 2), v)
        F = Fs(v) + fv
        H.append(F + (T * S[i298] if T > 0 else 0.0))
    # C_P at a higher pressure: where does it cross the zero-pressure curve? (the paper: ~85 K)
    _, _, _, Cp5 = props(5.0)
    lo = (Ts > 20) & (Ts < 300)
    diff = (Cp5 - Cp)[lo]
    j = np.where(np.sign(diff[:-1]) != np.sign(diff[1:]))[0]
    cross = float(Ts[lo][j[0]]) if len(j) else None
    # mode Grueneisen parameters of TA(X), TA(L): -dln(w)/dln(V), quadratic fit in ln V
    lnV = np.log(Vp)
    gam, freq = {}, {}
    for mode in ("TA_X", "TA_L"):
        w = np.array([r[f"{mode}_cm"] for r in ph])
        c = np.polyfit(lnV, np.log(w), 2)
        for tag, aa in (("a0", PAPER["a0"]), ("exp_a", EXPT["a0"])):
            lv = math.log(aa ** 3 / 4)
            gam[f"g_{mode}_{tag}"] = float(-np.polyval(np.polyder(c), lv))
            freq[f"{mode}_{tag}"] = float(np.exp(np.polyval(c, lv)))
    out["qha"] = {
        "a0_zp": float((4 * V0zp) ** (1 / 3)), "B_zp_Mbar": float(V0zp * (d2s + d2) * RY_BOHR3_GPA / 100),
        "zpe_kJ_per_mol_cells": float(zpe_cell * RY_J * NA / 1000),
        "nte_range_K": [float(neg.min()), float(neg.max())] if len(neg) else None,
        "alpha_min_1e6": float(alpha.min() * 1e6), "T_alpha_min": float(Ts[np.argmin(alpha)]),
        "alpha_20K_1e6": float(alpha[int(np.argmin(abs(Ts - 20)))] * 1e6),
        "alpha_300K_1e6": float(alpha[i298] * 1e6),
        "S298_J_per_K_mol": float(S[i298] * per_mol_atoms), "dH298_kJ_per_mol": float((H[1] - H[0]) * per_mol_atoms / 1000),
        "cp_cross_K": cross, **gam, **freq,
        "volumes_bohr": [r["a"] for r in ph]}
    np.savez_compressed("qha.npz", T=Ts, a=(4 * Veq) ** (1 / 3), alpha=alpha, S=S, Cp=Cp, Cp5=Cp5)
    figures(Ts, alpha, Cp, Cp5, ph, out)
    return out


def figures(Ts, alpha, Cp, Cp5, ph, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(14, 3.8), constrained_layout=True)
    ax[0].plot(Ts, alpha * 1e6, color="#2a78d6", lw=2)
    ax[0].axhline(0, color="#6b6a63", lw=1)
    ax[0].set_xlim(0, 400)
    ax[0].set_ylim(-1, 3)
    ax[0].set_xlabel("T (K)")
    ax[0].set_ylabel("linear thermal expansion (1e-6 / K)")
    ax[0].set_title("thermal expansion (QHA, P = 0)")
    ax[1].plot(Ts, Cp * RY_J * NA / 2, color="#2a78d6", lw=2, label="P = 0")
    ax[1].plot(Ts, Cp5 * RY_J * NA / 2, color="#eb6834", lw=2, label="P = 5 GPa")
    ax[1].set_xlim(0, 300)
    ax[1].set_xlabel("T (K)")
    ax[1].set_ylabel("C_P (J / K / mol)")
    ax[1].legend(frameon=False)
    ax[1].set_title("specific heat")
    lnV = np.log([r["a"] ** 3 / 4 for r in ph])
    for k, (mode, col) in enumerate((("TA_X", "#1baf7a"), ("TA_L", "#eda100"))):
        ax[2].plot(lnV, np.log([r[f"{mode}_cm"] for r in ph]), "o-", color=col, label=mode)
    ax[2].set_xlabel("ln V (bohr^3)")
    ax[2].set_ylabel("ln omega (cm^-1)")
    ax[2].legend(frameon=False)
    ax[2].set_title("TA zone-boundary modes soften on compression")
    fig.savefig("qha.png", dpi=120)


def claims(o) -> list:
    rows = []
    s, q = o["static"], o.get("qha") or {}

    def add(what, paper, ours, tol, unit="", expt=None):
        ok = None if ours is None else abs(ours - paper) <= tol
        rows.append({"claim": what, "paper": paper, "reproduced": ours, "tolerance": tol, "unit": unit, "expt": expt,
                     "verdict": "pending" if ok is None else ("reproduced" if ok else "differs")})
    add("static LDA lattice constant", PAPER["a0_precise"], s["a0"], 0.05, "bohr", EXPT["a0"])
    add("static bulk modulus", PAPER["B_static_Mbar"], s["B0_Mbar"], 0.05, "Mbar")
    add("lattice constant with zero-point motion", PAPER["a0_zp"], q.get("a0_zp"), 0.05, "bohr")
    add("TA(X) at a = 10.18", PAPER["TA_X_a0"], q.get("TA_X_a0"), 5, "cm^-1")
    add("TA(L) at a = 10.18", PAPER["TA_L_a0"], q.get("TA_L_a0"), 5, "cm^-1")
    add("TA(X) at a = 10.26", PAPER["TA_X_exp_a"], q.get("TA_X_exp_a"), 5, "cm^-1", EXPT["TA_X"])
    add("TA(L) at a = 10.26", PAPER["TA_L_exp_a"], q.get("TA_L_exp_a"), 5, "cm^-1", EXPT["TA_L"])
    add("Grueneisen TA(X) at a = 10.18", PAPER["g_TA_X_a0"], q.get("g_TA_X_a0"), 0.3)
    add("Grueneisen TA(L) at a = 10.18", PAPER["g_TA_L_a0"], q.get("g_TA_L_a0"), 0.3)
    add("Grueneisen TA(X) at a = 10.26", PAPER["g_TA_X_exp_a"], q.get("g_TA_X_exp_a"), 0.3, "", EXPT["g_TA_X"])
    add("Grueneisen TA(L) at a = 10.26", PAPER["g_TA_L_exp_a"], q.get("g_TA_L_exp_a"), 0.3, "", EXPT["g_TA_L"])
    add("entropy at 298.15 K", PAPER["S298"], q.get("S298_J_per_K_mol"), 0.6, "J/K/mol", EXPT["S298"])
    add("H(298.15) - H(0)", PAPER["dH298_kJ"], q.get("dH298_kJ_per_mol"), 0.15, "kJ/mol", EXPT["dH298_kJ"])
    nr = q.get("nte_range_K")
    add("negative thermal expansion: upper end", PAPER["nte_range_K"][1], nr[1] if nr else None, 25, "K")
    add("negative thermal expansion: lower end", PAPER["nte_range_K"][0], nr[0] if nr else None, 15, "K")
    add("C_P independent of pressure near", PAPER["cp_cross_K"], q.get("cp_cross_K"), 20, "K")
    add("minimum of the linear expansion coefficient (expt ~ -0.5 near 80 K)", -0.5, q.get("alpha_min_1e6"), 0.25,
        "1e-6/K", -0.5)
    if o.get("ecut_study"):
        e = o["ecut_study"]
        top = max(e, key=lambda r: r["ecut"])
        add(f"static bulk modulus at the converged cutoff ({top['ecut']:.0f} Ry)", PAPER["B_static_Mbar"],
            top["B0_Mbar"], 0.05, "Mbar")
    return rows


if __name__ == "__main__":
    o = main()
    o["claims"] = claims(o)
    json.dump(o, open("qha.json", "w"), indent=1)
    lines = ["| claim | paper | reproduced | expt | verdict |", "|---|---|---|---|---|"]
    for c in o["claims"]:
        r = c["reproduced"]
        lines.append(f"| {c['claim']} ({c['unit']}) | {c['paper']} | {'-' if r is None else round(r, 3)} | "
                     f"{c['expt'] if c['expt'] is not None else ''} | **{c['verdict']}** |")
    Path("report.md").write_text("\n".join(lines) + "\n")
    n = {v: sum(c["verdict"] == v for c in o["claims"]) for v in ("reproduced", "differs", "pending")}
    print(json.dumps({"dir": os.getcwd(), "a0_static": o["static"]["a0"], "B0_Mbar": o["static"]["B0_Mbar"], **n}))
