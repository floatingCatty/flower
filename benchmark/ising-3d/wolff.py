"""Wolff cluster Monte Carlo of the simple-cubic Ising model (periodic L^3) at one coupling K0, recording the time
series of the dimensionless energy E = -sum_<ij> s_i s_j and the magnetization M = sum_i s_i.

  python3 wolff.py L SEED N_MEAS [K0]
    thermalizes for about 2000 sweep-equivalents, then records N_MEAS measurements, each after a fixed number of
    clusters (about L^3 flipped spins on average, set once from the thermalization). Writes series.npz (E, M as int64) and checkpoints the
    chain in $FLOWER_STATE_DIR every few minutes, so a retry resumes. Prints the outputs JSON.
"""
import json
import os
import sys
import time

import numpy as np
from numba import njit

K0_DEFAULT = 0.221654          # the coupling of the paper's runs


@njit(cache=True)
def neighbours(L):
    n = L * L * L
    nb = np.empty((n, 6), dtype=np.int64)
    for x in range(L):
        for y in range(L):
            for z in range(L):
                i = (x * L + y) * L + z
                nb[i, 0] = (((x + 1) % L) * L + y) * L + z
                nb[i, 1] = (((x - 1) % L) * L + y) * L + z
                nb[i, 2] = (x * L + (y + 1) % L) * L + z
                nb[i, 3] = (x * L + (y - 1) % L) * L + z
                nb[i, 4] = (x * L + y) * L + (z + 1) % L
                nb[i, 5] = (x * L + y) * L + (z - 1) % L
    return nb


@njit(cache=True)
def energy(s, nb):
    e = 0
    for i in range(s.size):
        e -= s[i] * (s[nb[i, 0]] + s[nb[i, 2]] + s[nb[i, 4]])
    return e


@njit(cache=True)
def wolff_sweeps(s, nb, p, n_meas, n_cl, stack, m):
    """n_meas measurements, each after n_cl clusters; returns the (E, M) series, the summed cluster size and m.
    The number of clusters between measurements must not depend on the trajectory: stopping once the flipped
    spins reach N (a state-dependent time) over-samples the states right after large clusters, i.e. too ordered
    a distribution (found by the `verify` step: z = 130 against exact enumeration at L = 3)."""
    n = s.size
    E = np.empty(n_meas, dtype=np.int64)
    M = np.empty(n_meas, dtype=np.int64)
    total = 0
    for k in range(n_meas):
        for _ in range(n_cl):
            seed = np.random.randint(n)
            s0 = s[seed]
            s[seed] = -s0
            stack[0] = seed
            top = 1
            size = 1
            while top > 0:
                top -= 1
                i = stack[top]
                for j in range(6):
                    t = nb[i, j]
                    if s[t] == s0:
                        if np.random.random() < p:
                            s[t] = -s0
                            stack[top] = t
                            top += 1
                            size += 1
            total += size
            m -= 2 * s0 * size
        E[k] = energy(s, nb)
        M[k] = m
    return E, M, total, m


def thermalize(s, nb, p, stack, n_sweeps=2000):
    """About n_sweeps sweep-equivalents (in rounds of clusters), and the number of clusters per sweep-equivalent
    from the mean cluster size seen: fixed for the rest of the run."""
    n = s.size
    n_cl = max(1, n // 4)
    done = 0
    sizes = 0
    clusters = 0
    while done < n_sweeps * n:
        _, _, tot, _ = wolff_sweeps(s, nb, p, 10, n_cl, stack, int(s.sum()))
        done += tot
        sizes += tot
        clusters += 10 * n_cl
        n_cl = max(1, int(round(n / (sizes / clusters))))
    return n_cl


def main(L, seed, n_meas, k0):
    np.random.seed(seed)
    _seed_numba(seed)
    nb = neighbours(L)
    n = L ** 3
    p = 1.0 - np.exp(-2.0 * k0)
    state = os.environ.get("FLOWER_STATE_DIR") or "."
    ck = os.path.join(state, "chain.npz")
    t0 = time.time()
    stack = np.empty(n, dtype=np.int64)
    if os.path.exists(ck):
        c = np.load(ck)
        s, E_done, M_done, n_cl = c["s"].copy(), list(c["E"]), list(c["M"]), int(c["n_cl"])
        _seed_numba(int(c["reseed"]))
        resumed = True
    else:
        s = np.ones(n, dtype=np.int64)
        E_done, M_done = [], []
        resumed = False
        n_cl = thermalize(s, nb, p, stack)                                    # ordered start
    chunk = max(100, int(2e8 / (n * 6)))                                      # a few seconds per chunk
    last = time.time()
    while len(E_done) < n_meas:
        k = min(chunk, n_meas - len(E_done))
        E, M, _, _ = wolff_sweeps(s, nb, p, k, n_cl, stack, int(s.sum()))
        E_done += list(E)
        M_done += list(M)
        if time.time() - last > 300:
            reseed = np.random.randint(2 ** 31)
            _seed_numba(reseed)
            np.savez(ck + ".tmp.npz", s=s, E=np.array(E_done), M=np.array(M_done), reseed=reseed, n_cl=n_cl)
            os.replace(ck + ".tmp.npz", ck)
            last = time.time()
    E, M = np.array(E_done[:n_meas]), np.array(M_done[:n_meas])
    np.savez("series.npz", E=E, M=M, L=L, K0=k0, seed=seed, n_cl=n_cl)
    m = np.abs(M) / n
    u4 = 1 - np.mean(m ** 4) / (3 * np.mean(m ** 2) ** 2)
    return {"L": L, "seed": seed, "K0": k0, "n_meas": int(n_meas), "clusters_per_measurement": n_cl,
            "e_mean": float(E.mean() / n), "abs_m_mean": float(m.mean()), "U4_at_K0": float(u4),
            "resumed": resumed, "seconds": round(time.time() - t0, 1)}


@njit(cache=True)
def _seed_numba(seed):
    np.random.seed(seed)


@njit(cache=True)
def exact_moments(L, K):
    """<E>, <M^2>, <M^4> by enumerating all 2^(L^3) states (L = 3: 134 million)."""
    nb = neighbours(L)
    n = L ** 3
    s = np.empty(n, dtype=np.int64)
    z = 0.0
    e1 = 0.0
    m2 = 0.0
    m4 = 0.0
    emin = -3 * n
    for c in range(1 << n):
        mm = 0
        for i in range(n):
            s[i] = 1 if (c >> i) & 1 else -1
            mm += s[i]
        e = 0
        for i in range(n):
            e -= s[i] * (s[nb[i, 0]] + s[nb[i, 2]] + s[nb[i, 4]])
        w = np.exp(-K * (e - emin))
        z += w
        e1 += w * e
        m2 += w * mm * mm
        m4 += w * mm ** 4
    return e1 / z, m2 / z, m4 / z


@njit(cache=True)
def metropolis(L, K, n_meas, seed):
    """Single-spin-flip Metropolis, an independent sampler for the check: E and M once per sweep."""
    np.random.seed(seed)
    nb = neighbours(L)
    n = L ** 3
    s = np.ones(n, dtype=np.int64)
    E = np.empty(n_meas, dtype=np.int64)
    M = np.empty(n_meas, dtype=np.int64)
    for k in range(n_meas + 1000):
        for _ in range(n):
            i = np.random.randint(n)
            h = 0
            for j in range(6):
                h += s[nb[i, j]]
            if np.random.random() < np.exp(-2.0 * K * s[i] * h):
                s[i] = -s[i]
        if k >= 1000:
            e = 0
            for i in range(n):
                e -= s[i] * (s[nb[i, 0]] + s[nb[i, 2]] + s[nb[i, 4]])
            E[k - 1000] = e
            M[k - 1000] = s.sum()
    return E, M


def check(n_meas=200000, K=K0_DEFAULT, L=3):
    """The kernel against exact enumeration at L = 3, and the neighbour table's symmetry for several L."""
    for LL in (3, 4, 5, 8):
        nb = neighbours(LL)
        for i in range(LL ** 3):
            for t in nb[i]:
                if not (0 <= t < LL ** 3) or i not in nb[t]:
                    raise SystemExit("neighbour table broken at L=%d, site %d -> %d" % (LL, i, t))
    ee, em2, em4 = exact_moments(L, K)
    _seed_numba(7)
    nb = neighbours(L)
    s = np.ones(L ** 3, dtype=np.int64)
    stack = np.empty(L ** 3, dtype=np.int64)
    n_cl = thermalize(s, nb, 1 - np.exp(-2 * K), stack)
    E, M, _, _ = wolff_sweeps(s, nb, 1 - np.exp(-2 * K), n_meas, n_cl, stack, int(s.sum()))
    Em, Mm = metropolis(L, K, n_meas, 11)
    out = {}
    for sampler, (Es, Ms) in (("wolff", (E, M)), ("metropolis", (Em, Mm))):
        Es, Ms = Es.astype(float), Ms.astype(float)
        for name, x, ref in (("E", Es, ee), ("M2", Ms ** 2, em2), ("M4", Ms ** 4, em4)):
            blocks = x[: len(x) // 50 * 50].reshape(50, -1).mean(axis=1)
            err = blocks.std(ddof=1) / np.sqrt(50)
            out["%s_%s" % (sampler, name)] = {"mc": float(x.mean()), "exact": float(ref),
                                              "z": float((x.mean() - ref) / err)}
    out["ok"] = all(abs(v["z"]) < 4 for v in out.values() if isinstance(v, dict))
    return out


if __name__ == "__main__":
    if sys.argv[1] == "check":
        res = check()
        print(json.dumps(res))
        sys.exit(0 if res["ok"] else 1)
    L, seed, n_meas = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    k0 = float(sys.argv[4]) if len(sys.argv) > 4 else K0_DEFAULT
    print(json.dumps(main(L, seed, n_meas, k0)))
