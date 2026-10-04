"""The Delta factor of this run's equations of state against WIEN2k (Lejaeghere et al.).

  python3 delta.py      FLOWER_INPUTS {"eos": [step outputs {el, V, E}], "wien2k": path (the Delta package beside it)}
Writes ours.txt (V0, B0, B1 per element, the package's format), delta.json, report.md, delta.png; prints outputs.

The fit and the integral follow the package's eosfit.py and calcDelta.py (version 3.0), ported to Python 3. The
port is checked against the package's own calcDelta.py (run through 2to3, np.float -> np.float64) on its published data
("QE with SSSP Efficiency" in history/QE-history.txt): both must give the same per-element Delta.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

PAPER_AVG = {"SSSP efficiency 1.1": 0.44, "SSSP precision 1.1": 0.33}     # Prandini et al. 2018, meV/atom
EV_A3_PER_GPA = 1e9 / 1.602176565e-19 / 1e30


def bm_fit(V, E):
    """eosfit.py: E = cubic polynomial in V^(-2/3); V0, B0 (GPa), B1, and the residual."""
    V, E = np.asarray(V), np.asarray(E)
    fit = np.polyfit(V ** (-2 / 3), E, 3, full=True)
    ssr = fit[1][0] if len(fit[1]) else 0.0
    sst = np.sum((E - E.mean()) ** 2)
    p = np.poly1d(fit[0])
    d1, d2, d3 = np.polyder(p, 1), np.polyder(p, 2), np.polyder(p, 3)
    x = next((r.real for r in np.roots(d1) if abs(r.imag) < 1e-12 and r.real > 0 and d2(r.real) > 0), None)
    if x is None:
        raise ValueError("no minimum")
    v0 = x ** (-3 / 2)
    dv2 = 4 / 9 * x ** 5 * d2(x)
    dv3 = -20 / 9 * x ** (13 / 2) * d2(x) - 8 / 27 * x ** (15 / 2) * d3(x)
    b0 = dv2 / x ** (3 / 2)
    b1 = -1 - x ** (-3 / 2) * dv3 / dv2
    return float(v0), float(b0 / EV_A3_PER_GPA), float(b1), float(ssr / sst)


def delta(a, b):
    """calcDelta.py (symmetric range): rms of E_a(V) - E_b(V) over [0.94, 1.06] x the mean V0, in meV/atom; plus
    Delta1 (normalised to V0 = 30 A^3, B0 = 100 GPa)."""
    (v0a, b0a, b1a), (v0b, b0b, b1b) = a, b
    b0a, b0b = b0a * EV_A3_PER_GPA, b0b * EV_A3_PER_GPA
    vi, vf = 0.94 * (v0a + v0b) / 2, 1.06 * (v0a + v0b) / 2

    def coeffs(v0, b0, b1):
        return (9 * v0 ** 3 * b0 / 16 * (b1 - 4), 9 * v0 ** (7 / 3) * b0 / 16 * (14 - 3 * b1),
                9 * v0 ** (5 / 3) * b0 / 16 * (3 * b1 - 16), 9 * v0 * b0 / 16 * (6 - b1))

    a3, a2, a1, a0 = np.subtract(coeffs(v0a, b0a, b1a), coeffs(v0b, b0b, b1b))
    x = [a0 ** 2, 6 * a1 * a0, -3 * (2 * a2 * a0 + a1 ** 2), -2 * a3 * a0 - 2 * a2 * a1,
         -3 / 5 * (2 * a3 * a1 + a2 ** 2), -6 / 7 * a3 * a2, -1 / 3 * a3 ** 2]

    def F(v):   # the antiderivative of (E_a - E_b)^2, as in calcDelta.py
        return sum(c * v ** (-(2 * n - 3) / 3) for n, c in enumerate(x))

    d = 1000 * np.sqrt((F(vf) - F(vi)) / (vf - vi))
    v0m, b0m = (v0a + v0b) / 2, (b0a + b0b) / 2
    return float(d), float(d * 30 * 100 * EV_A3_PER_GPA / (v0m * b0m))


def table(path, section=None):
    """{el: (V0, B0, B1)} from a calcDelta data file, or from one `# ...` section of a history file."""
    out, on = {}, section is None
    for ln in open(path):
        if ln.startswith("#"):
            on = section is None or section in ln
            continue
        p = ln.split()
        if on and len(p) >= 4:
            try:
                out[p[0]] = tuple(float(x) for x in p[1:4])
            except ValueError:
                pass
    return out


def check_port(pkg, ours_fn):
    """Run the package's own calcDelta.py (2to3) on the history's SSSP-efficiency data vs WIEN2k, and compare."""
    work = Path("port-check")
    work.mkdir(exist_ok=True)
    shutil.copy(pkg / "calcDelta.py", work / "calcDelta.py")
    shutil.copy(pkg / "WIEN2k.txt", work / "WIEN2k.txt")
    hist = table(pkg / "history" / "QE-history.txt", "SSSP Efficiency")
    (work / "hist.txt").write_text("".join("%s %r %r %r\n" % (el, *v) for el, v in hist.items()))
    subprocess.run([sys.executable, "-m", "lib2to3", "-w", "-n", "calcDelta.py"], cwd=work, check=True,
                   capture_output=True)
    code = (work / "calcDelta.py").read_text()      # and NumPy 2 removed the np.float alias it uses
    (work / "calcDelta.py").write_text(re.sub(r"\bnp\.float\b(?!\d)", "np.float64", code))
    r = subprocess.run([sys.executable, "calcDelta.py", "hist.txt", "WIEN2k.txt", "--stdout"], cwd=work,
                       capture_output=True, text=True, check=True)
    theirs = {}
    for ln in r.stdout.splitlines():
        p = ln.split()
        if len(p) >= 2 and p[0] in hist:
            try:
                theirs[p[0]] = float(p[1])
            except ValueError:
                pass
    ref = table(pkg / "WIEN2k.txt")
    mine = {el: ours_fn(hist[el], ref[el])[0] for el in theirs}
    worst = max(abs(mine[el] - theirs[el]) for el in theirs)
    if worst > 2e-3:
        raise SystemExit("the Delta port disagrees with calcDelta.py by %.4f meV/atom" % worst)
    return {"n": len(theirs), "max_diff": worst, "avg_history": float(np.mean(list(theirs.values()))),
            "history": theirs}


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    pkg = Path(I["wien2k"]).parent
    ref = table(pkg / "WIEN2k.txt")
    hist = table(pkg / "history" / "QE-history.txt", "SSSP Efficiency")
    port = check_port(pkg, delta)
    rows, fits = [], {}
    for it in I["eos"]:
        if not it or not it.get("V"):
            continue
        el = it["el"]
        try:
            v0, b0, b1, res = bm_fit(it["V"], it["E"])
        except ValueError as exc:
            rows.append({"el": el, "error": str(exc)})
            continue
        fits[el] = (v0, b0, b1)
        d, d1 = delta((v0, b0, b1), ref[el])
        dh = delta((v0, b0, b1), hist[el])[0] if el in hist else None
        rows.append({"el": el, "V0": v0, "B0": b0, "B1": b1, "residual": res, "delta": d, "delta1": d1,
                     "delta_history": port["history"].get(el), "delta_vs_history_entry": dh,
                     "V0_rel_wien2k": v0 / ref[el][0] - 1, "outside_96_104": abs(v0 / ref[el][0] - 1) > 0.04,
                     "mag": it.get("mag")})
    Path("ours.txt").write_text("".join("%s %.6f %.6f %.6f\n" % (el, *v) for el, v in sorted(fits.items())))
    ok = [r for r in rows if "delta" in r]
    avg = float(np.mean([r["delta"] for r in ok])) if ok else None
    common = [r for r in ok if r["delta_history"] is not None]
    avg_hist_common = float(np.mean([r["delta_history"] for r in common])) if common else None
    json.dump({"rows": rows, "port_check": port, "avg": avg}, open("delta.json", "w"), indent=1)
    write_report(rows, avg, port, avg_hist_common)
    figure(ok)
    worst = sorted(ok, key=lambda r: -r["delta"])[:5]
    return {"n_elements": len(ok), "avg_delta": avg, "paper_avg_delta": PAPER_AVG["SSSP efficiency 1.1"],
            "avg_delta_history_entry": port["avg_history"], "port_max_diff": port["max_diff"],
            "worst": [[r["el"], round(r["delta"], 3)] for r in worst],
            "n_outside_96_104": sum(r["outside_96_104"] for r in ok),
            "failed": [r["el"] for r in rows if "error" in r]}


def write_report(rows, avg, port, avg_hist_common):
    ok = [r for r in rows if "delta" in r]
    L = ["# Delta test: SSSP efficiency 1.1 (Quantum ESPRESSO) vs WIEN2k", "",
         "| | Delta (meV/atom), average over elements | elements |", "|---|---|---|",
         "| this run (SSSP 1.1 efficiency, its recommended cutoffs) | %s | %d |" % (
             "%.3f" % avg if avg is not None else "n/a", len(ok)),
         "| paper: SSSP efficiency 1.1 at 200 Ry (Prandini et al. 2018) | 0.44 | 71 (excl. rare-earth nitrides) |",
         "| published: QE + SSSP Efficiency (Castelli, Delta package history) | %.3f | %d |" % (port["avg_history"], port["n"]),
         "", "The Delta formula ported here agrees with the package's calcDelta.py to %.1e meV/atom on its "
         "published data." % port["max_diff"], "",
         "| element | V0 (A^3/atom) | V0/V0(WIEN2k) - 1 | B0 (GPa) | B1 | Delta | Delta1 | published QE+SSSP Delta | Delta vs that entry |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(ok, key=lambda r: -r["delta"]):
        L.append("| %s | %.4f | %+.2f %% | %.2f | %.2f | %.3f | %.3f | %s | %s |" % (
            r["el"], r["V0"], 100 * r["V0_rel_wien2k"], r["B0"], r["B1"], r["delta"], r["delta1"],
            "%.3f" % r["delta_history"] if r["delta_history"] is not None else "",
            "%.3f" % r["delta_vs_history_entry"] if r["delta_vs_history_entry"] is not None else ""))
    bad = [r["el"] for r in rows if "error" in r]
    if bad:
        L += ["", "No minimum in the fitted EOS: " + ", ".join(bad)]
    Path("report.md").write_text("\n".join(L) + "\n")


def figure(ok):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ase.data import atomic_numbers
    ok = sorted(ok, key=lambda r: atomic_numbers[r["el"]])
    fig, ax = plt.subplots(figsize=(14, 4))
    x = np.arange(len(ok))
    ax.bar(x - 0.2, [r["delta"] for r in ok], 0.4, label="this run vs WIEN2k")
    ax.bar(x + 0.2, [r["delta_history"] or 0 for r in ok], 0.4, label="published QE+SSSP efficiency vs WIEN2k")
    ax.set_xticks(x)
    ax.set_xticklabels([r["el"] for r in ok], fontsize=7)
    ax.set_ylabel("Delta (meV/atom)")
    ax.legend()
    fig.tight_layout()
    fig.savefig("delta.png", dpi=110)


if __name__ == "__main__":
    print(json.dumps(main(), default=lambda o: o.item()))
