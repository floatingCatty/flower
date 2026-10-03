"""Local analysis: compare the valence-band quantities of all methods with experiment (table + charts).

Called by the flower `compare` node with the eig.json files fetched back from the remote machine.
"""
import json
import math
import os
from pathlib import Path

import si_valence as sv

# Experiment (low temperature): Delta_so = 44.1 meV; Luttinger parameters gamma1..3 = 4.285, 0.339, 1.446
# (cyclotron resonance; as tabulated e.g. in the nextnano database). Masses follow from the gammas:
# m_hh[100] = 1/(g1 - 2 g2), m_lh[100] = 1/(g1 + 2 g2), m_hh[111] = 1/(g1 - 2 g3), m_lh[111] = 1/(g1 + 2 g3),
# m_so = 1/g1. No direct measurement of the 1 % biaxial splitting: the k.p row (experimental b) is the reference.
G1, G2, G3 = 4.285, 0.339, 1.446
EXP = {"dso_meV": 44.1, "gamma1": G1, "gamma2": G2, "gamma3": G3,
       "m_hh_100": 1 / (G1 - 2 * G2), "m_lh_100": 1 / (G1 + 2 * G2), "m_so_100": 1 / G1,
       "m_hh_111": 1 / (G1 - 2 * G3), "m_lh_111": 1 / (G1 + 2 * G3)}

ORDER = ["qe", "abacus", "pyscf", "dftb", "tb", "epm", "kp"]
LABEL = {"qe": "QE (PBE, PW)", "abacus": "ABACUS (PBE, PW)", "pyscf": "PySCF (PBE, all-e X2C)",
         "dftb": "DFTB+ (pbc-0-3)", "tb": "Tight binding sp3d5s*", "epm": "Empirical pseudopot.",
         "kp": "k·p 6-band (exp. par.)"}
COLOR = {"qe": "#2a78d6", "abacus": "#eb6834", "pyscf": "#1baf7a", "dftb": "#eda100", "tb": "#e87ba4",
         "epm": "#008300", "kp": "#4a3aa7"}  # categorical slots 1-7 in fixed order (validated, light mode)
INK, MUTED, GRID = "#1f1f1e", "#6b6a63", "#e6e5df"


def _dots(ax, keys, vals, ref=None, ref_label="experiment", fmt="{:.3f}", title=""):
    """Cleveland dot plot: one dot per method on a tight axis, the reference as a dashed line. (Bars would
    have to start at zero and hide differences of a few percent; truncated bars would mislead.)"""
    y = list(range(len(keys)))[::-1]
    pts = vals + ([ref] if ref is not None else [])
    lo, hi = min(pts), max(pts)
    pad = (hi - lo) * 0.25 or abs(hi) * 0.1 or 1.0
    ax.set_xlim(lo - pad, hi + pad * 1.6)
    if ref is not None:
        ax.axvline(ref, color=INK, lw=1.1, ls=(0, (4, 3)), zorder=1)
        ax.text(ref, -0.75, f"{ref_label} {fmt.format(ref)}", fontsize=8, color=MUTED, ha="center", va="top",
                zorder=4, bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.0})
    for yi, k, v in zip(y, keys, vals):
        ax.plot([lo - pad, v], [yi, yi], color=GRID, lw=0.8, zorder=0)
        ax.scatter([v], [yi], s=64, color=COLOR[k], edgecolor="white", linewidth=1.5, zorder=3)
        ax.text(v + pad * 0.22, yi, fmt.format(v), va="center", fontsize=8, color=INK, zorder=4,
                bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.6})
    ax.set_yticks(y)
    ax.set_yticklabels([LABEL[k] for k in keys], color=INK, fontsize=9)
    ax.set_ylim(-1.2, len(keys) - 0.4)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(axis="x", colors=MUTED, labelsize=8)
    ax.tick_params(axis="y", length=0)


def compare(eig_files: dict, ctx: dict) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    wd = Path(ctx["workdir"])
    res = {}
    for key in ORDER:
        p = eig_files.get(key)
        if p and os.path.exists(p):
            res[key] = sv.analyse(json.loads(Path(p).read_text()))
    keys = [k for k in ORDER if k in res]

    # ---- table (markdown) -- the accessible view of every chart
    cols = [("dso_meV", "Δso (meV)", "{:.1f}"), ("m_hh_100", "m_hh[100]", "{:.3f}"), ("m_lh_100", "m_lh[100]", "{:.3f}"),
            ("m_so_100", "m_so", "{:.3f}"), ("m_hh_111", "m_hh[111]", "{:.3f}"), ("m_lh_111", "m_lh[111]", "{:.3f}"),
            ("gamma1", "γ1", "{:.2f}"), ("gamma2", "γ2", "{:.2f}"), ("gamma3", "γ3", "{:.2f}"),
            ("strain_split12_meV", "HH–LH @1% (meV)", "{:.0f}"), ("strain_split13_meV", "E1–E3 @1% (meV)", "{:.0f}")]
    lines = ["| method | " + " | ".join(c[1] for c in cols) + " |", "|---|" + "---|" * len(cols)]
    for k in keys:
        lines.append(f"| {LABEL[k]} | " + " | ".join(c[2].format(res[k][c[0]]) for c in cols) + " |")
    lines.append("| **experiment** | " + " | ".join(c[2].format(EXP[c[0]]) if c[0] in EXP else "—" for c in cols) + " |")
    notes = [f"- **{LABEL[k]}**: {res[k]['notes']}" for k in keys]
    md = ["# Si valence band at Γ: method comparison", "",
          "All with spin-orbit coupling, at a0 = 5.431 Å. Masses in m0 (holes, positive), from the band curvature at "
          "|k| = 0.005, 0.01, 0.02 (2π/a0) along [100] and [111]. Strain: biaxial (001), ε∥ = 1 %, ε⊥ = −2C12/C11 ε∥.",
          "", *lines, "", "Method notes:", *notes, ""]
    (wd / "comparison.md").write_text("\n".join(md))

    # ---- charts
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=150)
    _dots(ax, keys, [res[k]["dso_meV"] for k in keys], EXP["dso_meV"], fmt="{:.1f}",
          title="Spin-orbit split-off gap Δso at Γ (meV)")
    fig.tight_layout(); fig.savefig(wd / "dso.png"); plt.close(fig)

    mass_cols = [("m_hh_100", "HH [100]"), ("m_lh_100", "LH [100]"), ("m_so_100", "SO"),
                 ("m_hh_111", "HH [111]"), ("m_lh_111", "LH [111]")]
    fig, axes = plt.subplots(1, 5, figsize=(14, 3.8), dpi=150, sharey=True)
    for ax, (c, t) in zip(axes, mass_cols):
        _dots(ax, keys, [res[k][c] for k in keys], EXP[c], title=f"{t} mass (m0)")
        if ax is not axes[0]:
            ax.tick_params(axis="y", labelleft=False)
    fig.tight_layout(); fig.savefig(wd / "masses.png"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=150)
    kp_ref = res.get("kp", {}).get("strain_split12_meV")
    _dots(ax, keys, [res[k]["strain_split12_meV"] for k in keys], kp_ref, ref_label="k·p (exp. b)", fmt="{:.0f}",
          title="HH–LH splitting at Γ, 1 % biaxial (001) tension (meV)")
    fig.tight_layout(); fig.savefig(wd / "strain.png"); plt.close(fig)

    dft = [k for k in ("qe", "abacus", "pyscf") if k in res]
    head = (f"Δso: DFT {min(res[k]['dso_meV'] for k in dft):.1f}–{max(res[k]['dso_meV'] for k in dft):.1f} meV vs "
            f"exp 44.1; DFTB+ {res['dftb']['dso_meV']:.1f}" if dft and "dftb" in res else f"{len(keys)} methods compared")
    return {"methods": keys, "results": {k: {c[0]: res[k][c[0]] for c in cols} for k in keys}, "experiment": EXP,
            "table_md": "\n".join(lines), "summary": head}
