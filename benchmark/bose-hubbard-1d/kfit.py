"""The Luttinger parameter K from the decay of Gamma(r) = <b+ b> ~ r^(-K/2), and the Kosterlitz-Thouless point
where K = 1/2, as Kuhner, White and Monien (Sec. V, Table I).

  python3 kfit.py      FLOWER_INPUTS {"runs": [step outputs with local_dir holding gamma.npz]}
For every (L, t) and fit interval r1 <= r <= r2: K = -2 d ln Gamma / d ln r (least squares). Per t, K from the two
biggest systems: K_u = K(biggest), K_l = the linear extrapolation in 1/L, K = (K_u + K_l)/2, error (K_u - K_l)/2
(the paper's estimate). t_c per interval where K(t) = 1/2 (linear interpolation between the bracketing t).
Writes kfit.json, report.md, kfit.png; prints the outputs JSON.
"""
import json
import os
from pathlib import Path

import numpy as np

INTERVALS = [(4, 8), (8, 16), (16, 32), (32, 48), (48, 64)]
PAPER = {(4, 8): (0.2874, 0.0001), (8, 16): (0.2938, 0.0001), (16, 32): (0.2968, 0.0003),
         (32, 48): (0.3062, 0.0003), (48, 64): (0.3107, 0.01)}
PAPER_TC = (0.297, 0.01)


def load(runs):
    data = {}
    for it in runs:
        d = Path((it or {}).get("local_dir") or "")
        if (d / "gamma.npz").is_file():
            z = np.load(d / "gamma.npz")
            data[(int(z["L"]), round(float(z["t"]), 6))] = (z["r"], z["gamma"])
    return data


def kfit(r, g, r1, r2):
    sel = (r >= r1) & (r <= r2) & (g > 0)
    if sel.sum() < 3:
        return None
    slope = np.polyfit(np.log(r[sel]), np.log(g[sel]), 1)[0]
    return float(-2 * slope)


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    data = load(I.get("runs") or [])
    ts = sorted({t for _, t in data})
    table = {}
    for (r1, r2) in INTERVALS:
        row = {}
        for t in ts:
            Ls = sorted(L for L, tt in data if tt == t and L >= 2 * r2 + 8)   # pairs must stay off the ends
            ks = {L: kfit(*data[(L, t)], r1, r2) for L in Ls}
            ks = {L: k for L, k in ks.items() if k is not None}
            if not ks:
                continue
            Lb = sorted(ks)
            ku = ks[Lb[-1]]
            if len(Lb) >= 2:
                L1, L2 = Lb[-2], Lb[-1]
                kl = ks[L2] - (ks[L1] - ks[L2]) * (1 / L2) / (1 / L1 - 1 / L2)
            else:
                kl = ku
            row[t] = {"K": (ku + kl) / 2, "err": abs(ku - kl) / 2, "K_by_L": ks}
        table["%d-%d" % (r1, r2)] = row
    tcs = {}
    for (r1, r2) in INTERVALS:
        row = table["%d-%d" % (r1, r2)]
        tt = sorted(row)
        tc = None
        for a, b in zip(tt, tt[1:]):
            ka, kb = row[a]["K"], row[b]["K"]
            if (ka - 0.5) * (kb - 0.5) <= 0 and ka != kb:
                tc = a + (0.5 - ka) * (b - a) / (kb - ka)
                break
        tcs["%d-%d" % (r1, r2)] = tc
    claims = []
    for (r1, r2) in INTERVALS:
        key = "%d-%d" % (r1, r2)
        ref, err = PAPER[(r1, r2)]
        tc = tcs[key]
        ok = tc is not None and abs(tc - ref) <= max(3 * err, 0.005)
        claims.append({"claim": "t_c from %d <= r <= %d" % (r1, r2), "paper": "%.4f +- %g" % (ref, err),
                       "this_run": "%.4f" % tc if tc is not None else "no crossing", "ok": bool(ok)})
    best = tcs.get("16-32")
    claims.append({"claim": "t_c (the paper's final estimate)", "paper": "0.297 +- 0.01",
                   "this_run": "%.4f (16 <= r <= 32)" % best if best else "no crossing",
                   "ok": bool(best is not None and abs(best - PAPER_TC[0]) <= PAPER_TC[1])})
    json.dump({"K": {k: {str(t): v for t, v in row.items()} for k, row in table.items()}, "t_c": tcs,
               "claims": claims}, open("kfit.json", "w"), indent=1, default=str)
    report(claims, table, data)
    figure(table)
    return {"n_claims": len(claims), "n_reproduced": sum(c["ok"] for c in claims), "t_c": tcs,
            "points": len(data)}


def report(claims, table, data):
    L = ["# 1D Bose-Hubbard KT point (Kuhner, White, Monien 2000): %d of %d claims reproduced" % (
        sum(c["ok"] for c in claims), len(claims)), "", "| claim | paper | this run | |", "|---|---|---|---|"]
    L += ["| %s | %s | %s | %s |" % (c["claim"], c["paper"], c["this_run"], "✓" if c["ok"] else "✗") for c in claims]
    L += ["", "Systems: " + ", ".join("L=%d t=%g" % k for k in sorted(data)), "",
          "| t | " + " | ".join("K (%s)" % k for k in table) + " |", "|---|" + "---|" * len(table)]
    ts = sorted({t for row in table.values() for t in row})
    for t in ts:
        L.append("| %g | " % t + " | ".join(
            "%.3f +- %.3f" % (table[k][t]["K"], table[k][t]["err"]) if t in table[k] else "" for k in table) + " |")
    Path("report.md").write_text("\n".join(L) + "\n")


def figure(table):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    for k, row in table.items():
        ts = sorted(row)
        ax.errorbar(ts, [row[t]["K"] for t in ts], [row[t]["err"] for t in ts], marker="o", label="r = " + k)
    ax.axhline(0.5, color="k", lw=0.8, ls="--")
    ax.set(xlabel="t / U", ylabel="K", title="Luttinger parameter at density 1")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig("kfit.png", dpi=110)


if __name__ == "__main__":
    print(json.dumps(main(), default=str))
