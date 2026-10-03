"""Local twist angles of a relaxed TTG structure (Fig. 3G/H, fig. S10 of the paper), from relax.jl's maps.

* AAA sites: local maxima of the stacking (misfit) energy E12 + E23 where *both* bilayers are near AA stacking
  (each energy above `aa_frac` of its maximum). A maximum where only one bilayer is AA belongs to an AtB region.
* local twist at an AAA site: from the mean distance lambda to its Delaunay neighbours (as the paper does with
  "nearest-neighbour AAA site distance"): theta = 2 asin(a / (2 lambda)), a = 2.46 A.
* the histogram and its populations (plaquette / soliton / twiston), and theta_I = the lowest-twist population.

python3 relax_analysis.py   (in the directory with relax_meta.json and the *.f32 maps)
"""
from __future__ import annotations

import json
import os
import math
import sys

import numpy as np
from scipy import ndimage
from scipy.signal import find_peaks
from scipy.spatial import Delaunay

A_LAT = 1.42 * math.sqrt(3)   # Angstrom, the authors' graphene lattice constant (l = 1.42 A bond)


def load(name, n):
    return np.fromfile(name, dtype=np.float32).reshape(n, n)   # [iy, ix]: x fastest


def aaa_sites(m12, m23, step, lam_px, aa_frac=0.6):
    tot = m12 + m23
    size = max(3, int(0.5 * lam_px))
    mx = ndimage.maximum_filter(tot, size=size, mode="nearest")
    cand = np.argwhere((tot == mx) & (tot > 0.3 * tot.max()))
    both = (m12[cand[:, 0], cand[:, 1]] > aa_frac * m12.max()) & (m23[cand[:, 0], cand[:, 1]] > aa_frac * m23.max())
    return cand[both], cand[~both]


def local_twist(pts_px, step, n, margin_px):
    tri = Delaunay(pts_px)
    nb = [set() for _ in range(len(pts_px))]
    for s in tri.simplices:
        for i in s:
            nb[i].update(s)
    th, keep = [], []
    for i, p in enumerate(pts_px):
        if min(p[0], p[1], n - 1 - p[0], n - 1 - p[1]) < margin_px:   # Delaunay is unreliable at the border
            continue
        d = [np.linalg.norm(pts_px[j] - p) for j in nb[i] if j != i]
        if len(d) < 5:
            continue
        lam = float(np.mean(sorted(d)[:6])) * step
        th.append(math.degrees(2 * math.asin(A_LAT / (2 * lam))))
        keep.append(i)
    return np.array(th), np.array(keep, int)


def populations(th, lo, hi, bw=0.006):
    x = np.linspace(lo, hi, 601)
    kde = np.sum(np.exp(-0.5 * ((x[:, None] - th[None]) / bw) ** 2), axis=1)
    kde /= kde.max()
    pk, prop = find_peaks(kde, prominence=0.05)
    return x, kde, [{"theta": float(x[i]), "height": float(kde[i]), "prominence": float(p)}
                    for i, p in zip(pk, prop["prominences"])]


def plot(meta, m12, m23, good, bad, th, keep, x, kde, step, n):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ext = [meta["x0"] / 10, meta["x1"] / 10, meta["x0"] / 10, meta["x1"] / 10]   # nm
    fig, ax = plt.subplots(1, 4, figsize=(17, 4.4), constrained_layout=True)
    for a, m, t in ((ax[0], m12, "stacking energy, top-middle"), (ax[1], m23, "stacking energy, middle-bottom")):
        a.imshow(m, origin="lower", extent=ext, cmap="viridis")
        a.set_title(t)
        a.set_xlabel("x (nm)")
    def pos(p):   # pixel (iy, ix) -> (x, y) in nm
        return (meta["x0"] + p[:, 1] * step) / 10, (meta["x0"] + p[:, 0] * step) / 10
    ax[2].imshow(m12 + m23, origin="lower", extent=ext, cmap="Greys")
    if len(keep):
        sc = ax[2].scatter(*pos(good[keep]), c=th, s=14, cmap="RdYlBu_r")
        fig.colorbar(sc, ax=ax[2], label="local twist (deg)")
    if len(bad):
        ax[2].scatter(*pos(bad), marker="x", s=12, c="k", label="AA in one bilayer only")
        ax[2].legend(loc="lower left", fontsize=7)
    ax[2].set_title(f"AAA sites (AtA fraction {len(good) / max(1, len(good) + len(bad)):.2f})")
    ax[3].hist(th, bins=40, color="#8e8e8e")
    ax[3].plot(x, kde * ax[3].get_ylim()[1], color="#c0392b")
    ax[3].set_xlabel("local twist (deg)")
    ax[3].set_title(f"theta_TM {meta['theta_TM']}, theta_BM {meta['theta_BM']}, N {meta['N']}")
    fig.savefig("relax.png", dpi=110)


def main() -> dict:
    meta = json.load(open("relax_meta.json"))
    n = int(meta["npix"])
    step = (meta["x1"] - meta["x0"]) / (n - 1)                       # Angstrom per pixel
    m12, m23 = load("misfit12.f32", n), load("misfit23.f32", n)
    th_small = min(meta["theta_TM"], meta["theta_BM"])
    lam_px = A_LAT / (2 * math.sin(math.radians(max(meta["theta_TM"], meta["theta_BM"])) / 2)) / step
    good, bad = aaa_sites(m12, m23, step, lam_px)
    th, keep = local_twist(good.astype(float), step, n, margin_px=lam_px)
    lo, hi = min(meta["theta_TM"], meta["theta_BM"]) - 0.25, max(meta["theta_TM"], meta["theta_BM"]) + 0.25
    x, kde, pops = populations(th, lo, hi)
    pops.sort(key=lambda p: p["theta"])
    # curl-based local twist (plot_relax.m): theta_12 = |theta_TM + asin(c2/2) + asin(c1/2)| (sign conventions
    # of that script; layer 2 is the fixed middle layer)
    c = [load(f"curl{k}.f32", n) for k in (1, 2, 3)]
    th12 = np.degrees(np.abs(math.radians(meta["theta_TM"]) + np.arcsin(np.clip(-c[1] / 2, -1, 1))
                             + np.arcsin(np.clip(-c[0] / 2, -1, 1))))
    th23 = np.degrees(np.abs(math.radians(meta["theta_BM"]) + np.arcsin(np.clip(-c[2] / 2, -1, 1))
                             + np.arcsin(np.clip(-c[1] / 2, -1, 1))))
    np.savez_compressed("relax_maps.npz", m12=m12, m23=m23, th12=th12.astype(np.float32),
                        th23=th23.astype(np.float32), aaa=good, other_max=bad, aaa_theta=th, aaa_keep=keep,
                        kde_x=x, kde=kde, step=step)
    ata = len(good) / max(1, len(good) + len(bad))
    plot(meta, m12, m23, good, bad, th, keep, x, kde, step, n)
    # second estimator: the authors' curl-based local twist (plot_relax.m), at the AAA sites and over the map
    curl_stats = {}
    for name, arr in (("theta12_curl", th12), ("theta23_curl", th23)):
        at = arr[good[:, 0], good[:, 1]] if len(good) else np.array([])
        xs, kd, pp = populations(at, lo, hi) if len(at) else (None, None, [])
        curl_stats[name] = {"at_aaa_mean": float(np.mean(at)) if len(at) else None,
                            "at_aaa_populations": sorted(round(q["theta"], 3) for q in pp),
                            "map_percentiles_5_50_95": [float(np.percentile(arr, q)) for q in (5, 50, 95)]}
    out = {**{k: meta[k] for k in ("theta_TM", "theta_BM", "N", "amp", "converged", "iterations", "seconds_opt")},
           "delta_theta": abs(meta["theta_TM"] - meta["theta_BM"]),
           "n_aaa": int(len(good)), "n_other_maxima": int(len(bad)), "ata_fraction": ata,
           "theta_mean": float(np.mean(th)) if len(th) else None, "theta_std": float(np.std(th)) if len(th) else None,
           "populations": pops, "n_populations": len(pops),
           "theta_I": pops[0]["theta"] if pops else None,
           "theta_I_minus_smaller": (pops[0]["theta"] - th_small) if pops else None,
           "window_nm": (meta["x1"] - meta["x0"]) / 10, "curl_estimator": curl_stats}
    return out


if __name__ == "__main__":
    res = main()
    json.dump(res, open("relax.json", "w"), indent=1)
    print(json.dumps({"dir": os.getcwd(), **{k: res[k] for k in ("n_aaa", "ata_fraction", "n_populations", "theta_I",
                                                                 "theta_mean")}}))
    sys.exit(0)
