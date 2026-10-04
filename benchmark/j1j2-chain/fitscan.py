"""Which fitting choices turn the paper's own Table I into its Table II?  Eggert's text says: cubic splines of
r<Sz(0)Sz(r)> through even and odd r, their difference is rG(r), fitted to eq. (14). Not stated: the r range, the
spline end conditions, whether the fit is to rG or (rG)^2, and at which r the curves are compared.

  python3 fitscan.py      FLOWER_INPUTS {"table1": path}; writes scan.json and prints the best choices
"""
import itertools
import json
import os
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.optimize import curve_fit

TABLE2 = {-0.25: (-1.4976, 0.021601), 0.0: (-2.9501, 0.010696), 0.1: (-4.6883, 0.006546), 0.2: (-14.393, 0.002080)}


def fit(r, szsz, rmin, rmax, bc, target, grid):
    r, y = np.asarray(r), np.asarray(r) * np.asarray(szsz)
    ev, od = r % 2 == 0, r % 2 == 1
    se, so = CubicSpline(r[ev], y[ev], bc_type=bc), CubicSpline(r[od], y[od], bc_type=bc)
    rr = np.arange(rmin, rmax + 1) if grid == "integer" else np.linspace(rmin, rmax, 200)
    g = (se(rr) - so(rr)) / 2
    if target == "(rG)^2 linear":
        slope, icpt = np.polyfit(np.log(rr), g ** 2, 1)
        return slope, -icpt / slope
    f = lambda x, al, lnr0: np.sqrt(np.clip(al * (np.log(x) - lnr0), 0, None))
    (al, lnr0), _ = curve_fit(f, rr, g, p0=(0.01, -3.0), maxfev=20000)
    return al, lnr0


def main():
    t = json.loads(Path(json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())["table1"]).read_text())
    cols = {c: j for j, c in enumerate(t["columns"]) if c != "crit"}
    rows = []
    for rmin, rmax, bc, target, grid in itertools.product(range(4, 10), (14, 15, 16), ("not-a-knot", "natural"),
                                                          ("rG", "(rG)^2 linear"), ("integer", "dense")):
        if rmax - rmin < 4:
            continue
        try:
            res = {j2: fit(t["r"], [v[cols[j2]] for v in t["values"]], rmin, rmax, bc, target, grid) for j2 in TABLE2}
        except (RuntimeError, ValueError):
            continue
        err_ln = max(abs(res[j][1] - TABLE2[j][0]) / abs(TABLE2[j][0]) for j in TABLE2 if j != 0.2)
        err_al = max(abs(res[j][0] - TABLE2[j][1]) / TABLE2[j][1] for j in TABLE2)
        rows.append({"rmin": rmin, "rmax": rmax, "spline": bc, "fit": target, "points": grid,
                     "max_rel_err_ln_r0": err_ln, "max_rel_err_a_lambda0": err_al,
                     "ln_r0": {str(j): round(float(res[j][1]), 4) for j in res},
                     "a_lambda0": {str(j): round(float(res[j][0]), 6) for j in res}})
    rows.sort(key=lambda x: x["max_rel_err_ln_r0"] + x["max_rel_err_a_lambda0"])
    json.dump(rows, open("scan.json", "w"), indent=1)
    best = rows[0]
    return {"n_choices": len(rows), "best": {k: best[k] for k in ("rmin", "rmax", "spline", "fit", "points")},
            "best_err_ln_r0": best["max_rel_err_ln_r0"], "best_err_a_lambda0": best["max_rel_err_a_lambda0"],
            "top5": [{k: x[k] for k in ("rmin", "rmax", "spline", "fit", "points", "max_rel_err_ln_r0",
                                        "max_rel_err_a_lambda0")} for x in rows[:5]]}


if __name__ == "__main__":
    print(json.dumps(main(), default=lambda o: o.item()))
