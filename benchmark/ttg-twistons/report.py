"""The reproduction report: every theoretical claim of Turkel et al. (Science 376, 193, 2022) next to what this
run reproduced, with a verdict, plus figures. Missing inputs (steps still running) are reported as pending.

Reads $FLOWER_INPUTS (the step's resolved `inputs:`); writes report.md, claims.json and fig_*.png.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

COL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]  # validated
INK, MUTED, GRID = "#1f1f1e", "#6b6a63", "#e6e5df"

# Values read off the published figures (Science 376, 193) or quoted from its text.
PAPER = {
    "expt_sep": 18.0, "expt_fwhm": 23.0,              # Fig. 2E, text: "~18 meV", "~23 meV"
    "sp1_sep": 0.5, "sp1_fwhm": 0.5,                   # Fig. 2E: bars near zero ("severely underestimates")
    "sp2_sep": 18.0, "sp2_fwhm": 4.0,                  # Fig. 2E; text: widths "a factor of ~6 smaller"
    "hf_sep": 16.0, "hf_fwhm": 19.0,                   # Fig. 2E (HF bar incl. lifetime broadening)
    "split_1p8": 40.0,                                  # text: "split apart by ~40 meV ... unstrained TTG at ~1.8 deg"
    "resonance_nuP": 2.4,                               # Fig. 4D/E caption
    "relax_populations": [1.46, 1.53, 1.575],          # Fig. 3H peaks P, S, T (read off)
}


def load_inputs() -> dict:
    p = os.environ.get("FLOWER_INPUTS")
    return json.loads(Path(p).read_text()) if p and Path(p).is_file() else {}


def jload(d, name):
    try:
        return json.loads((Path(d) / name).read_text())
    except (OSError, TypeError, ValueError):
        return None


def verdict(val, ref, tol_abs=None, tol_rel=None):
    if val is None:
        return "pending"
    ok = True
    if tol_abs is not None:
        ok = ok and abs(val - ref) <= tol_abs
    if tol_rel is not None:
        ok = ok and abs(val - ref) <= tol_rel * abs(ref)
    return "reproduced" if ok else "differs"


def sp_by_label(sp):
    out = {}
    for it in sp or []:
        if isinstance(it, dict) and it.get("dir"):
            r = jload(it["dir"], "result.json")
            if r:
                out[r["label"]] = (r, Path(it["dir"]))
    return out


# ---------------------------------------------------------------------- flat-band resonance (Fig. 4D/E)
def resonance(sp):
    """Rigid single-particle bands at 1.45 (plaquette) and 1.8 deg (twiston), filled to the same carrier density:
    nu_T = nu_P * A_T / A_P. delta_c = (E_c,T - mu_T) - (E_c,P - mu_P), likewise delta_v; resonance where it
    vanishes. Peak energies: band-resolved top-layer DOS maxima (spectrum.npz)."""
    from ttg import filling_to_mu
    if "SP2 1.45" not in sp or "SP2 1.8" not in sp:
        return None
    res = {}
    for key in ("SP2 1.45", "SP2 1.8"):
        r, d = sp[key]
        e = np.load(d / "eigs.npz")
        s = np.load(d / "spectrum.npz")
        eg = s["egrid_meV"]
        res[key] = {"E": e["e_meV"] / 1000, "theta": r["theta"],
                    "e_v": float(eg[np.argmax(s["dos_vb"])]), "e_c": float(eg[np.argmax(s["dos_cb"])])}
    P, T = res["SP2 1.45"], res["SP2 1.8"]
    ratio = (math.sin(math.radians(P["theta"]) / 2) / math.sin(math.radians(T["theta"]) / 2)) ** 2
    nus = np.round(np.arange(-3.9, 3.91, 0.05), 3)
    rows = []
    for nu in nus:
        muP = 1000 * filling_to_mu(P["E"], 8, nu)
        muT = 1000 * filling_to_mu(T["E"], 8, nu * ratio)
        rows.append((nu, (T["e_c"] - muT) - (P["e_c"] - muP), (T["e_v"] - muT) - (P["e_v"] - muP), muP, muT))
    a = np.array(rows)

    def window(col, sign, tol=3.0):
        """Fillings (on the given side of CNP) where the twiston and plaquette peaks agree within tol meV:
        rigid bands give a plateau (|delta| ~ 2 meV), not a single crossing."""
        sel = (a[:, 0] * sign > 0) & (abs(a[:, col]) < tol)
        return [float(a[sel, 0].min()), float(a[sel, 0].max())] if sel.any() else None

    return {"nu": a[:, 0], "delta_c": a[:, 1], "delta_v": a[:, 2], "ratio": ratio,
            "window_electron": window(1, +1), "window_hole": window(2, -1),
            "min_delta_c": float(np.min(abs(a[a[:, 0] > 0, 1]))), "min_delta_v": float(np.min(abs(a[a[:, 0] < 0, 2]))),
            "P": P, "T": T}


# ---------------------------------------------------------------------- figures
def setup_mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                         "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
                         "axes.spines.top": False, "axes.spines.right": False})
    return plt


def fig_2e(plt, rows):
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.6), constrained_layout=True)
    names = [r[0] for r in rows]
    for k, (col, title) in enumerate(((1, "VHS separation (meV)"), (3, "VHS width, FWHM (meV)"))):
        a = ax[k]
        for i, r in enumerate(rows):
            if r[col] is not None:
                a.plot(r[col], i, "o", ms=9, color=COL[i % 8], mec="white", mew=1.5, zorder=3)
            if r[col + 1] is not None:
                a.plot(r[col + 1], i, "D", ms=7, mfc="none", mec=INK, mew=1.2, zorder=4)
        a.set_yticks(range(len(rows)))
        a.set_yticklabels(names if k == 0 else [""] * len(rows))
        a.invert_yaxis()
        a.set_title(title, color=INK, fontsize=11)
    ax[1].plot([], [], "o", color=MUTED, label="this reproduction")
    ax[1].plot([], [], "D", mfc="none", mec=INK, label="paper (Fig. 2E)")
    ax[1].legend(loc="lower right", fontsize=8, frameon=False)
    fig.savefig("fig_2e.png", dpi=130)


def fig_spectra(plt, sp, labels, name, title):
    fig, ax = plt.subplots(figsize=(6, 4.2), constrained_layout=True)
    off = 0
    for i, lab in enumerate(labels):
        if lab not in sp:
            continue
        s = np.load(sp[lab][1] / "spectrum.npz")
        y = s["ldos_top"]
        m = (s["egrid_meV"] > -120) & (s["egrid_meV"] < 120)
        from scipy.ndimage import gaussian_filter1d
        yy = gaussian_filter1d(y[m], 20)   # 2 meV display smoothing
        yy = yy / yy.max()
        ax.plot(s["egrid_meV"][m], yy + off, color=COL[i % 8], lw=2, label=lab)
        off += 1.1
    ax.set_xlabel("energy (meV)")
    ax.set_ylabel("top-layer LDOS (offset)")
    ax.set_yticks([])
    ax.legend(fontsize=8, frameon=False)
    ax.set_title(title, color=INK, fontsize=11)
    fig.savefig(name, dpi=130)


def fig_resonance(plt, rz, sp):
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8), constrained_layout=True)
    for i, lab in enumerate(("SP2 1.45", "SP2 1.8")):
        b = np.load(sp[lab][1] / "bands.npz")
        for j in range(b["e_meV"].shape[1]):
            ax[0].plot(b["x"], b["e_meV"][:, j], color=COL[i], lw=1.4, label=lab if j == 0 else None)
        ax[0].set_xticks(b["ticks"])
        ax[0].set_xticklabels([str(t) for t in b["labels"]])
    ax[0].set_ylim(-150, 150)
    ax[0].set_ylabel("energy (meV)")
    ax[0].legend(fontsize=8, frameon=False)
    ax[0].set_title("SP2 bands, 1.45 vs 1.8 deg", color=INK, fontsize=11)
    ax[1].plot(rz["nu"], rz["delta_c"], color=COL[2], lw=2, label="conduction peaks, delta_c")
    ax[1].plot(rz["nu"], rz["delta_v"], color=COL[3], lw=2, label="valence peaks, delta_v")
    ax[1].axhline(0, color=MUTED, lw=1)
    for w in (rz["window_electron"], rz["window_hole"]):
        if w:
            ax[1].axvspan(w[0], w[1], color=GRID, alpha=0.8, lw=0)
    ax[1].axvline(PAPER["resonance_nuP"], color=COL[7], lw=1.2, ls="--", label="paper: nu_P = 2.4")
    ax[1].set_xlabel("filling of the 1.45 deg region, nu_P")
    ax[1].set_ylabel("twiston - plaquette peak (meV)")
    ax[1].legend(fontsize=8, frameon=False)
    ax[1].set_title("flat-band resonance (Fig. 4D/E)", color=INK, fontsize=11)
    fig.savefig("fig_4_resonance.png", dpi=130)


# ---------------------------------------------------------------------- the claims
def main() -> dict:
    I = load_inputs()
    sp = sp_by_label(I.get("sp"))
    ex = jload((I.get("expt") or {}).get("dir"), "expt.json")
    hf = jload((I.get("hf") or {}).get("dir"), "hf.json")
    ahf = jload((I.get("authors_hf") or {}).get("local_dir"), "authors_hf.json")
    cor = jload((I.get("corrugation") or {}).get("dir"), "corrugation.json")
    rel = [jload(it.get("dir"), "relax.json") for it in (I.get("relax") or []) if isinstance(it, dict)]
    rel = [r for r in rel if r]
    claims = []

    def claim(fig, what, paper, ours, verdict_, note=""):
        claims.append({"figure": fig, "claim": what, "paper": paper, "reproduced": ours, "verdict": verdict_,
                       "note": note})

    g = lambda lab, k: sp[lab][0]["vhs"][k] if lab in sp else None  # noqa: E731
    # Fig. 1B
    if cor:
        claim("1B", "AtA corrugation is a hexagonal moire lattice, AtB a honeycomb", "hexagonal / honeycomb",
              f"AtA {cor['AtA']['maxima_per_moire_cell']:.2f} maxima/cell, {cor['AtA']['coordination']} neighbours; "
              f"AtB {cor['AtB']['maxima_per_moire_cell']:.2f}/cell, {cor['AtB']['coordination']} neighbours",
              "reproduced" if cor["AtA_hexagonal"] and cor["AtB_honeycomb"] else "differs")
    # Fig. 2E
    claim("2E", "SP1 (ab initio velocity) severely underestimates VHS separation and width",
          "both near 0 meV", f"sep {g('SP1 1.55', 'sep'):.1f}, FWHM {g('SP1 1.55', 'fwhm_mean'):.1f} meV"
          if "SP1 1.55" in sp else None,
          "reproduced" if "SP1 1.55" in sp and g("SP1 1.55", "sep") < 3 and g("SP1 1.55", "fwhm_mean") < 3 else "pending")
    claim("2E", "SP2 (+30 % velocity) reproduces the separation (~18 meV)", "~18 meV",
          f"{g('SP2 1.55', 'sep'):.1f} meV" if "SP2 1.55" in sp else None,
          verdict(g("SP2 1.55", "sep"), PAPER["sp2_sep"], tol_abs=2.0))
    claim("2E", "SP2 widths are ~6x smaller than experiment (~4 meV)", "~4 meV",
          f"{g('SP2 1.55', 'fwhm_mean'):.1f} meV" if "SP2 1.55" in sp else None,
          verdict(g("SP2 1.55", "fwhm_mean"), PAPER["sp2_fwhm"], tol_abs=1.5))
    if hf:
        h = hf["hf"]["eta_0.5"]
        claim("2E", "Hartree-Fock (eps 10) gives the separation (~16 meV)", "~16 meV",
              f"{h['sep']:.1f} meV (our implementation of the paper's scheme)",
              verdict(h["sep"], PAPER["hf_sep"], tol_abs=4.0),
              "converged" if hf["convergence"]["converged"] else "NOT converged")
        h4 = hf["hf"]["eta_4"]
        claim("2E", "HF + 4 meV lifetime broadening gives the widths (~19-23 meV)", "~19 meV",
              f"{h4['fwhm_mean']:.1f} meV", verdict(h4["fwhm_mean"], PAPER["hf_fwhm"], tol_abs=5.0))
    if ahf:
        claim("2E", "the authors' own HF code (Dataverse) reproduces their HF numbers", "~16 / ~19 meV",
              f"sep {ahf['sep_meV']:.1f}, FWHM {ahf['fwhm_meV']:.1f} meV",
              verdict(ahf["sep_meV"], PAPER["hf_sep"], tol_abs=4.0))
    if ex:
        claim("2E", "experimental reference re-derived from the raw Fig. 2B spectrum", "18 / 23 meV",
              f"{ex['cnp_sep_meV']:.1f} / {ex['cnp_fwhm_meV']:.1f} samples (fit variants: sep "
              f"{ex['sep_range_meV'][0]:.0f}-{ex['sep_range_meV'][1]:.0f}, FWHM {ex['fwhm_range_meV'][0]:.0f}-"
              f"{ex['fwhm_range_meV'][1]:.0f})", "differs" if abs(ex["cnp_sep_meV"] - 18) > 2 else "reproduced",
              "the data file has no energy axis; 1 meV/sample is assumed. The paper's numbers correspond to "
              f"{18 / ex['cnp_sep_meV']:.2f}-{23 / ex['cnp_fwhm_meV']:.2f} meV/sample (a +-160 mV sweep)")
    # fig. S14B
    ds = [lab for lab in ("SP2 1.55", "SP2 1.55 D=10meV", "SP2 1.55 D=20meV", "SP2 1.55 D=40meV") if lab in sp]
    if len(ds) == 4:
        seps = [g(lab, "sep") for lab in ds]
        ws = [g(lab, "fwhm_mean") for lab in ds]
        claim("S14B", "displacement field (experimental range) barely changes separation and widths",
              "no notable change", f"sep {min(seps):.1f}-{max(seps):.1f}, FWHM {min(ws):.1f}-{max(ws):.1f} meV "
              "for D = 0-40 meV", "reproduced" if max(seps) - min(seps) < 2 and max(ws) - min(ws) < 2 else "differs")
    # Fig. 3F
    if all(k in sp for k in ("SP2 1.45", "SP2 1.6 strain 0.55%", "SP2 1.9", "SP2 1.8")):
        claim("3F", "unstrained 1.8 deg: flat bands split apart by ~40 meV", "~40 meV",
              f"{g('SP2 1.8', 'sep'):.1f} meV (1.9 deg: {g('SP2 1.9', 'sep'):.1f})",
              verdict(g("SP2 1.8", "sep"), PAPER["split_1p8"], tol_rel=0.25))
        w0, ws_ = g("SP2 1.6 strain 0.55%", "fwhm_mean"), g("SP2 1.45", "fwhm_mean")
        claim("3F", "0.55 % heterostrain at 1.6 deg attenuates/broadens the flat-band peaks", "attenuated",
              f"FWHM {w0:.1f} meV vs {ws_:.1f} meV unstrained 1.45 deg", "reproduced" if w0 > 2 * ws_ else "differs")
    # Fig. 4
    rz = resonance(sp)
    if rz:
        we, wh = rz["window_electron"], rz["window_hole"]
        inside = we is not None and we[0] <= PAPER["resonance_nuP"] <= we[1]
        claim("4D/E", "flat-band resonance (1.45 vs 1.8 deg) at nu_P = 2.4", "nu_P = 2.4",
              (f"peaks aligned within 3 meV for nu_P in [{we[0]:.1f}, {we[1]:.1f}] (electrons)" if we else "no window")
              + (f", [{wh[0]:.1f}, {wh[1]:.1f}] (holes)" if wh else "")
              + f"; closest {rz['min_delta_c']:.1f} meV",
              "reproduced" if inside else "differs",
              f"rigid bands, same carrier density: nu_T = {rz['ratio']:.3f} nu_P")
    # Fig. 3G/H, S10
    main_rel = next((r for r in rel if abs(r["theta_BM"] - 1.69) < 1e-6 and r["N"] == 54 and r["amp"] == 10), None)
    if main_rel:
        pops = [round(p["theta"], 3) for p in main_rel["populations"]]
        claim("3G/H", "relaxation gives universal AtA stacking", "all AtA", f"AtA fraction {main_rel['ata_fraction']:.2f}",
              "reproduced" if main_rel["ata_fraction"] > 0.9 else "differs")
        claim("3H", "local twist angles are trimodal: plaquette / soliton / twiston", "~1.46 / 1.53 / 1.575",
              f"{len(pops)} populations at {pops}", "reproduced" if len(pops) == 3 else "differs")
    scan = sorted([r for r in rel if r["amp"] == 10 and r["N"] == 36], key=lambda r: r["delta_theta"])
    if scan:
        txt = ", ".join(f"dtheta {r['delta_theta']:.2f}: theta_I {r['theta_I']:.3f}" for r in scan if r["theta_I"])
        lock = all(abs(r["theta_I_minus_smaller"]) < 0.05 for r in scan if r["delta_theta"] <= 0.5 and r["theta_I"])
        claim("S10", "theta_I locks to the smaller of theta_TM, theta_BM for dtheta <~ 0.5 deg",
              "theta_I = 1.5 deg (the smaller)", txt, "reproduced" if lock else "differs")

    plt = setup_mpl()
    rows = [("experiment", PAPER["expt_sep"], PAPER["expt_sep"], PAPER["expt_fwhm"], PAPER["expt_fwhm"]),
            ("SP1", g("SP1 1.55", "sep"), PAPER["sp1_sep"], g("SP1 1.55", "fwhm_mean"), PAPER["sp1_fwhm"]),
            ("SP2", g("SP2 1.55", "sep"), PAPER["sp2_sep"], g("SP2 1.55", "fwhm_mean"), PAPER["sp2_fwhm"])]
    if hf:
        rows.append(("HF (ours, +4 meV)", hf["hf"]["eta_4"]["sep"], PAPER["hf_sep"], hf["hf"]["eta_4"]["fwhm_mean"],
                     PAPER["hf_fwhm"]))
    if ahf:
        rows.append(("HF (authors' code)", ahf["sep_meV"], PAPER["hf_sep"], ahf["fwhm_meV"], PAPER["hf_fwhm"]))
    fig_2e(plt, rows)
    fig_spectra(plt, sp, ["SP2 1.45", "SP2 1.6 strain 0.55%", "SP2 1.9"], "fig_3f.png",
                "Fig. 3F: (theta, strain) = (1.45, 0), (1.6, 0.55 %), (1.9, 0)")
    fig_spectra(plt, sp, ["SP2 1.55", "SP2 1.55 D=10meV", "SP2 1.55 D=20meV", "SP2 1.55 D=40meV"], "fig_s14b.png",
                "fig. S14B: displacement field, SP2 1.55 deg")
    if rz:
        fig_resonance(plt, rz, sp)
    if scan:
        fig, ax = plt.subplots(figsize=(5.5, 3.8), constrained_layout=True)
        for k, (amp, mk) in enumerate(((10, "o"), (1, "s"))):
            pts = [r for r in rel if r["amp"] == amp and r["theta_I"]]
            if pts:
                ax.plot([r["delta_theta"] for r in pts], [r["theta_I"] for r in pts], mk, ms=9, color=COL[k],
                        mec="white", mew=1.5, label=f"GSFE x{amp}" + (" (authors')" if amp == 10 else " (physical)"))
        ax.axhline(1.5, color=MUTED, lw=1, ls="--", label="theta_TM = 1.5 (the smaller twist)")
        ax.set_xlabel("delta theta = theta_BM - theta_TM (deg)")
        ax.set_ylabel("plaquette twist theta_I (deg)")
        ax.legend(fontsize=8, frameon=False)
        ax.set_title("fig. S10: does theta_I lock to the smaller twist?", color=INK, fontsize=11)
        fig.savefig("fig_s10.png", dpi=130)

    counts = {}
    for c in claims:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    lines = ["# Reproduction of the theory in Turkel et al., Science 376, 193 (2022)", "",
             "| fig. | claim | paper | reproduced | verdict |", "|---|---|---|---|---|"]
    for c in claims:
        lines.append(f"| {c['figure']} | {c['claim']} | {c['paper']} | {c['reproduced'] or '-'} | **{c['verdict']}**"
                     + (f" ({c['note']})" if c["note"] else "") + " |")
    Path("report.md").write_text("\n".join(lines) + "\n")
    Path("claims.json").write_text(json.dumps(claims, indent=1))
    return {"dir": os.getcwd(), "n_claims": len(claims), **{f"n_{k}": v for k, v in counts.items()}}


if __name__ == "__main__":
    print(json.dumps(main()))
