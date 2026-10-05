"""Diagnostics of tm.py: the block series of gamma_min for a fixed number of steps (no early stopping), for given
(L, W, seed) triples. Prints {"runs": [{L, W, seed, Lambda, rel_err, blocks: [Lambda per block]}]}.

  python3 tmtrace.py STEPS L:W:SEED [L:W:SEED ...]
"""
import json
import sys

import numpy as np

import tm


def trace(L, W, seed, steps):
    LL = L * L
    rng = np.random.default_rng(seed)
    X, _ = np.linalg.qr(rng.standard_normal((2 * LL, LL)))
    P1, P0 = np.ascontiguousarray(X[:LL]), np.ascontiguousarray(X[LL:])
    tm.seed(seed)
    blocks, block, nqr, done = [], 0.0, 0, 0
    while done < steps:
        tm.advance(P1, P0, L, W, 0.0, tm.Q)
        done += tm.Q
        X, R = np.linalg.qr(np.vstack([P1, P0]))
        block += np.log(abs(R[LL - 1, LL - 1]))
        nqr += 1
        P1, P0 = np.ascontiguousarray(X[:LL]), np.ascontiguousarray(X[LL:])
        if nqr % 2000 == 0:
            blocks.append(block / (2000 * tm.Q))
            block = 0.0
    g = np.mean(blocks)
    return {"L": L, "W": W, "seed": seed, "Lambda": 1 / (g * L),
            "rel_err": float(np.std(blocks, ddof=1) / np.sqrt(len(blocks)) / g),
            "blocks": [round(1 / (b * L), 4) for b in blocks]}


if __name__ == "__main__":
    steps = int(sys.argv[1])
    runs = [trace(int(a.split(":")[0]), float(a.split(":")[1]), int(a.split(":")[2]), steps) for a in sys.argv[2:]]
    print(json.dumps({"runs": runs}))
