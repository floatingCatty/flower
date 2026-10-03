"""Fig. 1B: out-of-plane corrugation of AtA and AtB trilayer stacking, from the authors' recipe (Dataverse
1B_code.txt, MATLAB): the height is a sum of cosines of the local bilayer disregistry, maximal on AA,
z = z12 + z23 with z_ij = sum_j cos(d_ij . b_j). AtB is AtA with the top layer shifted by one bond.

The paper's claim, checked quantitatively here: AtA gives a *hexagonal* (triangular) moire lattice of
maxima, one per moire cell, 6 nearest neighbours; AtB gives a *honeycomb*, two per cell, 3 neighbours.

python3 corrugation.py [THETA_DEG=3]
"""
from __future__ import annotations

import json
import os
import math
import sys

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree


def corrugation(theta_deg: float = 3.0, a: float = 10.0, n: int = 220, npix: int = 1500, x0: int = 300, y0: int = 200):
    a1, a2 = a * np.array([1.0, 0.0]), a * np.array([0.5, math.sqrt(3) / 2])
    d = a * np.array([0.0, 0.577])                      # one bond: shifts AA -> AB
    pref = 4 * math.pi / (math.sqrt(3) * a)
    b = pref * np.array([[math.sqrt(3) / 2, -0.5], [0.0, 1.0], [-math.sqrt(3) / 2, -0.5]])
    ij = np.array([(i, j) for i in range(1, n + 1) for j in range(1, n + 1)], float)
    L1 = ij[:, :1] * a1 + ij[:, 1:] * a2
    t = math.radians(theta_deg)
    R = np.array([[math.cos(t), math.sin(t)], [-math.sin(t), math.cos(t)]])   # MATLAB: R1*atom'
    L2 = L1 @ R.T
    z = {}
    t1 = cKDTree(L1)
    t3b = cKDTree(L1 + d)
    _, i1 = t1.query(L2)
    _, i3b = t3b.query(L2)
    zz = lambda dd: np.sum(np.cos(dd @ b.T), axis=1)   # noqa: E731
    z12 = zz(L1[i1] - L2)
    z["AtA"] = z12 + zz(L1[i1] - L2)                     # layer 3 = layer 1
    z["AtB"] = z12 + zz((L1 + d)[i3b] - L2)
    # pixel map (MATLAB: nearest layer-1 site of each pixel, values above 6 dropped, Gaussian blur sigma 5;
    # their window was 556 px, about 3 moire periods; a larger one here so the lattice of maxima can be counted)
    xs = np.arange(x0, x0 + npix)
    ys = np.arange(y0, y0 + npix)
    X, Y = np.meshgrid(xs, ys, indexing="xy")
    _, ip = cKDTree(L2).query(np.stack([X.ravel(), Y.ravel()], axis=1))
    maps = {}
    for k, v in z.items():
        m = np.where(v[ip] < 6, v[ip], 0.0).reshape(npix, npix)
        m = ndimage.gaussian_filter(m, 5)
        m = (m - m.min()) / (m.max() - m.min())
        maps[k] = m
    lam = a / (2 * math.sin(t / 2))                     # moire period (same units as a)
    return maps, lam


def lattice_of_maxima(m: np.ndarray, lam: float, margin: float) -> dict:
    size = max(3, int(lam / 4))
    mx = ndimage.maximum_filter(m, size=size)
    pk = np.argwhere((m == mx) & (m > 0.5))
    n = m.shape[0]
    inner = pk[(pk.min(axis=1) > margin) & (pk.max(axis=1) < n - 1 - margin)]
    tree = cKDTree(pk)
    d, _ = tree.query(inner, k=7)
    nn = d[:, 1]
    coord = [int(np.sum(row[1:] < 1.2 * row[1])) for row in d]
    area = (n - 2 * margin) ** 2
    cell = math.sqrt(3) / 2 * lam ** 2
    return {"n_maxima": int(len(inner)), "maxima_per_moire_cell": len(inner) * cell / area,
            "nn_distance_over_lambda": float(np.median(nn) / lam), "coordination": int(np.median(coord))}


def main(theta: float = 3.0) -> dict:
    maps, lam = corrugation(theta)
    out = {"theta_deg": theta, "lambda": lam}
    for k, m in maps.items():
        out[k] = lattice_of_maxima(m, lam, margin=lam / 2)
    out["AtA_hexagonal"] = out["AtA"]["coordination"] == 6 and abs(out["AtA"]["maxima_per_moire_cell"] - 1) < 0.25
    out["AtB_honeycomb"] = out["AtB"]["coordination"] == 3 and abs(out["AtB"]["maxima_per_moire_cell"] - 2) < 0.4
    np.savez_compressed("corrugation.npz", AtA=maps["AtA"].astype(np.float32), AtB=maps["AtB"].astype(np.float32))
    return out


if __name__ == "__main__":
    res = main(float(sys.argv[1]) if len(sys.argv) > 1 else 3.0)
    json.dump(res, open("corrugation.json", "w"), indent=1)
    print(json.dumps({"dir": os.getcwd(), **{k: res[k] for k in ("AtA_hexagonal", "AtB_honeycomb")}}))
