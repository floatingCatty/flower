"""Assemble CCSD(T)/CBS interaction energies from the per-level steps and compare with the S22 references.

E_int(CCSD(T)/CBS) = E_HF(aTZ) + E_corr,MP2(CBS: aDZ->aTZ, X^-3) + [CCSD(T) - MP2](cc-pVDZ), all counterpoise
corrected. References: the paper (Jurecka et al. 2006, psi4 BIND_S220) and the revised S22B (Marshall,
Burns & Sherrill 2011). Reads $FLOWER_INPUTS {"calc": [...], "data": path}; writes s22_report.json/.md and a
figure. Missing levels (a step failed or is still running) are reported as such: partial results are fine.
"""
import json
import os
from pathlib import Path

GROUPS = {"hydrogen-bonded": range(1, 8), "dispersion": range(8, 16), "mixed": range(16, 23)}


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    recs = {r["index"]: r for r in json.loads(Path(I["data"]).read_text())}
    lev = {}
    # calc_big: the large ones with more memory; calc_remote: those re-run on another machine (later ones win)
    for it in list(I.get("calc") or []) + list(I.get("calc_big") or []) + list(I.get("calc_remote") or []):
        d = Path((it or {}).get("local_dir") or "")
        if (d / "result.json").is_file():
            r = json.loads((d / "result.json").read_text())
            lev[(r["index"], r["level"])] = r
    rows = []
    for i, rec in sorted(recs.items()):
        d, t, c = lev.get((i, "mp2-adz")), lev.get((i, "mp2-atz")), lev.get((i, "ccsdt-dz"))
        row = {"index": i, "atoms": len(rec["A"]) + len(rec["B"]), "ref_2006": rec["ref_2006"],
               "ref_S22B": rec["ref_S22B"], "missing": [k for k, v in (("mp2-adz", d), ("mp2-atz", t),
                                                                          ("ccsdt-dz", c)) if v is None]}
        if d and t:
            corr = (27 * t["mp2_corr_int_kcal"] - 8 * d["mp2_corr_int_kcal"]) / 19
            row["mp2_cbs"] = t["hf_int_kcal"] + corr
            row["mp2_atz"] = t["hf_int_kcal"] + t["mp2_corr_int_kcal"]
        if c:
            row["delta_ccsdt"] = c["delta_int_kcal"]
        if "mp2_cbs" in row and "delta_ccsdt" in row:
            row["ccsdt_cbs"] = row["mp2_cbs"] + row["delta_ccsdt"]
            row["err_2006"] = row["ccsdt_cbs"] - row["ref_2006"]
            row["err_S22B"] = row["ccsdt_cbs"] - row["ref_S22B"]
            tol = max(0.3, 0.05 * abs(row["ref_2006"]))
            row["verdict"] = "reproduced" if abs(row["err_2006"]) <= tol else "differs"
        else:
            row["verdict"] = "pending"
        row["seconds"] = sum(x["seconds"] for x in (d, t, c) if x)
        rows.append(row)
    done = [r for r in rows if "ccsdt_cbs" in r]
    stats = {}
    for gname, rng in list(GROUPS.items()) + [("all", range(1, 23))]:
        g = [r for r in done if r["index"] in rng]
        if g:
            stats[gname] = {"n": len(g), "mae_2006": sum(abs(r["err_2006"]) for r in g) / len(g),
                            "max_2006": max(abs(r["err_2006"]) for r in g),
                            "mae_S22B": sum(abs(r["err_S22B"]) for r in g) / len(g)}
    out = {"rows": rows, "stats": stats, "n_done": len(done),
           "n_reproduced": sum(r["verdict"] == "reproduced" for r in rows),
           "n_differs": sum(r["verdict"] == "differs" for r in rows)}
    json.dump(out, open("s22_report.json", "w"), indent=1)
    md = ["| # | atoms | paper 2006 | S22B | this run CCSD(T)/CBS | MP2/CBS | dCCSD(T) | error vs 2006 | verdict |",
          "|---|---|---|---|---|---|---|---|---|"]
    f = lambda x: "-" if x is None else f"{x:.2f}"  # noqa: E731
    for r in rows:
        md.append(f"| {r['index']} | {r['atoms']} | {f(r['ref_2006'])} | {f(r['ref_S22B'])} | {f(r.get('ccsdt_cbs'))} | "
                  f"{f(r.get('mp2_cbs'))} | {f(r.get('delta_ccsdt'))} | {f(r.get('err_2006'))} | **{r['verdict']}**"
                  + (f" (missing {', '.join(r['missing'])})" if r["missing"] else "") + " |")
    md += ["", "| group | n | MAE vs 2006 | max vs 2006 | MAE vs S22B |", "|---|---|---|---|---|"]
    for gname, s in stats.items():
        md.append(f"| {gname} | {s['n']} | {s['mae_2006']:.2f} | {s['max_2006']:.2f} | {s['mae_S22B']:.2f} |")
    Path("s22_report.md").write_text("\n".join(md) + "\n")
    figure(rows)
    return out


def figure(rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    done = [r for r in rows if "ccsdt_cbs" in r]
    fig, ax = plt.subplots(figsize=(9, 3.6), constrained_layout=True)
    col = {"hydrogen-bonded": "#2a78d6", "dispersion": "#eb6834", "mixed": "#1baf7a"}
    for gname, rng in GROUPS.items():
        g = [r for r in done if r["index"] in rng]
        ax.plot([r["index"] for r in g], [r["err_2006"] for r in g], "o", ms=8, color=col[gname], label=gname,
                mec="white", mew=1.2)
        ax.plot([r["index"] for r in g], [r["err_S22B"] for r in g], "D", ms=6, mfc="none", mec=col[gname])
    ax.axhline(0, color="#6b6a63", lw=1)
    ax.axhspan(-0.3, 0.3, color="#e6e5df", lw=0)
    ax.set_xticks(range(1, 23))
    ax.set_xlabel("S22 complex")
    ax.set_ylabel("this run - reference (kcal/mol)")
    ax.plot([], [], "D", mfc="none", mec="#6b6a63", label="vs revised S22B")
    ax.legend(frameon=False, fontsize=8, ncol=4)
    ax.set_title("CCSD(T)/CBS[aDZ->aTZ MP2 + dCCSD(T)/DZ] vs the paper's reference (circles)")
    fig.savefig("s22.png", dpi=120)


if __name__ == "__main__":
    o = main()
    s = o["stats"].get("all", {})
    print(json.dumps({"dir": os.getcwd(), "n_done": o["n_done"], "n_reproduced": o["n_reproduced"],
                      "n_differs": o["n_differs"], "mae_2006": s.get("mae_2006"), "mae_S22B": s.get("mae_S22B")}))
