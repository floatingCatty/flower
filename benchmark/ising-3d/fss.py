"""Finite-size scaling of the Wolff time series, following Ferrenberg, Xu and Landau, PRE 97, 043301 (2018).

  python3 fss.py     FLOWER_INPUTS {"runs": [step outputs with local_dir holding series.npz], "omega": 0.83,
                                    "lmin": smallest L in the fits}
Writes fss.json, report.md, fss.png; prints the outputs JSON.

Single-histogram reweighting in K around K0. For each L: the maxima (golden-section search) of
d ln<|m|>/dK, d ln<m^2>/dK, dU4/dK, dU2/dK give nu from  max = a L^(1/nu) (1 + b L^-omega); the locations of those
maxima and of the specific heat and the finite-lattice susceptibility give Kc from
K(L) = Kc + A L^(-1/nu) (1 + B L^-omega) at our nu. Errors: jackknife over blocks, leaving one block out of every
lattice size at once, so the cross-correlations between estimates from the same data are carried into the errors
of their averages (the paper's point, Weigel and Janke).
"""
import json
import os
from pathlib import Path

import numpy as np
from scipy.optimize import curve_fit, minimize_scalar

PAPER = {"Kc": (0.221654626, 0.000000005), "nu": (0.629912, 0.000086)}
NB = 100         # jackknife blocks per lattice size (enough to estimate the covariance of 4-6 estimates)
QUANTS = ["dlnm1", "dlnm2", "dU4", "dU2"]
PEAKS = QUANTS + ["C", "chi"]


class Series:
    def __init__(self, L, K0, E, M, mask=None):
        self.L, self.K0, self.N = L, K0, L ** 3
        self.E, self.m = E.astype(float), np.abs(M).astype(float) / L ** 3
        self.mask = mask

    def avg(self, K, sel):
        e, m = self.E[sel], self.m[sel]
        lw = -(K - self.K0) * e
        w = np.exp(lw - lw.max())
        w /= w.sum()
        a = lambda x: float(np.dot(w, x))
        E1, E2 = a(e), a(e * e)
        m1, m2, m4 = a(m), a(m * m), a(m ** 4)
        mE1, m2E = a(m * e), a(m * m * e)
        m4E = a(m ** 4 * e)
        return dict(E1=E1, E2=E2, m1=m1, m2=m2, m4=m4, mE1=mE1, m2E=m2E, m4E=m4E)

    def quantity(self, q, K, sel):
        a = self.avg(K, sel)
        if q == "dlnm1":
            return a["E1"] - a["mE1"] / a["m1"]
        if q == "dlnm2":
            return a["E1"] - a["m2E"] / a["m2"]
        if q == "dU4":    # U4 = 1 - <m4>/(3<m2>^2); d<X>/dK = <X><E> - <XE>
            d4 = a["m4"] * a["E1"] - a["m4E"]
            d2 = a["m2"] * a["E1"] - a["m2E"]
            return -(d4 * a["m2"] - 2 * a["m4"] * d2) / (3 * a["m2"] ** 3)
        if q == "dU2":    # U2 = 1 - <m2>/(3<|m|>^2)
            d2 = a["m2"] * a["E1"] - a["m2E"]
            d1 = a["m1"] * a["E1"] - a["mE1"]
            return -(d2 * a["m1"] - 2 * a["m2"] * d1) / (3 * a["m1"] ** 3)
        if q == "C":
            return K * K * (a["E2"] - a["E1"] ** 2) / self.N
        if q == "chi":
            return K * self.N * (a["m2"] - a["m1"] ** 2)
        if q == "U4":
            return 1 - a["m4"] / (3 * a["m2"] ** 2)
        raise KeyError(q)

    def peak(self, q, sel):
        w = 2.5 / self.E[sel].std()       # where single-histogram reweighting is reliable (~1/sigma_E)
        r = minimize_scalar(lambda k: -abs(self.quantity(q, k, sel)), bounds=(self.K0 - w, self.K0 + w),
                            method="bounded", options={"xatol": 1e-10})
        return float(r.x), float(-r.fun)


MOMENTS = ("1", "E", "E2", "m", "m2", "m4", "mE", "m2E", "m4E")
NGRID = 241


class Grid:
    """Reweighted moments of one lattice size on a fine K grid, summed per jackknife block: every leave-one-out
    estimate then costs a subtraction, so many blocks are affordable."""

    def __init__(self, s, blocks, nb):
        self.s, self.nb = s, nb
        w = 2.5 / s.E.std()
        self.K = np.linspace(s.K0 - w, s.K0 + w, NGRID)
        order = np.argsort(blocks, kind="stable")
        e, m = s.E[order], s.m[order]
        starts = np.searchsorted(blocks[order], np.arange(nb))
        cols = {"1": np.ones_like(e), "E": e, "E2": e * e, "m": m, "m2": m * m, "m4": m ** 4, "mE": m * e,
                "m2E": m * m * e, "m4E": m ** 4 * e}
        self.S = np.empty((NGRID, len(MOMENTS), nb))
        for k, K in enumerate(self.K):
            lw = -(K - s.K0) * e
            wt = np.exp(lw - lw.max())
            for j, name in enumerate(MOMENTS):
                self.S[k, j] = np.add.reduceat(wt * cols[name], starts)
        self.S /= self.S[:, :1, :].sum(axis=2, keepdims=True)          # common scale per K (ratios unaffected)

    def curves(self, drop=None):
        tot = self.S.sum(axis=2) if drop is None else self.S.sum(axis=2) - self.S[:, :, drop % self.nb]
        a = {name: tot[:, j] / tot[:, 0] for j, name in enumerate(MOMENTS)}
        K, N = self.K, self.s.N
        d4 = a["m4"] * a["E"] - a["m4E"]
        d2 = a["m2"] * a["E"] - a["m2E"]
        d1 = a["m"] * a["E"] - a["mE"]
        return {"dlnm1": a["E"] - a["mE"] / a["m"], "dlnm2": a["E"] - a["m2E"] / a["m2"],
                "dU4": -(d4 * a["m2"] - 2 * a["m4"] * d2) / (3 * a["m2"] ** 3),
                "dU2": -(d2 * a["m"] - 2 * a["m2"] * d1) / (3 * a["m"] ** 3),
                "C": K * K * (a["E2"] - a["E"] ** 2) / N, "chi": K * N * (a["m2"] - a["m"] ** 2),
                "U4": 1 - a["m4"] / (3 * a["m2"] ** 2)}

    def peaks(self, drop=None):
        out = {}
        for q, y in self.curves(drop).items():
            if q == "U4":
                continue
            y = np.abs(y)
            i = int(np.clip(np.argmax(y), 1, len(y) - 2))
            y0, y1, y2 = y[i - 1], y[i], y[i + 1]
            den = y0 - 2 * y1 + y2
            t = 0.5 * (y0 - y2) / den if den != 0 else 0.0          # parabola through the three points
            h = self.K[1] - self.K[0]
            out[q] = (float(self.K[i] + t * h), float(y1 - 0.25 * (y0 - y2) * t))
        return out


def load(runs):
    by = {}
    for it in runs:
        d = Path((it or {}).get("local_dir") or "")
        f = d / "series.npz"
        if f.is_file():
            z = np.load(f)
            by.setdefault(int(z["L"]), []).append((float(z["K0"]), z["E"], z["M"]))
    out = {}
    for L, rs in sorted(by.items()):
        K0 = rs[0][0]
        E = np.concatenate([r[1] for r in rs])
        M = np.concatenate([r[2] for r in rs])
        # blocks: each seed's series cut into equal pieces, NB in total per L
        per = max(1, NB // len(rs))
        blocks = np.concatenate([np.repeat(np.arange(per) + per * i, int(np.ceil(len(r[1]) / per)))[:len(r[1])]
                                 for i, r in enumerate(rs)])
        out[L] = (Series(L, K0, E, M), blocks, per * len(rs))
    return out


_GRIDS = {}


def grids(data):
    if not _GRIDS:
        for L, (s, blocks, nb) in data.items():
            _GRIDS[L] = Grid(s, blocks, nb)
    return _GRIDS


def estimates(data, lmin, omega, drop=None):
    """One full analysis; ``drop``: the block left out of every size (jackknife), None for all data."""
    peaks = {L: g.peaks(drop) for L, g in grids(data).items()}
    Ls = np.array([L for L in data if L >= lmin], float)
    corr = len(Ls) >= 4          # the correction-to-scaling term only when the sizes can carry it
    nus = {}
    for q in QUANTS:
        y = np.array([peaks[int(L)][q][1] for L in Ls])
        if corr:
            f = lambda L, a, inv_nu, b: a * L ** inv_nu * (1 + b * L ** -omega)
            p0 = (y[0] / Ls[0] ** 1.59, 1.59, 0.0)
        else:
            f = lambda L, a, inv_nu: a * L ** inv_nu
            p0 = (y[0] / Ls[0] ** 1.59, 1.59)
        p, _ = curve_fit(f, Ls, y, p0=p0, maxfev=20000)
        nus[q] = 1 / p[1]
    nu = float(np.mean(list(nus.values())))
    kcs = {}
    for q in PEAKS:
        y = np.array([peaks[int(L)][q][0] for L in Ls])
        if corr:
            f = lambda L, kc, A, B: kc + A * L ** (-1 / nu) * (1 + B * L ** -omega)
            p0 = (0.2216546, 0.0, 0.0)
        else:
            f = lambda L, kc, A: kc + A * L ** (-1 / nu)
            p0 = (0.2216546, 0.0)
        p, _ = curve_fit(f, Ls, y, p0=p0, maxfev=20000)
        kcs[q] = p[0]
    return {"peaks": peaks, "nu_each": nus, "nu": nu, "Kc_each": kcs, "Kc": float(np.mean(list(kcs.values())))}


def combine(full_each, jk_each, keys):
    """The paper's combination of estimates from the same data (Weigel and Janke): weights G^-1 1 / (1' G^-1 1)
    with G the jackknife covariance matrix of the estimates; error 1/sqrt(1' G^-1 1)."""
    x = np.array([full_each[k] for k in keys])
    J = np.array([[s[k] for k in keys] for s in jk_each])
    n = len(J)
    d = J - J.mean(axis=0)
    G = (n - 1) / n * d.T @ d
    Gi = np.linalg.pinv(G)
    one = np.ones(len(keys))
    w = Gi @ one / (one @ Gi @ one)
    return float(w @ x), float(1 / np.sqrt(one @ Gi @ one)), {k: float(v) for k, v in zip(keys, w)}


def jackknife(full, samples, key):
    x = np.array([s[key] for s in samples])
    n = len(x)
    return float(np.sqrt((n - 1) / n * np.sum((x - x.mean()) ** 2)))


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    omega, lmin = float(I.get("omega", 0.83)), int(I.get("lmin", 8))
    data = load(I["runs"])
    if len([L for L in data if L >= lmin]) < 2:      # a preview before two sizes are done
        Path("report.md").write_text("Not enough lattice sizes yet: %s\n" % sorted(data))
        Path("fss.png").write_bytes(b"")
        return {"Kc": None, "Kc_err": None, "nu": None, "nu_err": None, "sizes": sorted(data)}
    full = estimates(data, lmin, omega)
    nbl = min(nb for _, _, nb in data.values())
    jk = [estimates(data, lmin, omega, drop=j) for j in range(nbl)]
    # covariance-weighted combinations replace the plain means of `estimates`
    full["nu"], nu_err, w_nu = combine(full["nu_each"], [x["nu_each"] for x in jk], QUANTS)
    full["Kc"], kc_err, w_kc = combine(full["Kc_each"], [x["Kc_each"] for x in jk], PEAKS)
    err = {"nu": nu_err, "Kc": kc_err, "weights_nu": w_nu, "weights_Kc": w_kc}
    # the plain mean over the quantities, its jackknife error (correlations included); for Kc without the
    # specific heat, whose peak is dominated by the analytic background at these sizes
    kq = [q for q in PEAKS if q != "C"]
    plain = {"nu": float(np.mean([full["nu_each"][q] for q in QUANTS])),
             "Kc": float(np.mean([full["Kc_each"][q] for q in kq]))}
    plain_jk = [{"nu": np.mean([x["nu_each"][q] for q in QUANTS]), "Kc": np.mean([x["Kc_each"][q] for q in kq])}
                for x in jk]
    err["plain"] = {k: (plain[k], jackknife(None, plain_jk, k)) for k in ("nu", "Kc")}
    err["unstable"] = any(not -0.05 <= w <= 1.05 for w in list(w_nu.values()) + list(w_kc.values()))
    # how the results depend on the smallest lattice in the fits (all data, plain errors)
    lmin_scan = {}
    for lm in sorted(L for L in data if L >= 8):
        if len([L for L in data if L >= lm]) >= 3:
            e = estimates(data, lm, omega)
            lmin_scan[lm] = {"nu_each": e["nu_each"], "Kc_each": e["Kc_each"]}
    err_each = {"nu": {q: jackknife(None, [{"x": s["nu_each"][q]} for s in jk], "x") for q in QUANTS},
                "Kc": {q: jackknife(None, [{"x": s["Kc_each"][q]} for s in jk], "x") for q in PEAKS}}
    # Binder cumulant at Kc (paper's value) for each L, a check of universality
    u4 = {L: float(np.interp(PAPER["Kc"][0], g.K, g.curves()["U4"])) for L, g in grids(data).items()}
    claims = []
    for k in ("Kc", "nu"):
        ref, rerr = PAPER[k]
        for how, (val, e) in (("covariance-weighted", (full[k], err[k])), ("plain mean", err["plain"][k])):
            z = abs(val - ref) / np.hypot(e, rerr)
            counted = how == "plain mean" or not err["unstable"]
            claims.append({"claim": "%s (%s)" % (k, how), "paper": "%.9g(%s)" % (ref, rerr),
                           "this_run": "%.7g +- %.2g" % (val, e), "z": float(z), "ok": bool(z < 3),
                           "counted": counted})
    json.dump({"full": full, "err": err, "err_each": err_each, "u4_at_Kc": u4, "claims": claims, "lmin_scan": lmin_scan,
               "sizes": {L: int(len(data[L][0].E)) for L in data}}, open("fss.json", "w"), indent=1,
              default=lambda o: o.item() if hasattr(o, "item") else str(o))
    report(full, err, err_each, u4, claims, data, lmin, omega, lmin_scan)
    figure({**full, **{k: (err["plain"][k][0] if err["unstable"] else full[k]) for k in ("Kc", "nu")}}, data, lmin)
    use = err["plain"] if err["unstable"] else {k: (full[k], err[k]) for k in ("Kc", "nu")}
    return {"Kc": use["Kc"][0], "Kc_err": use["Kc"][1], "nu": use["nu"][0], "nu_err": use["nu"][1],
            "combination": "plain mean" if err["unstable"] else "covariance-weighted",
            "n_reproduced": sum(c["ok"] for c in claims if c["counted"]),
            "n_claims": sum(c["counted"] for c in claims),
            "sizes": sorted(data), "measurements": int(sum(len(d[0].E) for d in data.values()))}


def report(full, err, err_each, u4, claims, data, lmin, omega, lmin_scan=None):
    L = ["# 3d Ising critical point (Ferrenberg, Xu, Landau 2018): %d of %d claims reproduced" % (
        sum(c["ok"] for c in claims if c["counted"]), sum(c["counted"] for c in claims)), "",
         "| | paper (L = 16..1024) | this run (L = %s) | deviation |" % ", ".join(map(str, sorted(data))),
         "|---|---|---|---|"]
    for c in claims:
        L.append("| %s | %s | %s | %.1f sigma %s |" % (c["claim"], c["paper"], c["this_run"], c["z"],
                                                       ("✓" if c["ok"] else "✗") if c["counted"]
                                                       else "(not counted: unstable weights)"))
    L += ["", "Fits use L >= %d and the correction exponent omega = %.2f (fixed; with 4 or more sizes)." % (
        lmin, omega), "",
          "Combined with the jackknife covariance matrix of the estimates (weights below), as in the paper, and as a",
          "plain mean with its jackknife error. %s" % (
              "The covariance weights fall outside [0, 1]: that combination extrapolates and its error is not "
              "reliable here." if err["unstable"] else "The covariance weights lie within [0, 1]."), "",
          "| quantity | nu | weight | Kc | weight |", "|---|---|---|---|---|"]
    for q in PEAKS:
        nu = "%.4f +- %.4f | %.2f" % (full["nu_each"][q], err_each["nu"][q], err["weights_nu"][q]) \
            if q in full["nu_each"] else " | "
        L.append("| %s | %s | %.7f +- %.7f | %.2f |" % (q, nu, full["Kc_each"][q], err_each["Kc"][q],
                                                       err["weights_Kc"][q]))
    if lmin_scan:
        L += ["", "Sensitivity to the smallest lattice in the fits (plain mean over the quantities):", "",
              "| L_min | nu | Kc (without C) |", "|---|---|---|"]
        for lm, e in sorted(lmin_scan.items()):
            L.append("| %d | %.4f | %.7f |" % (lm, np.mean(list(e["nu_each"].values())),
                                               np.mean([v for k, v in e["Kc_each"].items() if k != "C"])))
    L += ["", "| L | measurements | U4 at the paper's Kc |", "|---|---|---|"]
    L += ["| %d | %d | %.4f |" % (l, len(data[l][0].E), u4[l]) for l in sorted(data)]
    Path("report.md").write_text("\n".join(L) + "\n")


def figure(full, data, lmin):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(14, 4))
    Ls = np.array(sorted(data), float)
    for q in QUANTS:
        ax[0].loglog(Ls, [full["peaks"][int(L)][q][1] for L in Ls], "o-", label=q)
    ax[0].set(xlabel="L", ylabel="maximum", title="nu = %.4f" % full["nu"])
    ax[0].legend(fontsize=7)
    for q in PEAKS:
        ax[1].plot(Ls ** (-1 / full["nu"]), [full["peaks"][int(L)][q][0] for L in Ls], "o", label=q)
    ax[1].axhline(PAPER["Kc"][0], color="k", lw=0.8, ls="--")
    ax[1].set(xlabel="L^(-1/nu)", ylabel="K at the maximum", title="Kc = %.7f" % full["Kc"])
    ax[1].legend(fontsize=7)
    for L, g in sorted(grids(data).items()):
        ax[2].plot(g.K, g.curves()["U4"], label="L=%d" % L)
    ax[2].axvline(PAPER["Kc"][0], color="k", lw=0.8, ls="--")
    ax[2].set(xlabel="K", ylabel="U4", title="Binder cumulant")
    ax[2].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig("fss.png", dpi=110)


if __name__ == "__main__":
    print(json.dumps(main(), default=lambda o: o.item() if hasattr(o, "item") else str(o)))
