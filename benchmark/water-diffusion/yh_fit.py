"""Yeh-Hummer analysis (Tazi et al., JPCM 24, 284117 (2012)): D_PBC(L) = D0 - xi kB T / (6 pi eta L).
Reads $FLOWER_INPUTS {"md": [...]} (outputs of the MD steps, with local_dir); per model: mean D_PBC per box size
(over seeds), weighted linear fit in 1/L -> D0 and eta (with standard errors), and the paper's claims.
"""
import json
import math
import os
from pathlib import Path

import numpy as np

XI = 2.837297
KB = 1.380649e-23
T = 300.0
PAPER = {"spce": {"D0": (2.97, 0.05), "eta": (0.64, 0.02)}, "tip4p2005": {"D0": (2.49, 0.06), "eta": (0.83, 0.07)}}
EXPT = {"D0": 2.3, "eta": 0.896}


def main():
    I = json.loads(Path(os.environ["FLOWER_INPUTS"]).read_text())
    runs = []
    for it in I.get("md") or []:
        d = Path((it or {}).get("local_dir") or "")
        if (d / "result.json").is_file():
            runs.append(json.loads((d / "result.json").read_text()))
    out = {"models": {}, "n_runs": len(runs), "claims": []}
    for model in ("spce", "tip4p2005"):
        rs = [r for r in runs if r["model"] == model]
        if not rs:
            continue
        sizes = sorted({r["N"] for r in rs})
        rows = []
        for n in sizes:
            ds = np.array([r["D_pbc"] for r in rs if r["N"] == n])
            errs = [r["D_err"] for r in rs if r["N"] == n and r.get("D_err")]
            L = [r["L_nm"] for r in rs if r["N"] == n][0]
            # error of the mean: from the seeds when there are several, else the run's own block estimate
            e = float(np.std(ds, ddof=1) / math.sqrt(len(ds))) if len(ds) > 1 else (errs[0] if errs else 0.05 * ds[0])
            e = max(e, float(np.mean(errs)) / math.sqrt(len(ds)) if errs else e)
            rows.append({"N": n, "L_nm": L, "D": float(ds.mean()), "err": e, "n_seeds": int(len(ds))})
        x = np.array([1 / r["L_nm"] for r in rows])
        y = np.array([r["D"] for r in rows])
        w = 1 / np.array([max(r["err"], 1e-3) for r in rows]) ** 2
        A = np.vstack([np.ones_like(x), x]).T
        cov = np.linalg.inv(A.T @ (w[:, None] * A))
        D0, s = cov @ A.T @ (w * y)
        chi2 = float(np.sum(w * (y - A @ np.array([D0, s])) ** 2))
        dof = max(1, len(x) - 2)
        scale = max(1.0, chi2 / dof)                         # inflate errors if the scatter exceeds the bars
        eD0, es = math.sqrt(cov[0, 0] * scale), math.sqrt(cov[1, 1] * scale)
        # slope s (1e-9 m^2/s * nm) = - xi kB T / (6 pi eta)  ->  eta in mPa s (cP)
        sl = -s * 1e-9 * 1e-9
        eta = XI * KB * T / (6 * math.pi * sl) * 1e3
        eeta = eta * es / abs(s)
        out["models"][model] = {"sizes": rows, "D0": float(D0), "D0_err": eD0, "eta_cP": eta, "eta_err": eeta,
                                "chi2_per_dof": chi2 / dof}
        p = PAPER[model]
        for key, val, err in (("D0", float(D0), eD0), ("eta", eta, eeta)):
            ref, rerr = p[key]
            ok = abs(val - ref) <= 2 * math.hypot(err, rerr)
            out["claims"].append({"claim": f"{model} {key}", "paper": f"{ref} +- {rerr}", "reproduced": f"{val:.3f} +- {err:.3f}",
                                  "verdict": "reproduced" if ok else "differs"})
    m = out["models"]
    if "spce" in m and "tip4p2005" in m:
        closer_D = abs(m["tip4p2005"]["D0"] - EXPT["D0"]) < abs(m["spce"]["D0"] - EXPT["D0"])
        closer_eta = abs(m["tip4p2005"]["eta_cP"] - EXPT["eta"]) < abs(m["spce"]["eta_cP"] - EXPT["eta"])
        out["claims"].append({"claim": "TIP4P/2005 agrees better with experiment (D and eta)", "paper": "yes",
                              "reproduced": f"D: {closer_D}, eta: {closer_eta}",
                              "verdict": "reproduced" if closer_D and closer_eta else "differs"})
        over = m["spce"]["D0"] > EXPT["D0"] and m["spce"]["eta_cP"] < EXPT["eta"]
        out["claims"].append({"claim": "SPC/E overestimates D and underestimates eta", "paper": "yes",
                              "reproduced": f"D0 {m['spce']['D0']:.2f} vs 2.3, eta {m['spce']['eta_cP']:.2f} vs 0.896",
                              "verdict": "reproduced" if over else "differs"})
    json.dump(out, open("yh.json", "w"), indent=1)
    md = ["| claim | paper | this run | verdict |", "|---|---|---|---|"]
    md += [f"| {c['claim']} | {c['paper']} | {c['reproduced']} | **{c['verdict']}** |" for c in out["claims"]]
    Path("report.md").write_text("\n".join(md) + "\n")
    figure(out)
    return out


def figure(out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4), constrained_layout=True)
    for (model, r), col in zip(out["models"].items(), ("#2a78d6", "#eb6834")):
        x = [1 / s["L_nm"] for s in r["sizes"]]
        ax.errorbar(x, [s["D"] for s in r["sizes"]], yerr=[s["err"] for s in r["sizes"]], fmt="o", color=col,
                    label=f"{model}: D0 {r['D0']:.2f}, eta {r['eta_cP']:.2f} cP")
        xx = np.linspace(0, max(x) * 1.05, 50)
        s = -(XI * KB * T / (6 * math.pi * r["eta_cP"] * 1e-3)) * 1e18
        ax.plot(xx, r["D0"] + s * xx, color=col, lw=1.2)
    ax.axhline(EXPT["D0"], color="#6b6a63", ls="--", lw=1, label="experiment 2.3")
    ax.set_xlabel("1 / L (nm^-1)")
    ax.set_ylabel("D_PBC (1e-9 m^2/s)")
    ax.set_xlim(left=0)
    ax.legend(frameon=False, fontsize=8)
    ax.set_title("Yeh-Hummer extrapolation, T = 300 K")
    fig.savefig("yh.png", dpi=120)


if __name__ == "__main__":
    o = main()
    print(json.dumps({"dir": os.getcwd(), "n_runs": o["n_runs"],
                      "n_reproduced": sum(c["verdict"] == "reproduced" for c in o["claims"]),
                      "n_claims": len(o["claims"])}))
