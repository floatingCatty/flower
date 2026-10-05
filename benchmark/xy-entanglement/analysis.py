"""The paper's entanglement laws against this run's S_L of the infinite XY chain (Vidal, Latorre, Rico, Kitaev 2003).

  python3 analysis.py     FLOWER_INPUTS {"curves": [curves step outputs: g, h, L, S]}

Claims, each fitted on 20 <= L <= 200 where a curve is used (XX chains with their Fermi-surface oscillation
cos(2 kF L)/L, kF = arccos h, as a fitted correction):
  Eq. (14) XX chain (gamma 0), h = 0 and 0.5: S_L = log2(L)/3 + k1(a), k1 depending on the field.
  Eq. (15) critical Ising (gamma 1, h 1): S_L = log2(L)/6 + k2, with k2 ~ pi/3 as printed.
  Eq. (16) Ising off criticality, a close to 1: the saturated entropy grows as log2(1/|1 - a|)/6.
  Eq. (18) critical field, anisotropy gamma: S_L(1) - S_L(gamma) -> -log2(gamma)/6 (c = cbar = 1/2).
Writes report.md, analysis.json, entropy.png; prints the outputs JSON.
"""
import json
import os
from pathlib import Path

import numpy as np


def fit(L, S, lo=20, hi=200, kF=None):
    """slope and constant of S = slope log2 L + k [+ b cos(2 kF L) / L, the Fermi-surface oscillation of XX chains]."""
    L, S = np.asarray(L, float), np.asarray(S)
    m = (L >= lo) & (L <= hi)
    cols = [np.log2(L[m]), np.ones(m.sum())] + ([np.cos(2 * kF * L[m]) / L[m]] if kF is not None else [])
    x = np.linalg.lstsq(np.array(cols).T, S[m], rcond=None)[0]
    return float(x[0]), float(x[1])


def saturated_ctm(a):
    """S of a block deep in the gapped (paramagnetic) phase, L -> infinity: twice the half-chain entropy, whose
    entanglement spectrum is eps_l = (2l + 1) eps, eps = pi K(k')/K(k), k = a (Peschel's corner transfer matrix
    result for the transverse Ising chain with field 1/a > 1)."""
    from scipy.special import ellipk
    eps = np.pi * ellipk(1 - a * a) / ellipk(a * a)
    e = (2 * np.arange(4000) + 1) * eps
    p = 1 / (1 + np.exp(np.minimum(e, 700)))
    q = 1 - p
    h2 = -(p * np.log2(np.where(p > 0, p, 1)) + q * np.log2(np.where(q > 0, q, 1)))
    return float(2 * h2.sum())


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    cv = [c for c in I["curves"] if c]
    get = lambda g, h: next((c for c in cv if abs(c["g"] - g) < 1e-9 and abs(c["h"] - h) < 1e-9), None)
    claims, out = [], {}
    xx0, xx5, ising = get(0.0, 0.0), get(0.0, 0.5), get(1.0, 1.0)
    s0, k0 = fit(xx0["L"], xx0["S"], kF=np.arccos(0.0))
    s5, k5 = fit(xx5["L"], xx5["S"], kF=np.arccos(0.5))
    si, ki = fit(ising["L"], ising["S"])
    out.update(xx_h0=(s0, k0), xx_h05=(s5, k5), ising=(si, ki))
    claims.append({"claim": "Eq. (14) XX chain, h = 0: slope of S_L in log2 L", "paper": "1/3 = 0.3333",
                   "this_run": "%.4f (k1 = %.4f)" % (s0, k0), "ok": bool(abs(s0 - 1 / 3) < 0.005)})
    claims.append({"claim": "Eq. (14) XX chain, h = 0.5: slope; k1 depends on the field", "paper": "1/3; k1(a)",
                   "this_run": "%.4f (k1 = %.4f, %+.4f from h = 0)" % (s5, k5, k5 - k0),
                   "ok": bool(abs(s5 - 1 / 3) < 0.005 and abs(k5 - k0) > 0.01)})
    claims.append({"claim": "Eq. (15) critical Ising: slope of S_L in log2 L", "paper": "1/6 = 0.1667",
                   "this_run": "%.4f" % si, "ok": bool(abs(si - 1 / 6) < 0.005)})
    claims.append({"claim": "Eq. (15) critical Ising: the constant k2", "paper": "pi/3 = %.4f" % (np.pi / 3),
                   "this_run": "%.4f (the XX constant k1(h = 0) is %.4f)" % (ki, k0), "ok": bool(abs(ki - np.pi / 3) < 0.02)})
    # Eq. (16): saturation near a = 1 (h = 1/a > 1), the largest block computed
    sat = sorted(((1 / c["h"], c["S"][-1], c["L"][-1]) for c in cv if c["g"] == 1.0 and c["h"] > 1.0 and len(c["L"]) <= 5))
    a = np.array([x[0] for x in sat]); S = np.array([x[1] for x in sat])
    conv = [float(c["S"][-1] - c["S"][-2]) for c in cv if c["g"] == 1.0 and c["h"] > 1.0 and len(c["L"]) <= 5]
    ctm_dev = max(abs(saturated_ctm(x) - y) for x, y in zip(a, S))
    claims.append({"claim": "the saturated entropies (L = 2000, a = 0.9-0.995) equal the corner-transfer-matrix "
                            "formula", "paper": "-", "this_run": "max deviation %.1e" % ctm_dev, "ok": bool(ctm_dev < 1e-4)})
    aa = 1 - 10.0 ** -np.arange(2, 9)
    Sa = np.array([saturated_ctm(x) for x in aa])
    local = np.diff(Sa) / np.diff(np.log2(1 / (1 - aa)))          # slope between successive decades of 1 - a
    slope16 = float(local[-1])
    claims.append({"claim": "Eq. (16) Ising, a -> 1: saturated S vs log2(1/|1-a|), slope (1-a = 1e-7 to 1e-8)",
                   "paper": "1/6 = 0.1667", "this_run": "%.4f (1-a = 1e-2 to 1e-3: %.4f; this run's L = 2000 points "
                   "over 0.98-0.995: %.4f)" % (slope16, local[0], float(np.polyfit(np.log2(1 / np.abs(1 - a[-3:])), S[-3:], 1)[0])),
                   "ok": bool(abs(slope16 - 1 / 6) < 0.002)})
    # Eq. (18): S_L(gamma = 1) - S_L(gamma) at L = 200 vs -log2(gamma)/6
    devs = []
    for g in (0.25, 0.5, 2.0):
        c = get(g, 1.0)
        d = ising["S"][-1] - c["S"][-1]
        devs.append((g, d, -np.log2(g) / 6))
    worst = max(abs(d - p) for g, d, p in devs)
    claims.append({"claim": "Eq. (18) critical field: S_L(1) - S_L(gamma) at L = 200, gamma = 0.25, 0.5, 2",
                   "paper": "-log2(gamma)/6 = " + ", ".join("%.4f" % p for g, d, p in devs),
                   "this_run": ", ".join("%.4f" % d for g, d, p in devs), "ok": bool(worst < 0.002)})
    n_ok = sum(c["ok"] for c in claims)
    Lines = ["# Entanglement in quantum critical phenomena (Vidal, Latorre, Rico, Kitaev 2003): %d of %d claims"
             % (n_ok, len(claims)), "", "| claim | paper | this run | |", "|---|---|---|---|"]
    Lines += ["| %s | %s | %s | %s |" % (c["claim"], c["paper"], c["this_run"], "✓" if c["ok"] else "✗") for c in claims]
    Lines += ["", "Saturated entropies (Eq. 16): " + ", ".join("a = %g: %.4f" % (x[0], x[1]) for x in sat)]
    Path("report.md").write_text("\n".join(Lines) + "\n")
    json.dump({"claims": claims, "fits": out}, open("analysis.json", "w"), indent=1)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for c, lab in ((xx0, "XX, h = 0"), (xx5, "XX, h = 0.5"), (ising, "Ising, critical")):
        ax.plot(np.log2(c["L"]), c["S"], ".", ms=3, label=lab)
    ax.set_xlabel("log2 L"); ax.set_ylabel("S_L (bits)"); ax.legend()
    fig.tight_layout(); fig.savefig("entropy.png", dpi=130)
    print(json.dumps({"n_claims": len(claims), "n_reproduced": n_ok, "slope_xx": s0, "slope_ising": si,
                      "k1_h0": k0, "k2": ki, "slope_eq16": slope16}))


if __name__ == "__main__":
    main()
