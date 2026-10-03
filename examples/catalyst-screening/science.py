"""Small analysis helpers called by `function` nodes (run with the science Python environment)."""
from __future__ import annotations

import os


def plot_descriptor(results: list, target: float, ctx: dict) -> dict:
    """Bar chart of O adsorption energies with the target band; returns the plot path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = sorted([r for r in results if r], key=lambda r: abs(r["e_ads"] - target))
    names = [r["metal"] for r in rows]
    vals = [r["e_ads"] for r in rows]
    fig, ax = plt.subplots(figsize=(6, 3.6), dpi=130)
    colors = ["#1f7a45" if abs(v - target) < 0.3 else "#98a2b3" for v in vals]
    ax.bar(names, vals, color=colors)
    ax.axhline(target, color="#b42318", lw=1.2, ls="--", label=f"target {target:+.2f} eV")
    ax.axhspan(target - 0.3, target + 0.3, color="#b42318", alpha=0.08)
    ax.set_ylabel("E_ads(O) on (111) / eV  [EMT]")
    ax.set_title("O adsorption descriptor, closest to target first")
    ax.legend(frameon=False)
    fig.tight_layout()
    path = os.path.join(ctx["workdir"], "descriptor.png")
    fig.savefig(path)
    best = rows[0]["metal"] if rows else None
    return {"plot": path, "order": names, "best_by_descriptor": best,
            "summary": f"plotted {len(rows)} metals; closest to target: {best}"}
