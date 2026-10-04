"""Finite-size data and extrapolations of the SSE runs, against Sandvik, PRB 56, 11678 (1997).

  python3 fits.py      FLOWER_INPUTS {"runs": [step outputs with local_dir holding bins.npz], "table2": path,
                                      "lmin": smallest L in the fits (the paper: 6)}
Writes fits.json, report.md, fits.png; prints the outputs JSON.

Per size: the bins of all chains together (mean, standard error), compared with the paper's Table II. Fits, as the
paper's unconstrained ones (Sec. III, Eq. 39): E(L) = E + e3/L^3 + e4/L^4 + e5/L^5; M^2(L) from 3 S(pi,pi)/L^2 and
from 3 C(L/2,L/2), each M^2 + m1/L + m2/L^2 + m3/L^3; rho_s(L) and chi(L) linear + quadratic in 1/L;
c = sqrt(rho_s / chi).
"""
import json
import os
from pathlib import Path

import numpy as np
from scipy.optimize import curve_fit

PAPER = {"E": (-0.669437, 0.000005), "M": (0.3070, 0.0003), "rho_s": (0.175, 0.002), "chi": (0.0625, 0.0009),
         "c": (1.673, 0.007)}
PAPER_FREE = {"E": (-0.66943, 0.00002), "M1": (0.3062, 0.0006), "M2": (0.3068, 0.0009), "rho_s": (0.179, 0.004),
              "chi": (0.063, 0.001), "c": (1.69, 0.02)}


def load(runs):
    by = {}
    for it in runs:
        d = Path((it or {}).get("local_dir") or "")
        if (d / "bins.npz").is_file():
            z = np.load(d / "bins.npz")
            by.setdefault(int(z["L"]), []).append({k: z[k] for k in ("E", "S", "C", "rho", "chi")})
    out = {}
    for L, rs in sorted(by.items()):
        out[L] = {}
        for k in ("E", "S", "C", "rho", "chi"):
            x = np.concatenate([r[k] for r in rs])
            out[L][k] = (float(x.mean()), float(x.std(ddof=1) / np.sqrt(len(x))))
        out[L]["chains"] = len(rs)
    return out


def fit(Ls, y, dy, powers):
    Ls, y, dy = (np.asarray(v, dtype=float) for v in (Ls, y, dy))
    f = lambda L, *p: p[0] + sum(c * L ** -k for c, k in zip(p[1:], powers))
    p, cov = curve_fit(f, Ls, y, sigma=dy, absolute_sigma=True, p0=[y[-1]] + [0.0] * len(powers))
    chi2 = float(np.sum(((f(Ls, *p) - y) / dy) ** 2))
    return p, np.sqrt(np.diag(cov)), chi2 / max(1, len(Ls) - len(p))


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    lmin = int(I.get("lmin", 6))
    data = load(I.get("runs") or [])
    t2 = {int(k): v for k, v in json.loads(Path(I["table2"]).read_text()).items()}
    # --- per size against Table II
    per = []
    for L, d in data.items():
        if L in t2:
            for k in ("E", "S", "C"):
                ref, rerr = t2[L][k], t2[L][k + "_err"]
                z = (d[k][0] - ref) / np.hypot(d[k][1], rerr)
                per.append({"L": L, "q": k, "this": d[k][0], "err": d[k][1], "paper": ref, "paper_err": rerr,
                            "z": float(z)})
    # --- fits
    Ls = [L for L in data if L >= lmin]
    if len(Ls) < 5:      # a preview before enough sizes are done: E needs 4 parameters
        Path("report.md").write_text("Not enough sizes yet: %s\n" % sorted(data))
        return {"sizes": sorted(data)}
    res = {}
    p, e, c2 = fit(Ls, [data[L]["E"][0] for L in Ls], [data[L]["E"][1] for L in Ls], (3, 4, 5))
    res["E"] = (p[0], e[0], c2)
    for name, getter in (("M1", lambda L: (3 * data[L]["S"][0] / L ** 2, 3 * data[L]["S"][1] / L ** 2)),
                         ("M2", lambda L: (3 * data[L]["C"][0], 3 * data[L]["C"][1]))):
        y = [getter(L) for L in Ls]
        p, e, c2 = fit(Ls, [v[0] for v in y], [v[1] for v in y], (1, 2, 3))
        res[name] = (float(np.sqrt(p[0])), float(e[0] / (2 * np.sqrt(p[0]))), c2)
    for name, key in (("rho_s", "rho"), ("chi", "chi")):
        Lq = [L for L in Ls if L < 16] if name == "chi" or name == "rho_s" else Ls   # the paper drops L = 16 here
        p, e, c2 = fit(Lq, [data[L][key][0] for L in Lq], [data[L][key][1] for L in Lq], (1, 2))
        res[name] = (p[0], e[0], c2)
    c = float(np.sqrt(res["rho_s"][0] / res["chi"][0]))
    dc = c / 2 * float(np.hypot(res["rho_s"][1] / res["rho_s"][0], res["chi"][1] / res["chi"][0]))
    res["c"] = (c, dc, None)
    claims = []
    n_ok_t2 = sum(abs(x["z"]) < 3 for x in per)
    claims.append({"claim": "Table II: E, S(pi,pi), C(L/2,L/2) for L = 4..16", "paper": "21 values",
                   "this_run": "%d/%d within 3 sigma (worst |z| %.1f)" % (n_ok_t2, len(per),
                                                                         max((abs(x["z"]) for x in per), default=0)),
                   "ok": n_ok_t2 == len(per) and len(per) == 21})
    for k, (val, err, c2) in res.items():
        ref_key = "M" if k in ("M1", "M2") else k
        ref, rerr = PAPER[ref_key]
        fref = PAPER_FREE.get(k)
        z = (val - ref) / np.hypot(err, rerr)
        claims.append({"claim": {"E": "E (infinite L)", "M1": "M from S(pi,pi)", "M2": "M from C(L/2,L/2)",
                                 "rho_s": "spin stiffness rho_s", "chi": "susceptibility chi",
                                 "c": "spin-wave velocity c = sqrt(rho_s/chi)"}[k],
                       "paper": "%g(%g); unconstrained fit %s" % (ref, rerr, "%g(%g)" % fref if fref else "-"),
                       "this_run": "%.6g +- %.2g%s" % (val, err, "" if c2 is None else " (chi2/dof %.2f)" % c2),
                       "z": float(z), "ok": bool(abs(z) < 3)})
    json.dump({"per_size": per, "fits": res, "claims": claims, "data": data}, open("fits.json", "w"), indent=1)
    report(claims, per, data, lmin)
    figure(data, res, t2)
    return {"n_claims": len(claims), "n_reproduced": sum(c["ok"] for c in claims), "E_inf": res["E"][0],
            "E_inf_err": res["E"][1], "M": res["M1"][0], "M_err": res["M1"][1], "sizes": sorted(data)}


def report(claims, per, data, lmin):
    L = ["# 2D Heisenberg antiferromagnet (Sandvik 1997): %d of %d claims reproduced" % (
        sum(c["ok"] for c in claims), len(claims)), "", "| claim | paper | this run | |", "|---|---|---|---|"]
    L += ["| %s | %s | %s | %s |" % (c["claim"], c["paper"], c["this_run"],
                                    ("✓" if c["ok"] else "✗") + ("" if "z" not in c else " (%.1f sigma)" % c["z"]))
          for c in claims]
    L += ["", "Fits use L >= %d, as the paper's." % lmin, "",
          "| L | chains | E (this run) | E (paper) | S(pi,pi) | S (paper) | C(L/2,L/2) | C (paper) | rho_s | chi |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    t = {(x["L"], x["q"]): x for x in per}
    for Lv, d in data.items():
        def cell(q):
            x = t.get((Lv, q))
            return ("%.6f(%d)" % (d[q][0], round(d[q][1] * 1e6)), "%.6f" % x["paper"] if x else "")
        e, s, cc = cell("E"), cell("S"), cell("C")
        L.append("| %d | %d | %s | %s | %s | %s | %s | %s | %.4f | %.4f |" % (
            Lv, d["chains"], e[0], e[1], s[0], s[1], cc[0], cc[1], d["rho"][0], d["chi"][0]))
    Path("report.md").write_text("\n".join(L) + "\n")


def figure(data, res, t2):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(14, 4))
    Ls = np.array(sorted(data), float)
    ax[0].errorbar(1 / Ls ** 3, [data[int(L)]["E"][0] for L in Ls], [data[int(L)]["E"][1] for L in Ls], fmt="o",
                   label="this run")
    ax[0].plot([1 / L ** 3 for L in t2], [v["E"] for v in t2.values()], "x", label="paper Table II")
    ax[0].axhline(res["E"][0], lw=0.8)
    ax[0].set(xlabel="1/L^3", ylabel="E/N", title="E = %.6f" % res["E"][0])
    ax[0].legend(fontsize=7)
    ax[1].plot(1 / Ls, [3 * data[int(L)]["S"][0] / L ** 2 for L in Ls], "o", label="3 S(pi,pi)/N")
    ax[1].plot(1 / Ls, [3 * data[int(L)]["C"][0] for L in Ls], "s", label="3 C(L/2,L/2)")
    ax[1].axhline(res["M1"][0] ** 2, lw=0.8)
    ax[1].set(xlabel="1/L", ylabel="M^2(L)", title="M = %.4f" % res["M1"][0])
    ax[1].legend(fontsize=7)
    ax[2].plot(1 / Ls, [data[int(L)]["rho"][0] for L in Ls], "o", label="rho_s")
    ax[2].plot(1 / Ls, [data[int(L)]["chi"][0] for L in Ls], "s", label="chi")
    ax[2].set(xlabel="1/L", title="rho_s = %.4f, chi = %.4f" % (res["rho_s"][0], res["chi"][0]))
    ax[2].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig("fits.png", dpi=110)


if __name__ == "__main__":
    print(json.dumps(main(), default=lambda o: o.item() if hasattr(o, "item") else str(o)))
