"""Localization length of the 3D Anderson model on an L x L bar by the transfer-matrix method (Slevin & Ohtsuki 1999).

H = sum_r V(r) |r><r| - sum_<r,r'> |r><r'|, nearest-neighbour hopping 1, V uniform in [-W/2, W/2] (box), periodic
boundaries across the bar, energy E = 0. Slice by slice along the bar, psi_{n+1} = (E - V_n) psi_n + sum_nb psi_n
- psi_{n-1} (the hopping inside the slice gives the neighbour sum), applied to M = L^2 columns at once; every Q
steps the 2L^2 x M block [psi_n; psi_{n-1}] is re-orthonormalized by QR and log|R_ii| accumulated. The smallest
positive Lyapunov exponent gamma is the M-th; Lambda = 1 / (gamma L) is the reduced localization length.

  python3 tm.py run L W [TARGET_REL_ERR] [MAX_STEPS] [SEED]   -> tm.json, prints the outputs JSON
  python3 tm.py check                                          -> the kernel against a dense calculation

The error of gamma is the standard error over blocks of 2000 re-orthonormalizations; a run stops when it is below
TARGET_REL_ERR (default 0.002) or at MAX_STEPS.
"""
import json
import sys
import time

import numpy as np
from numba import njit

Q = 8                                     # slices between re-orthonormalizations


@njit(cache=True)
def advance(P1, P0, L, W, E, nslices, seed_state):
    """nslices slices for all columns of P1 (psi_n) and P0 (psi_{n-1}), each (L*L, M), in place.
    seed_state: a 1-element int64 array, the running seed of the potential (reproducible per run)."""
    LL = L * L
    M = P1.shape[1]
    np.random.seed(seed_state[0])
    new = np.empty((LL, M))
    for _ in range(nslices):
        V = (np.random.random(LL) - 0.5) * W
        for i in range(LL):
            x, y = i % L, i // L
            nb = (((x + 1) % L) + y * L, ((x - 1) % L) + y * L, x + ((y + 1) % L) * L, x + ((y - 1) % L) * L)
            e = E - V[i]
            for m in range(M):
                new[i, m] = e * P1[i, m] + P1[nb[0], m] + P1[nb[1], m] + P1[nb[2], m] + P1[nb[3], m] - P0[i, m]
        P0[:, :] = P1
        P1[:, :] = new
    seed_state[0] = np.random.randint(0, 2 ** 31 - 1)


def lyapunov(L, W, target=0.002, max_steps=10 ** 7, seed=12345, E=0.0, ncols=None, min_steps=20000):
    LL = L * L
    M = ncols or LL
    rng = np.random.default_rng(seed)
    X, _ = np.linalg.qr(rng.standard_normal((2 * LL, M)))
    P1, P0 = np.ascontiguousarray(X[:LL]), np.ascontiguousarray(X[LL:])
    state = np.array([seed], dtype=np.int64)
    acc = np.zeros(M)
    block, blocks, nqr, steps = 0.0, [], 0, 0
    while True:
        advance(P1, P0, L, W, E, Q, state)
        steps += Q
        X, R = np.linalg.qr(np.vstack([P1, P0]))
        d = np.log(np.abs(np.diag(R)))
        acc += d
        block += d[M - 1]
        nqr += 1
        P1, P0 = np.ascontiguousarray(X[:LL]), np.ascontiguousarray(X[LL:])
        if nqr % 2000 == 0:
            blocks.append(block / (2000 * Q))
            block = 0.0
            if len(blocks) >= 10 and steps >= min_steps:
                g = np.mean(blocks)
                err = np.std(blocks, ddof=1) / np.sqrt(len(blocks))
                if err / g < target or steps >= max_steps:
                    return acc / steps, float(g), float(err), steps


def check():
    """The numba kernel against a dense numpy transfer matrix on the same potentials (L = 2, 3; all 2L^2 exponents),
    and the symplectic structure of the transfer matrix (singular values of a product pair as s, 1/s)."""
    out = {}
    for L in (2, 3):
        LL, W, n = L * L, 16.5, 400
        Tperp = np.zeros((LL, LL))
        for i in range(LL):
            x, y = i % L, i // L
            for j in (((x + 1) % L) + y * L, ((x - 1) % L) + y * L, x + ((y + 1) % L) * L, x + ((y - 1) % L) * L):
                Tperp[i, j] += 1.0
        state = np.array([7], dtype=np.int64)
        X = np.linalg.qr(np.random.default_rng(1).standard_normal((2 * LL, 2 * LL)))[0]
        P1, P0 = np.ascontiguousarray(X[:LL]), np.ascontiguousarray(X[LL:])
        acc_k = np.zeros(2 * LL)
        acc_d = np.zeros(2 * LL)
        Y = X.copy()
        np.random.seed(7)                     # the same stream the kernel draws from, slice by slice
        for k in range(n):
            st = state[0]
            advance(P1, P0, L, W, 0.0, 1, state)
            np.random.seed(st)
            V = (np.random.random(LL) - 0.5) * W
            T = np.block([[np.diag(-V) + Tperp, -np.eye(LL)], [np.eye(LL), np.zeros((LL, LL))]])
            Y = T @ Y
            Xk, Rk = np.linalg.qr(np.vstack([P1, P0]))
            Yd, Rd = np.linalg.qr(Y)
            acc_k += np.log(np.abs(np.diag(Rk)))
            acc_d += np.log(np.abs(np.diag(Rd)))
            P1, P0 = np.ascontiguousarray(Xk[:LL]), np.ascontiguousarray(Xk[LL:])
            Y = Yd
        gk, gd = np.sort(acc_k / n)[::-1], np.sort(acc_d / n)[::-1]
        dev = float(np.max(np.abs(gk - gd)))
        # symplectic: the singular values of a product of transfer matrices pair as s, 1/s exactly (a short
        # product, 5 slices, so the smallest ones are still resolved in double precision)
        rng = np.random.default_rng(3)
        Pm = np.eye(2 * LL)
        for k in range(5):
            V = (rng.random(LL) - 0.5) * W
            Pm = np.block([[np.diag(-V) + Tperp, -np.eye(LL)], [np.eye(LL), np.zeros((LL, LL))]]) @ Pm
        ls = np.log(np.linalg.svd(Pm, compute_uv=False))
        pair = float(np.max(np.abs(ls + ls[::-1])))
        out[f"L{L}"] = {"kernel_vs_dense": dev, "symplectic_pairing": pair, "gamma": gd[:LL].tolist()}
        assert dev < 1e-9, (L, dev)
        assert pair < 1e-6, (L, pair)
    print(json.dumps({"ok": True, **{k + "_dev": v["kernel_vs_dense"] for k, v in out.items()},
                      **{k + "_pair": v["symplectic_pairing"] for k, v in out.items()}}))


def run(L, W, target=0.002, max_steps=10 ** 7, seed=12345):
    t0 = time.time()
    gam, g, err, steps = lyapunov(L, W, target, max_steps, seed)
    out = {"L": L, "W": W, "gamma_min": g, "gamma_err": err, "Lambda": 1 / (g * L), "Lambda_err": err / g / (g * L),
           "rel_err": err / g, "steps": steps, "seed": seed, "seconds": round(time.time() - t0, 1)}
    json.dump(out, open("tm.json", "w"), indent=1)
    print(json.dumps(out))


if __name__ == "__main__":
    if sys.argv[1] == "check":
        check()
    else:
        a = sys.argv[2:]
        run(int(a[0]), float(a[1]), *[f(x) for f, x in zip((float, int, int), a[2:])])
