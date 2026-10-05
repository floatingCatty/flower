"""Finite-size scaling of the reduced localization length Lambda(L, W) with corrections to scaling, as Slevin &
Ohtsuki (1999), Eqs. (2)-(4):

  Lambda = sum_{n=0}^{nI} chi_i^n L^{n y} F_n(chi_r L^{1/nu}),   F_n(x) = sum_{k=0}^{nR} a_nk x^k,
  chi_r = sum_{m=1}^{mR} b_m w^m,  chi_i = sum_{m=0}^{mI} c_m w^m,  w = (W - W_c) / W_c,
with a_01 = 1 and c_0 = 1 fixing the scales (the box fit of Table I: nR=3, nI=1, mR=2, mI=0, 12 parameters).
Lambda_c = a_00. Also the fit without corrections (nI = 0) on L >= 8 near W_c, as Table III.

  python3 fss.py     FLOWER_INPUTS {"tables": tables.json, "points": [tm step outputs]}
  python3 fss.py selftest   synthetic data from the model (W_c 16.54, nu 1.57, y -2.8), 0.1 % noise, the paper's
                            sizes and W grid: the fit must recover W_c and nu inside its 95 % intervals
chi^2 fits (scipy least_squares), goodness of fit Q, and 95 % intervals from 200 fits to data resampled within
their errors (the paper's re-sampling). Writes fss.json, report.md, fss.png; prints the outputs JSON.
"""
import json
import os
from pathlib import Path

import numpy as np
from scipy import optimize, stats


def model(p, L, W, nR, nI, mR, mI):
    Wc, nu, y = p[0], p[1], p[2]
    k = 3
    a = np.zeros((nI + 1, nR + 1))
    for n in range(nI + 1):
        for j in range(nR + 1):
            if n == 0 and j == 1:
                a[n, j] = 1.0                    # a_01 = 1
            else:
                a[n, j] = p[k]; k += 1
    b = p[k:k + mR]; k += mR
    c = np.concatenate([[1.0], p[k:k + mI]])     # c_0 = 1
    w = (W - Wc) / Wc
    chi_r = sum(b[m] * w ** (m + 1) for m in range(mR))
    chi_i = sum(c[m] * w ** m for m in range(mI + 1))
    x = chi_r * L ** (1 / nu)
    out = 0.0
    for n in range(nI + 1):
        Fn = sum(a[n, j] * x ** j for j in range(nR + 1))
        out = out + (chi_i * L ** y) ** n * Fn if n else out + Fn
    return out


def nparams(nR, nI, mR, mI):
    return 3 + (nI + 1) * (nR + 1) - 1 + mR + mI


def fit(L, W, Lam, err, form, p0=None, starts=12, rng=None):
    nR, nI, mR, mI = form
    np_ = nparams(*form)
    best = None
    rng = rng or np.random.default_rng(0)
    for s in range(starts):
        if p0 is not None and s == 0:
            x0 = np.array(p0)
        else:
            x0 = np.concatenate([[16.5 + rng.normal(0, 0.2), 1.5 + rng.normal(0, 0.1), -3 + rng.normal(0, 0.5)],
                                 rng.normal(0, 0.3, np_ - 3)])
            x0[3] = 0.58                       # a_00 ~ Lambda_c
            idx_b = 3 + (nI + 1) * (nR + 1) - 1
            x0[idx_b] = -1.5 + rng.normal(0, 0.3)
        if nI == 0:
            x0[2] = 0.0
        res = optimize.least_squares(lambda p: (model(p, L, W, *form) - Lam) / err, x0, method="lm", max_nfev=20000)
        if best is None or res.cost < best.cost:
            best = res
    chi2 = 2 * best.cost
    dof = len(Lam) - np_ + (1 if nI == 0 else 0)
    return best.x, chi2, dof, float(stats.chi2.sf(chi2, dof))


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    T = json.loads(Path(I["tables"]).read_text())
    pts = [p for p in I["points"] if p and p.get("Lambda")]
    L = np.array([p["L"] for p in pts], float)
    W = np.array([p["W"] for p in pts], float)
    Lam = np.array([p["Lambda"] for p in pts])
    err = np.array([p["Lambda_err"] for p in pts])
    rng = np.random.default_rng(1)
    out, L_md = {}, []
    for name, form, sel in (("corrections", (3, 1, 2, 0), np.ones(len(L), bool)),
                            ("no_corrections_L8", (3, 0, 2, 0), (L >= 8) & (W >= 16) & (W <= 17))):
        x, chi2, dof, Q = fit(L[sel], W[sel], Lam[sel], err[sel], form, rng=rng)
        boot = []
        for b in range(200):
            Lb = Lam[sel] + rng.normal(0, err[sel])
            xb = fit(L[sel], W[sel], Lb, err[sel], form, p0=x, starts=1, rng=rng)[0]
            boot.append(xb[:4])
        boot = np.array(boot)
        lo, hi = np.percentile(boot, [2.5, 97.5], axis=0)
        out[name] = {"form": form, "n_data": int(sel.sum()), "n_params": nparams(*form), "chi2": chi2, "dof": dof,
                     "Q": Q, "Wc": [x[0], lo[0], hi[0]], "nu": [x[1], lo[1], hi[1]],
                     "Lc": [x[3], lo[3], hi[3]], "y": [x[2], lo[2], hi[2]] if form[1] else None, "params": x.tolist()}
    claims = []
    t2, t3 = T["table2"]["B"], T["table3"]["B"]
    for key, label in (("Wc", "W_c"), ("nu", "nu"), ("Lc", "Lambda_c")):
        ref, mine = t2[key], out["corrections"][key]
        overlap = mine[1] <= ref["hi"] and mine[2] >= ref["lo"]
        claims.append({"claim": f"{label} with corrections to scaling (Table II, box)",
                       "paper": "%g [%g, %g]" % (ref["value"], ref["lo"], ref["hi"]),
                       "this_run": "%.4f [%.4f, %.4f]" % tuple(mine), "ok": bool(overlap)})
    for key, label in (("Wc", "W_c"), ("nu", "nu")):
        ref, mine = t3[key], out["no_corrections_L8"][key]
        overlap = mine[1] <= ref["hi"] and mine[2] >= ref["lo"]
        claims.append({"claim": f"{label} without corrections, L >= 8, W in [16, 17] (Table III, box)",
                       "paper": "%g [%g, %g]" % (ref["value"], ref["lo"], ref["hi"]),
                       "this_run": "%.4f [%.4f, %.4f]" % tuple(mine), "ok": bool(overlap)})
    c = out["corrections"]
    claims.append({"claim": "an acceptable fit with corrections (goodness of fit Q > 0.1)", "paper": "Q = 0.5",
                   "this_run": "Q = %.2f (chi2 %.0f for %d dof)" % (c["Q"], c["chi2"], c["dof"]), "ok": c["Q"] > 0.1})
    n_ok = sum(x["ok"] for x in claims)
    Lines = ["# 3D Anderson transition, box distribution (Slevin & Ohtsuki 1999): %d of %d claims" % (n_ok, len(claims)),
             "", "| claim | paper | this run | |", "|---|---|---|---|"]
    Lines += ["| %s | %s | %s | %s |" % (x["claim"], x["paper"], x["this_run"], "✓" if x["ok"] else "✗") for x in claims]
    Lines += ["", "Data: %d points, L = %s, W = %g..%g, median relative error of Lambda %.2f %%." % (
        len(L), ", ".join(str(int(v)) for v in sorted(set(L))), W.min(), W.max(), 100 * np.median(err / Lam))]
    Path("report.md").write_text("\n".join(Lines) + "\n")
    json.dump({"claims": claims, "fits": out}, open("fss.json", "w"), indent=1, default=float)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
    p, form = np.array(c["params"]), c["form"]
    for Lv in sorted(set(L)):
        m = L == Lv
        ax[0].errorbar(W[m], Lam[m], err[m], fmt="o", ms=3, label=f"L={int(Lv)}")
        ws = np.linspace(W.min(), W.max(), 200)
        ax[0].plot(ws, model(p, Lv, ws, *form), "-", lw=0.8, color=ax[0].lines[-1].get_color())
    ax[0].set_xlabel("W"); ax[0].set_ylabel("Lambda"); ax[0].legend(fontsize=7)
    Wc, nu = c["Wc"][0], c["nu"][0]
    ax[1].plot((W - Wc) / Wc * L ** (1 / nu), Lam, ".", ms=3)
    ax[1].set_xlabel("w L^{1/nu}"); ax[1].set_ylabel("Lambda (uncorrected)")
    fig.tight_layout(); fig.savefig("fss.png", dpi=130)
    print(json.dumps({"n_claims": len(claims), "n_reproduced": n_ok, "Wc": c["Wc"], "nu": c["nu"], "Lc": c["Lc"],
                      "Q": c["Q"], "n_points": len(L)}))


def selftest():
    rng = np.random.default_rng(5)
    form = (3, 1, 2, 0)
    # Wc, nu, y, a_00, a_02, a_03, a_10..a_13, b_1, b_2  (a_01 = 1 and c_0 = 1 fixed)
    true = np.array([16.54, 1.57, -2.8, 0.576, 0.05, 0.01, 0.4, 0.2, 0.05, 0.0, -0.5, 0.1])   # Lambda 0.3..0.85
    Ls = np.array([4, 5, 6, 8, 10, 12, 14], float)
    Ws = np.linspace(15, 18, 13)
    L, W = np.meshgrid(Ls, Ws)
    L, W = L.ravel(), W.ravel()
    exact = model(true, L, W, *form)
    assert exact.min() > 0.1, exact.min()
    err = 0.001 * exact
    Lam = exact + rng.normal(0, err)
    x, chi2, dof, Q = fit(L, W, Lam, err, form, rng=rng)
    boot = np.array([fit(L, W, Lam + rng.normal(0, err), err, form, p0=x, starts=1, rng=rng)[0][:2] for _ in range(100)])
    lo, hi = np.percentile(boot, [2.5, 97.5], axis=0)
    ok = bool(lo[0] <= true[0] <= hi[0] and lo[1] <= true[1] <= hi[1])
    print(json.dumps({"ok": ok, "Wc": [x[0], lo[0], hi[0]], "nu": [x[1], lo[1], hi[1]], "Q": Q}))
    if not ok:
        raise SystemExit("the fit does not recover the parameters it was given")


if __name__ == "__main__":
    import sys
    selftest() if sys.argv[1:] == ["selftest"] else main()
