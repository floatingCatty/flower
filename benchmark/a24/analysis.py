"""CCSD(T)/CBS interaction energies of the A24 dimers from this run's parts, against the reference values.

  python3 analysis.py     FLOWER_INPUTS {"data": a24.json, "results": [calc step outputs, local_dir]}

Per dimer: E = HF(aQZ) + MP2corr(CBS; aTZ, aQZ, X^-3) + [CCSD(T) - MP2](aTZ). Claims: every dimer within
0.1 kcal/mol of the reference (the size of the corrections beyond CCSD(T)/CBS that the reference includes), and the
mean absolute deviation below 0.05 kcal/mol. Writes report.md, analysis.json, a24.png; prints the outputs.
"""
import json
import os
from pathlib import Path

import numpy as np


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    data = {r["index"]: r for r in json.loads(Path(I["data"]).read_text())}
    parts = {}
    for it in I["results"]:
        if it and it.get("local_dir") and Path(it["local_dir"], "result.json").is_file():
            r = json.loads(Path(it["local_dir"], "result.json").read_text())
            parts[(r["index"], r["level"])] = r
    rows = []
    for i, d in sorted(data.items()):
        t, q, c = parts.get((i, "mp2-atz")), parts.get((i, "mp2-aqz")), parts.get((i, "ccsdt-atz"))
        if not (t and q and c):
            rows.append({"index": i, "name": d["name"], "ref": d["ref"], "complete": False})
            continue
        corr = (4 ** 3 * q["mp2_corr_int_kcal"] - 3 ** 3 * t["mp2_corr_int_kcal"]) / (4 ** 3 - 3 ** 3)
        e = q["hf_int_kcal"] + corr + c["delta_int_kcal"]
        rows.append({"index": i, "name": d["name"], "ref": d["ref"], "complete": True, "E": e,
                     "hf": q["hf_int_kcal"], "mp2_cbs": q["hf_int_kcal"] + corr, "delta": c["delta_int_kcal"],
                     "dev": e - d["ref"]})
    done = [r for r in rows if r["complete"]]
    dev = np.array([r["dev"] for r in done])
    within = int(np.sum(np.abs(dev) <= 0.1))
    claims = [
        {"claim": "dimers within 0.1 kcal/mol of the reference", "paper": f"{len(rows)} dimers",
         "this_run": f"{within}/{len(done)}" + ("" if len(done) == len(rows) else f" ({len(rows) - len(done)} incomplete)"),
         "ok": within == len(rows)},
        {"claim": "mean absolute deviation (kcal/mol)", "paper": "< 0.05",
         "this_run": "%.3f (max %.3f, mean signed %+.3f)" % (np.mean(np.abs(dev)), np.max(np.abs(dev)), np.mean(dev)),
         "ok": bool(np.mean(np.abs(dev)) < 0.05)},
    ]
    L = ["# A24 at CCSD(T)/CBS (Rezac & Hobza 2013): %d of %d claims" % (sum(c["ok"] for c in claims), len(claims)), "",
         "| claim | reference | this run | |", "|---|---|---|---|"]
    L += ["| %s | %s | %s | %s |" % (c["claim"], c["paper"], c["this_run"], "✓" if c["ok"] else "✗") for c in claims]
    L += ["", "| # | dimer | reference | this run | deviation | HF/aQZ | MP2/CBS | CCSD(T)-MP2 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["complete"]:
            L.append("| %d | %s | %.3f | %.3f | %+.3f | %.3f | %.3f | %+.3f |" % (r["index"], r["name"], r["ref"], r["E"],
                                                                            r["dev"], r["hf"], r["mp2_cbs"], r["delta"]))
        else:
            L.append("| %d | %s | %.3f | - | - | | | |" % (r["index"], r["name"], r["ref"]))
    Path("report.md").write_text("\n".join(L) + "\n")
    json.dump({"claims": claims, "rows": rows}, open("analysis.json", "w"), indent=1)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.bar([r["index"] for r in done], [r["dev"] for r in done], color="tab:blue")
    ax.axhline(0.1, ls=":", c="k"); ax.axhline(-0.1, ls=":", c="k"); ax.axhline(0, c="k", lw=0.6)
    ax.set_xlabel("A24 dimer"); ax.set_ylabel("this run - reference (kcal/mol)")
    fig.tight_layout(); fig.savefig("a24.png", dpi=130)
    print(json.dumps({"n_complete": len(done), "n_within": within, "mad": float(np.mean(np.abs(dev))),
                      "max_abs": float(np.max(np.abs(dev))), "n_claims": len(claims),
                      "n_reproduced": sum(c["ok"] for c in claims)}))


if __name__ == "__main__":
    main()
