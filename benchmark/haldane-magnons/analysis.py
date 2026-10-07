"""This run's spectrum of the open S = 1 chain against Sorensen & Affleck (1993).

  python3 analysis.py     FLOWER_INPUTS {"levels": [levels step outputs], "claims": claims.json}

The paper's Eq. (8): E_{m+1}(L) - E_1(L) = m Delta + (pi v)^2 / (2 Delta (L-1)^2) sum_{i<=m} n_i^2 + O((L-1)^-3)
for m free-fermion magnons with n_i = 1..m (E_M: the lowest state with S^z = M). Fitted here as
c + a/(L-1)^2 + b/(L-1)^3 over the paper's lengths (L <= 100); the ratios a_31/a_21 and a_41/a_21 are sum n_i^2.
The profiles (Figs. 1-3): <S^z_x>_M - <S^z_x>_1 = 2/(L-1) sum_i sin^2(n_i pi x/(L-1)) at L = 100, and the bond
energies e(x) = Delta times that.

Claims: Table I within 5 units of the paper's last digit (DMRG is variational: the paper's m = 81 states gives upper
bounds); Delta and v, the 2- and 3-magnon constants and coefficients within the paper's error bars (twice for the
coefficients, whose value depends on the fit form, as the paper notes); sum n^2 consistent with 5 and 14; the
profiles within 10 % (bond energies 25 %) of their peak; parities 1-, 2+, 3-, 4+ relative to the 0+ ground state.
Writes report.md, analysis.json, magnons.png; prints the outputs JSON.
"""
import json
import os
from pathlib import Path

import numpy as np


def fit(L, y):
    x = 1.0 / (np.asarray(L, float) - 1)
    A = np.vstack([np.ones_like(x), x ** 2, x ** 3]).T
    coef, res, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    cov = np.linalg.inv(A.T @ A) * (resid @ resid / max(1, len(y) - 3))
    return coef, np.sqrt(np.diag(cov)), float(np.max(np.abs(resid)))


def free_fermion(L, m):
    x = np.arange(L)
    return 2 / (L - 1) * sum(np.sin(n * np.pi * x / (L - 1)) ** 2 for n in range(1, m + 1))


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    C = json.loads(Path(I["claims"]).read_text())
    lv = sorted((r for r in I["levels"] if r), key=lambda r: r["L"])
    L = np.array([r["L"] for r in lv])
    E = np.array([r["E"] for r in lv])          # columns M = 0..4
    claims, out = [], {}

    def claim(name, paper, ours, ok):
        claims.append({"claim": name, "paper": paper, "this_run": ours, "ok": bool(ok)})

    r100 = next(r for r in lv if r["L"] == 100)
    for M in (1, 2, 3, 4):
        p, dig = C["table1"][str(M)], C["table1_digits"][str(M)]
        d = r100["E"][M] - p
        claim(f"Table I: E of the lowest S^z = {M} state, L = 100", f"{p:.{dig}f}", "%.6f (%+.1e)" % (r100["E"][M], d),
              abs(d) <= 5 * 10 ** -dig)
    paper = C["fits"]
    # the asymptotic form needs L well above the correlation length (6) and the magnons' distance from the ends:
    # c + a/(L-1)^2 + b/(L-1)^3 leaves residuals of 3e-4 when L = 20-36 are included; fitted from L = 40
    sel = (L >= 40) & (L <= 100)
    (D20, a20, _), _, r20 = fit(L[L <= 100], E[L <= 100, 2] - E[L <= 100, 1])
    (D, a21, b21), e21, r21 = fit(L[sel], E[sel, 2] - E[sel, 1])
    v = np.sqrt(2 * D * a21) / np.pi
    (c31, a31, b31), e31, r31 = fit(L[sel], E[sel, 3] - E[sel, 1])
    (c41, a41, b41), e41, r41 = fit(L[sel], E[sel, 4] - E[sel, 1])
    out.update(gap=D, a21=a21, v=v, c31=c31, a31=a31, c41=c41, a41=a41, resid=[r21, r31, r41])
    pv = lambda k: paper[k][0]
    pe = lambda k: paper[k][1]
    claim("gap Delta (fit of E_2 - E_1, 40 <= L <= 100)", "0.4107(1)", "%.5f (L >= 20: %.5f)" % (D, D20),
          abs(D - pv("gap")) <= 2 * pe("gap"))
    claim("velocity v from the (L-1)^-2 coefficient", "2.49(1)", "%.3f (coefficient %.1f; paper 74.7(4))" % (v, a21),
          abs(v - pv("v")) <= 2 * pe("v"))
    claim("2 magnons: constant of E_3 - E_1 (= 2 Delta)", "0.823(1)", "%.4f (2 Delta = %.4f)" % (c31, 2 * D),
          abs(c31 - pv("c31")) <= 2 * pe("c31"))
    claim("2 magnons: (L-1)^-2 coefficient", "359(5)", "%.1f" % a31, abs(a31 - pv("a31")) <= 2 * pe("a31"))
    claim("2 magnons: sum n^2 = a_31/a_21 (free fermions: 1 + 4 = 5)", "4.80(6)", "%.2f" % (a31 / a21),
          abs(a31 / a21 - 5) < 0.5)
    claim("3 magnons: (L-1)^-2 coefficient", "1030(150)", "%.0f (constant %.4f, 3 Delta = %.4f)" % (a41, c41, 3 * D),
          abs(a41 - pv("a41")) <= 2 * pe("a41"))
    claim("3 magnons: sum n^2 = a_41/a_21 (free fermions: 1 + 4 + 9 = 14)", "14(2)", "%.1f" % (a41 / a21),
          abs(a41 / a21 - 14) <= 2 * pe("sum_n2_4"))
    # the profiles at L = 100
    sz = np.array(r100["sz"]); bond = np.array(r100["bond"])
    prof = {}
    for M in (2, 3, 4):
        m = M - 1
        ff = free_fermion(100, m)
        dsz = sz[M] - sz[1]
        dev = float(np.max(np.abs(dsz - ff)) / np.max(ff))
        de = bond[M] - bond[1]
        ffb = D * 0.5 * (ff[:-1] + ff[1:])                 # at the bond centres
        devb = float(np.max(np.abs(de - ffb)) / np.max(ffb))
        bulk = slice(12, 88)                                # two correlation lengths away from the ends
        devc = float(np.max(np.abs(dsz[bulk] - ff[bulk])) / np.max(ff))
        prof[M] = (dev, devb, devc)
        claim(f"Fig. {m}: <S^z> of the {m}-magnon state = free fermions n = 1..{m} (max deviation / peak)", "agreement",
              "%.3f (sites 12-87: %.3f)" % (dev, devc), dev < 0.10)
        claim(f"Fig. {m}: bond energies = Delta x the same profile (max deviation / peak)", "agreement",
              "%.3f" % devb, devb < 0.25)
    # parity under i -> L-1-i. Not relative to the S^z = 0 state: the 0+ singlet and the 1- triplet are degenerate to
    # 2e-8 at L = 100, and DMRG returns a mixture of the two (<P> = 0), the end spins pointing opposite ways
    par = np.array(r100["parity"])
    signs = [int(np.sign(p)) for p in par[1:]]
    claim("parities of the lowest S^z = 1..4 states (i -> L-1-i)", "-, +, -, +",
          ", ".join("+" if p > 0 else "-" for p in signs) + " (|<P>| = %s; S^z = 0: %.3f)"
          % (", ".join("%.3f" % abs(p) for p in par[1:]), par[0]), signs == [-1, 1, -1, 1])
    # beyond the paper: the same fits over all lengths (to 160)
    (Dx, ax, bx), ex, rx = fit(L[L >= 40], E[L >= 40, 2] - E[L >= 40, 1])
    out.update(gap_all=Dx, v_all=float(np.sqrt(2 * Dx * ax) / np.pi), split_01_L100=float(r100["E"][1] - r100["E"][0]))
    n_ok = sum(c["ok"] for c in claims)
    lines = [f"# S = 1 chain magnons (Sorensen & Affleck 1993): {n_ok} of {len(claims)} claims", "",
             "| claim | paper | this run | |", "|---|---|---|---|"]
    lines += [f"| {c['claim']} | {c['paper']} | {c['this_run']} | {'✓' if c['ok'] else '✗'} |" for c in claims]
    lines += ["", f"Fits over L = {L[sel].min()}-100 (largest residuals {r21:.1e}, {r31:.1e}, {r41:.1e}; over L = 20-100 "
              f"the E_2 - E_1 fit leaves {r20:.1e}); over all L "
              f"= 40-{L.max()}: Delta = {Dx:.5f}, v = {out['v_all']:.3f}. Singlet-triplet splitting at L = 100: "
              f"{out['split_01_L100']:.2e}.", "",
              "| L | E_0 | E_1 | E_2 | E_3 | E_4 |", "|---|---|---|---|---|---|"]
    lines += ["| %d | %s |" % (l, " | ".join("%.8f" % e for e in row)) for l, row in zip(L, E)]
    Path("report.md").write_text("\n".join(lines) + "\n")
    json.dump({"claims": claims, "fits": out, "L": L.tolist(), "E": E.tolist()}, open("analysis.json", "w"), indent=1)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(10, 4))
    x2 = 1 / (L - 1) ** 2
    for M, lab in ((2, "E2 - E1"), (3, "E3 - E1"), (4, "E4 - E1")):
        ax[0].plot(x2, E[:, M] - E[:, 1], "o", ms=3, label=lab)
    ax[0].set_xlabel("1/(L-1)^2"); ax[0].set_ylabel("gap"); ax[0].legend()
    for M in (2, 3, 4):
        ax[1].plot(sz[M] - sz[1], ".", ms=3, label=f"M = {M}")
        ax[1].plot(free_fermion(100, M - 1), "-", lw=0.8, color="0.4")
    ax[1].set_xlabel("site (L = 100)"); ax[1].set_ylabel("<Sz>_M - <Sz>_1"); ax[1].legend()
    fig.tight_layout(); fig.savefig("magnons.png", dpi=130)
    print(json.dumps({"n_claims": len(claims), "n_reproduced": n_ok, "gap": D, "v": float(v),
                      "sum_n2_2": float(a31 / a21), "sum_n2_3": float(a41 / a21), "E4_L100": r100["E"][4]}))


if __name__ == "__main__":
    main()
