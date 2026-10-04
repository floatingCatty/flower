"""Analysis of the J1-J2 chain diagonalizations against Eggert, PRB 54, R9612 (1996).

  python3 analysis.py rule     FLOWER_INPUTS {"sectors": [...]}: the symmetry blocks of the ground state, the lowest
                               excited singlet and the lowest triplet for L = 0 and 2 (mod 4), required to be the same
                               for every L scanned
  python3 analysis.py final    FLOWER_INPUTS {"table1": path, "ed": [...], "fit": {...}}: Table I, Table II and Figs. 3-4 (the
                               multiplicative logarithmic correction), and J2crit; writes report.md, claims.json,
                               j1j2.png
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.optimize import brentq, curve_fit

J2CRIT_PAPER = (0.241167, 0.000005)
J2CRIT_OKAMOTO_NOMURA = (0.2411, 0.0001)             # the paper's ref. [14], level crossing extrapolated in L
# Table II, transcribed from the PDF (its text layer drops one J2 label): J2 -> (ln r0, a*lambda0)
TABLE2 = {-0.25: (-1.4976, 0.021601), -0.1: (-2.1683, 0.014864), 0.0: (-2.9501, 0.010696),
          0.05: (-3.6111, 0.008626), 0.1: (-4.6883, 0.006546), 0.15: (-6.9080, 0.004383), 0.2: (-14.393, 0.002080)}
FIG3 = {"a": 0.0296, "r1": 0.85}
FIG4 = {"c1": 1.723, "c2": -1.35, "c3": 1.76}


def inputs():
    return json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())


def rule():
    by = {}
    for s in inputs()["sectors"]:
        key = str(s["L"] % 4)
        blocks = {w: s[w] for w in ("gs", "singlet", "triplet")}
        if by.setdefault(key, blocks) != blocks:
            raise SystemExit("L=%d breaks the rule for L = %s (mod 4): %s vs %s" % (s["L"], key, blocks, by[key]))
    if sorted(by) != ["0", "2"]:
        raise SystemExit("need sizes with L = 0 and 2 (mod 4), got %s" % sorted(by))
    return {"by_mod4": by, "sizes": sorted(s["L"] for s in inputs()["sectors"])}


FIT_DEFAULT = {"rmin": 5, "rmax": 15, "spline": "not-a-knot", "fit": "rG", "points": "integer"}


def rG_fit(r, szsz, opts=None):
    """Eggert's procedure: cubic splines of r<Sz(0)Sz(r)> through even and odd r, half their difference is the
    alternating amplitude rG(r); fit rG = sqrt(a*lambda0 * ln(r/r0)) (his eq. 14). The details he does not state
    (r range, spline end conditions, fitting rG or (rG)^2, integer or dense r) come from ``opts`` (the `fitscan`
    step picks those that turn his own Table I into his Table II)."""
    o = {**FIT_DEFAULT, **(opts or {})}
    r, y = np.asarray(r), np.asarray(r) * np.asarray(szsz)
    ev, od = r % 2 == 0, r % 2 == 1
    se, so = CubicSpline(r[ev], y[ev], bc_type=o["spline"]), CubicSpline(r[od], y[od], bc_type=o["spline"])
    rr = np.arange(o["rmin"], o["rmax"] + 1) if o["points"] == "integer" else np.linspace(o["rmin"], o["rmax"], 200)
    g = (se(rr) - so(rr)) / 2
    f = lambda x, al, lnr0: np.sqrt(np.clip(al * (np.log(x) - lnr0), 0, None))
    if o["fit"] == "(rG)^2 linear":
        slope, icpt = np.polyfit(np.log(rr), g ** 2, 1)
        al, lnr0 = slope, -icpt / slope
    else:
        (al, lnr0), _ = curve_fit(f, rr, g, p0=(0.01, -3.0), maxfev=20000)
    resid = float(np.max(np.abs(f(rr, al, lnr0) - g)))
    return {"a_lambda0": float(al), "ln_r0": float(lnr0), "max_resid": resid, "r": rr.tolist(), "rG": g.tolist()}


def final():
    I = inputs()
    paper1 = json.loads(Path(I["table1"]).read_text())
    runs = []
    for it in I["ed"]:
        d = Path((it or {}).get("local_dir") or "")
        if (d / "result.json").is_file():
            runs.append(json.loads((d / "result.json").read_text()))
    runs.sort(key=lambda x: x["L"])
    Ls = [x["L"] for x in runs]
    corr = {x["L"]: {c["J2"]: c["SzSz"] for c in x["corr"]} for x in runs}
    claims, lines = [], []

    # --- Table I: <Sz(0) Sz(L/2)>, 13 sizes x 5 couplings
    cols = [c if c != "crit" else J2CRIT_PAPER[0] for c in paper1["columns"]]
    diffs, rows = [], []
    for r, vals in zip(paper1["r"], paper1["values"]):
        if 2 * r not in corr:
            continue
        ours = [corr[2 * r][j2] for j2 in cols]
        diffs += [abs(a - b) for a, b in zip(ours, vals)]
        rows.append((r, vals, ours))
    n_agree = sum(d <= 1.5e-7 for d in diffs)          # to the 7 printed digits
    n_last = sum(d <= 5.5e-7 for d in diffs)           # the paper: "in some cases the last digit is uncertain"
    claims.append({"claim": "Table I: <Sz(0)Sz(L/2)>, L = 8..32, five J2",
                   "paper": "65 values, 7 decimals (\"in some cases the last digit is uncertain\")",
                   "this_run": "%d/%d agree to the printed digits, %d within a few units of the last (max |diff| %.1e)"
                               % (n_agree, len(diffs), n_last, max(diffs)),
                   "ok": n_last == len(diffs) == 65})
    lines += ["## Table I: <Sz(0) Sz(r)> at r = L/2", "",
              "| r | " + " | ".join("J2=%s paper / this run" % c for c in paper1["columns"]) + " |",
              "|---|" + "---|" * len(cols)]
    for r, vals, ours in rows:
        lines.append("| %d | " % r + " | ".join("%.7f / %.7f" % (a, b) for a, b in zip(vals, ours)) + " |")

    # --- Table II: the fit of eq. (14), first to the paper's own Table I (checks the procedure), then to ours
    fits_paper = {}
    for j, c in enumerate(cols[:4]):
        fits_paper[c] = rG_fit(paper1["r"], [v[j] for v in paper1["values"]], I.get("fit"))
    fits = {}
    for j2 in TABLE2:
        rs = [L // 2 for L in Ls if j2 in corr[L]]
        fits[j2] = rG_fit(rs, [corr[2 * r][j2] for r in rs], I.get("fit"))
    fo = {**FIT_DEFAULT, **(I.get("fit") or {})}
    lines += ["", "## Table II: rG(r) = sqrt(a*lambda0 * ln(r/r0))", "",
              "Fitted on r = %d..%d (%s points, %s splines, fit to %s): the choices that best turn the paper's own "
              "Table I into its Table II (step `fitscan`)." % (fo["rmin"], fo["rmax"], fo["points"], fo["spline"],
                                                               fo["fit"]), "",
              "| J2 | paper ln r0 | from the paper's Table I | this run | paper a*lambda0 | from the paper's Table I | this run |",
              "|---|---|---|---|---|---|---|"]
    t2_ok = []
    for j2, (lnr0, al) in TABLE2.items():
        fp, f = fits_paper.get(j2), fits[j2]
        lines.append("| %g | %.4f | %s | %.4f | %.6f | %s | %.6f |" % (
            j2, lnr0, "%.4f" % fp["ln_r0"] if fp else "", f["ln_r0"], al, "%.6f" % fp["a_lambda0"] if fp else "",
            f["a_lambda0"]))
        t2_ok.append(abs(f["a_lambda0"] - al) / al < 0.03 and abs(f["ln_r0"] - lnr0) / abs(lnr0) < 0.05)
    claims.append({"claim": "Table II: ln r0 and a*lambda0 at seven J2", "paper": "see table",
                   "this_run": "%d/7 within 5%% (ln r0) and 3%% (a*lambda0)" % sum(t2_ok), "ok": all(t2_ok)})

    # --- Fig. 3: ln r0 = ln r1 - a/(a*lambda0);  Fig. 4: lambda0 = c1 d + c2 d^2 + c3 d^3, d = J2crit - J2
    x = np.array([1 / fits[j]["a_lambda0"] for j in TABLE2])
    y = np.array([fits[j]["ln_r0"] for j in TABLE2])
    slope, icpt = np.polyfit(x, y, 1)
    a, r1 = -slope, float(np.exp(icpt))
    claims.append({"claim": "Fig. 3: ln r0 = ln r1 - 1/lambda0, a and r1", "paper": "a = 0.0296, r1 = 0.85",
                   "this_run": "a = %.4f, r1 = %.3f" % (a, r1),
                   "ok": abs(a - FIG3["a"]) / FIG3["a"] < 0.05 and abs(r1 - FIG3["r1"]) / FIG3["r1"] < 0.1})
    dj = np.array([J2CRIT_PAPER[0] - j for j in TABLE2])
    lam = np.array([fits[j]["a_lambda0"] / a for j in TABLE2])
    c, *_ = np.linalg.lstsq(np.vstack([dj, dj ** 2, dj ** 3]).T, lam, rcond=None)
    claims.append({"claim": "Fig. 4: lambda0(dJ2) = c1 dJ2 + c2 dJ2^2 + c3 dJ2^3", "paper": "c1 = 1.723, c2 = -1.35, c3 = 1.76",
                   "this_run": "c1 = %.3f, c2 = %.2f, c3 = %.2f" % tuple(c),
                   "ok": abs(c[0] - FIG4["c1"]) / FIG4["c1"] < 0.05})

    # --- J2crit: Delta = E(S=0) - E(S=1), the two lowest excitations
    delta = {}
    for x_ in runs:
        j2 = np.array([l["J2"] for l in x_["levels"]])
        delta[x_["L"]] = CubicSpline(j2, np.array([l["delta"] for l in x_["levels"]]))
    lo, hi = 0.20, 0.28
    cross = {L: brentq(delta[L], lo, hi, xtol=1e-12) for L in Ls}
    big = [L for L in Ls if L >= 12]
    A = np.vstack([np.ones(len(big)), 1 / np.array(big, float) ** 2, 1 / np.array(big, float) ** 4]).T
    on, *_ = np.linalg.lstsq(A, np.array([cross[L] for L in big]), rcond=None)
    pairs = {}
    for step in (2, 4):
        for L in Ls:
            if L + step in delta:
                f = lambda j, L=L, M=L + step: L ** 3 * delta[L](j) - M ** 3 * delta[M](j)
                try:
                    pairs.setdefault(step, {})[L] = brentq(f, lo, hi, xtol=1e-12)
                except ValueError:
                    pass
    last4 = [pairs[4][L] for L in sorted(pairs[4]) if L >= 20]
    est, spread = last4[-1], float(np.ptp(last4[-3:]))
    claims.append({"claim": "J2crit from Delta(L) ~ 1/L^3 (Eggert)", "paper": "0.241167 +- 0.000005",
                   "this_run": "%.6f (pair L=%d,%d; spread of the last three pairs %.6f)" % (
                       est, sorted(pairs[4])[-1], sorted(pairs[4])[-1] + 4, spread),
                   "ok": abs(est - J2CRIT_PAPER[0]) <= max(3 * J2CRIT_PAPER[1], spread)})
    claims.append({"claim": "J2crit from the singlet-triplet crossing, extrapolated in 1/L^2 (Okamoto-Nomura)",
                   "paper": "0.2411 +- 0.0001 (ref. 14)", "this_run": "%.5f (L = %d..%d)" % (on[0], big[0], big[-1]),
                   "ok": abs(on[0] - J2CRIT_OKAMOTO_NOMURA[0]) <= J2CRIT_OKAMOTO_NOMURA[1]})
    lines += ["", "## J2crit", "", "| L | crossing (Delta = 0) | L^3 Delta(L) = (L+2)^3 Delta(L+2) | L^3 Delta(L) = (L+4)^3 Delta(L+4) |",
              "|---|---|---|---|"]
    for L in Ls:
        lines.append("| %d | %.6f | %s | %s |" % (L, cross[L], "%.6f" % pairs[2][L] if L in pairs.get(2, {}) else "",
                                                 "%.6f" % pairs[4][L] if L in pairs.get(4, {}) else ""))

    e0 = {x_["L"]: next(c["E0"] for c in x_["corr"] if c["J2"] == 0.0) for x_ in runs}
    figure(fits, fits_paper, cross, pairs, on, x, y, a, icpt)
    n_ok = sum(c["ok"] for c in claims)
    head = ["# J1-J2 chain (Eggert, PRB 54, R9612): %d of %d claims reproduced" % (n_ok, len(claims)), "",
            "| claim | paper | this run | |", "|---|---|---|---|"]
    head += ["| %s | %s | %s | %s |" % (c["claim"], c["paper"], c["this_run"], "✓" if c["ok"] else "✗") for c in claims]
    head += ["", "Ground-state energy per site at J2 = 0: " + ", ".join("L=%d %.8f" % (L, e0[L] / L) for L in Ls)
             + " (Bethe ansatz, L -> infinity: %.8f)" % (0.25 - np.log(2)), ""]
    Path("report.md").write_text("\n".join(head + lines) + "\n")
    json.dump({"claims": claims, "crossing": cross, "pairs": pairs, "fits": {str(k): v for k, v in fits.items()},
               "fits_paper_table1": {str(k): v for k, v in fits_paper.items()}}, open("claims.json", "w"), indent=1,
              default=lambda o: o.item())
    return {"n_claims": len(claims), "n_reproduced": int(n_ok), "J2crit": est, "J2crit_crossing": float(on[0]),
            "table1_agree": n_agree, "a": a, "r1": r1}


def figure(fits, fits_paper, cross, pairs, on, x, y, a, icpt):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(14, 4.2))
    for j2, f in fits.items():
        ax[0].plot(f["r"], f["rG"], "o", ms=3)
        rr = np.linspace(min(f["r"]), max(f["r"]), 100)
        ax[0].plot(rr, np.sqrt(f["a_lambda0"] * (np.log(rr) - f["ln_r0"])), "-", lw=1, label="J2=%g" % j2)
    ax[0].set(xlabel="r", ylabel="rG(r)", title="multiplicative log correction (Fig. 2)")
    ax[0].legend(fontsize=7)
    Ls = sorted(cross)
    ax[1].plot([1 / L ** 2 for L in Ls], [cross[L] for L in Ls], "o", label="Delta = 0 crossing")
    for step, m in ((2, "s"), (4, "^")):
        p = pairs.get(step, {})
        ax[1].plot([1 / (L + step / 2) ** 2 for L in p], list(p.values()), m, label="L^3 Delta equal, L and L+%d" % step)
    xx = np.linspace(0, 1 / 144, 50)
    ax[1].plot(xx, on[0] + on[1] * xx + on[2] * xx ** 2, "-", lw=1, color="gray")
    ax[1].axhline(J2CRIT_PAPER[0], color="k", lw=0.8, ls="--", label="paper 0.241167")
    ax[1].set(xlabel="1/L^2", ylabel="J2", title="critical coupling")
    ax[1].legend(fontsize=7)
    ax[2].plot(x, y, "o", label="this run")
    ax[2].plot([1 / v[1] for v in TABLE2.values()], [v[0] for v in TABLE2.values()], "x", label="paper Table II")
    xx = np.linspace(0, max(x) * 1.05, 50)
    ax[2].plot(xx, icpt - a * xx, "-", lw=1)
    ax[2].set(xlabel="1/(a lambda0)", ylabel="ln r0", title="Fig. 3")
    ax[2].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig("j1j2.png", dpi=120)


if __name__ == "__main__":
    print(json.dumps({"rule": rule, "final": final}[sys.argv[1]](), default=lambda o: o.item()))
