"""This run's elastic constants against Tables 1-3 of the paper (Rassoulinejad-Mousavi, Mao, Zhang 2016).

  python3 analysis.py      FLOWER_INPUTS {"tables": tables.json, "index": potentials.json,
                                          "results": [elastic step outputs, local_dir]}

Per (metal, potential) with a complete paper row: the paper's C11, C12, C44 against this run's
  * static: the linear elastic constants (symmetric +-1 % strains, third-order terms cancel), and
  * rate: the paper's own protocol (constant-rate tension/shear, slope on 0-1 % strain).
A row is reproduced when all three constants agree within 5 % with the static values (the paper's protocol is
biased low by the third-order constants by a few %, see the README). Writes report.md, analysis.json, parity.png;
prints the outputs JSON.
"""
import json
import os
from pathlib import Path

import numpy as np

KEYS = ("C11", "C12", "C44")
TOL = 0.05


def spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    tables = json.loads(Path(I["tables"]).read_text())
    have = json.loads(Path(I["index"]).read_text())          # the potential files that were found
    ours = {}
    for it in I["results"]:
        if it and it.get("local_dir") and Path(it["local_dir"], "elastic.json").is_file():
            r = json.loads(Path(it["local_dir"], "elastic.json").read_text())
            ours[(r["metal"], r["potential"])] = r
    rows, claims = [], []
    for metal, t in tables.items():
        for p in t["rows"]:
            if not p["complete"]:
                continue
            r = ours.get((metal, p["potential"]))
            row = {"metal": metal, "potential": p["potential"], "paper": {k: p[k] for k in KEYS}, "found": r is not None}
            if r:
                row["static"] = {k: r[k] for k in KEYS}
                row["static_err"] = {k: r[k + "_err"] for k in KEYS}
                row["rate"] = {k: r[k + "_rate_1"] for k in KEYS}
                row["dev_static"] = {k: (p[k] - r[k]) / r[k] for k in KEYS}
                row["dev_rate"] = {k: (p[k] - r[k + "_rate_1"]) / r[k + "_rate_1"] for k in KEYS}
                row["ok"] = all(abs(v) <= TOL for v in row["dev_static"].values())
                row["a300"] = r["a300"]
            rows.append(row)
    done = [r for r in rows if r["found"]]
    n_ok = sum(r["ok"] for r in done)
    claims.append({"claim": "Table rows (complete, potential file found) with C11, C12, C44 all within 5 %",
                   "paper": f"{len(done)} rows", "this_run": f"{n_ok}/{len(done)}", "ok": n_ok >= 0.8 * len(done)})
    # the paper's protocol is biased low for C11, C12 (one-sided tension: third-order constants are negative)
    for k in KEYS:
        d = np.array([r["dev_static"][k] for r in done if r["ok"]])
        if len(d):
            claims.append({"claim": f"{k}: paper minus linear value, median over the reproduced rows",
                           "paper": "-", "this_run": "%+.1f %%" % (100 * np.median(d)), "ok": None})
    # does the paper rank the potentials the same way (per metal, by the constant itself)?
    for metal in tables:
        sub = [r for r in done if r["metal"] == metal]
        if len(sub) >= 4:
            rho = {k: spearman([r["paper"][k] for r in sub], [r["static"][k] for r in sub]) for k in KEYS}
            claims.append({"claim": f"{metal}: the potentials' order by C11, C12, C44 (Spearman rho, n={len(sub)})",
                           "paper": "-", "this_run": ", ".join("%s %.2f" % (k, v) for k, v in rho.items()),
                           "ok": min(rho.values()) >= 0.8})
    bad = [r for r in done if not r["ok"]]
    L = ["# EAM elastic constants at 300 K (Rassoulinejad-Mousavi, Mao, Zhang 2016): %d of %d rows within 5 %%"
         % (n_ok, len(done)), "",
         "| claim | paper | this run | |", "|---|---|---|---|"]
    L += ["| %s | %s | %s | %s |" % (c["claim"], c["paper"], c["this_run"],
                                     "" if c["ok"] is None else ("✓" if c["ok"] else "✗")) for c in claims]
    L += ["", "## Per potential (GPa): paper / this run static (linear) / this run with the paper's rate protocol", "",
          "| metal | potential | C11 | C12 | C44 | a(300 K) Å | |", "|---|---|---|---|---|---|---|"]
    for r in sorted(done, key=lambda r: (r["metal"], r["potential"])):
        cells = ["%.1f / %.1f / %.1f" % (r["paper"][k], r["static"][k], r["rate"][k]) for k in KEYS]
        L.append("| %s | %s | %s | %.4f | %s |" % (r["metal"], r["potential"], " | ".join(cells), r["a300"],
                                                   "✓" if r["ok"] else "✗"))
    missing = [r for r in rows if r["potential"] not in have]
    pending = [r for r in rows if r["potential"] in have and not r["found"]]
    L += ["", "Rows whose potential file was not found (not tested): " +
          (", ".join(f"{r['metal']} {r['potential']}" for r in missing) or "none")]
    if pending:
        L += ["", "Rows without a result (not run yet, or the run failed): " +
              ", ".join(f"{r['metal']} {r['potential']}" for r in pending)]
    if bad:
        L += ["", "## Not reproduced", ""] + [
            "- %s %s: paper %s vs this run %s" % (r["metal"], r["potential"],
                                                  ", ".join("%s %.1f" % (k, r["paper"][k]) for k in KEYS),
                                                  ", ".join("%s %.1f" % (k, r["static"][k]) for k in KEYS)) for r in bad]
    Path("report.md").write_text("\n".join(L) + "\n")
    json.dump({"claims": claims, "rows": rows}, open("analysis.json", "w"), indent=1, default=float)
    if not done:
        print(json.dumps({"n_rows": 0, "n_reproduced": 0, "n_not_found": len(missing), "n_pending": len(pending),
                          "not_reproduced": []}))
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(13, 4.2))
    for a, k in zip(ax, KEYS):
        for metal, c in (("Cu", "tab:orange"), ("Al", "tab:gray"), ("Ni", "tab:green")):
            sub = [r for r in done if r["metal"] == metal]
            a.scatter([r["static"][k] for r in sub], [r["paper"][k] for r in sub], s=14, color=c, label=metal)
        lim = [0, max(max(r["static"][k], r["paper"][k]) for r in done) * 1.08]
        a.plot(lim, lim, "k-", lw=0.6)
        a.plot(lim, [x * (1 - TOL) for x in lim], "k:", lw=0.6)
        a.plot(lim, [x * (1 + TOL) for x in lim], "k:", lw=0.6)
        a.set_xlabel(f"{k}, this run (linear, GPa)"); a.set_ylabel(f"{k}, paper (GPa)"); a.set_xlim(lim); a.set_ylim(lim)
    ax[0].legend()
    fig.tight_layout(); fig.savefig("parity.png", dpi=130)
    print(json.dumps({"n_rows": len(done), "n_reproduced": n_ok, "n_not_found": len(missing), "n_pending": len(pending),
                      "not_reproduced": [f"{r['metal']} {r['potential']}" for r in bad]}))


if __name__ == "__main__":
    main()
