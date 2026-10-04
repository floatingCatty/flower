"""Stochastic series expansion QMC (operator-loop updates) of the S=1/2 Heisenberg antiferromagnet on the periodic
L x L square lattice, H = J sum_<ij> S_i.S_j (J = 1), after Sandvik, PRB 56, 11678 (1997) and PRB 59, R14157 (1999).

  python3 sse.py L BETA SWEEPS SEED       production: equilibration, then SWEEPS measurement sweeps in bins
  python3 sse.py check                    L = 4 against exact diagonalization (the paper's Table I)

Per measurement sweep: n (energy), the staggered moment averaged over imaginary time (S(pi,pi)), the correlation at
(L/2, L/2) (a few time slices), winding numbers (stiffness) and the imaginary-time integrated m(q = 2pi/L, 0)
(susceptibility). Bin averages go to bins.npz; the chain is checkpointed in $FLOWER_STATE_DIR. Prints JSON.
"""
import json
import os
import sys
import time

import numpy as np
from numba import njit


def lattice(L):
    N = L * L
    nb = 2 * N
    bsites = np.empty((nb, 2), dtype=np.int64)
    bdir = np.empty(nb, dtype=np.int64)          # 0: x bond, 1: y bond
    for y in range(L):
        for x in range(L):
            i = x + y * L
            bsites[2 * i] = (i, (x + 1) % L + y * L); bdir[2 * i] = 0
            bsites[2 * i + 1] = (i, x + ((y + 1) % L) * L); bdir[2 * i + 1] = 1
    stag = np.array([(-1) ** (i % L + i // L) for i in range(N)], dtype=np.int64)
    return bsites, bdir, stag


@njit(cache=True)
def seed_rng(s):
    np.random.seed(s)


@njit(cache=True)
def diagonal_update(spin, ops, bsites, beta, n):
    nb = bsites.shape[0]
    M = ops.size
    for p in range(M):
        op = ops[p]
        if op == -1:
            b = np.random.randint(nb)
            if spin[bsites[b, 0]] != spin[bsites[b, 1]]:
                if np.random.random() * (M - n) < 0.5 * beta * nb:
                    ops[p] = 2 * b
                    n += 1
        elif op % 2 == 0:
            if np.random.random() * 0.5 * beta * nb < (M - n + 1):
                ops[p] = -1
                n -= 1
        else:
            b = op // 2
            spin[bsites[b, 0]] = -spin[bsites[b, 0]]
            spin[bsites[b, 1]] = -spin[bsites[b, 1]]
    return n


@njit(cache=True)
def sweep(spin, ops, bsites, beta, n, N):
    n = diagonal_update(spin, ops, bsites, beta, n)
    M = ops.size
    vlink = np.full(4 * M, -1, dtype=np.int64)
    first = np.full(N, -1, dtype=np.int64)
    last = np.full(N, -1, dtype=np.int64)
    for p in range(M):
        op = ops[p]
        if op == -1:
            continue
        b = op // 2
        v0 = 4 * p
        for k in range(2):
            s = bsites[b, k]
            v1 = last[s]
            vleg = v0 + k
            if v1 != -1:
                vlink[v1] = vleg
                vlink[vleg] = v1
            else:
                first[s] = vleg
            last[s] = v0 + k + 2
    for s in range(N):
        if first[s] != -1:
            vlink[first[s]] = last[s]
            vlink[last[s]] = first[s]
    for v0 in range(0, 4 * M, 2):
        if vlink[v0] < 0:
            continue
        v1 = v0
        if np.random.random() < 0.5:
            while True:
                ops[v1 // 4] ^= 1
                vlink[v1] = -2          # visited and flipped
                v2 = v1 ^ 1
                v1 = vlink[v2]
                vlink[v2] = -2
                if v1 == v0:
                    break
        else:
            while True:
                vlink[v1] = -1 - 4 * M  # visited, not flipped
                v2 = v1 ^ 1
                v1 = vlink[v2]
                vlink[v2] = -1 - 4 * M
                if v1 == v0:
                    break
    for s in range(N):
        if first[s] != -1:
            if vlink[first[s]] == -2:
                spin[s] = -spin[s]
        elif np.random.random() < 0.5:
            spin[s] = -spin[s]
    return n


@njit(cache=True)
def measure(spin, ops, bsites, bdir, stag, L, beta, n, cq, sq, nslice_c):
    """Propagate through the operator string once: staggered moment averaged over time, C(L/2,L/2) on a few
    slices, winding currents, and the time-integrated m(q) for chi(q)."""
    N = L * L
    s = spin.copy()
    ms = 0.0
    mqr = 0.0
    mqi = 0.0
    for i in range(N):
        ms += stag[i] * s[i]
        mqr += cq[i] * s[i]
        mqi += sq[i] * s[i]
    ms *= 0.5; mqr *= 0.5; mqi *= 0.5
    ms2sum = 0.0
    sumr = 0.0
    sumi = 0.0
    sq_sum = 0.0
    wx = 0
    wy = 0
    csum = 0.0
    cnt_c = 0
    M = ops.size
    step_c = max(1, n // nslice_c) if n > 0 else 1
    k = 0
    r = L // 2
    for p in range(M):
        op = ops[p]
        if op == -1:
            continue
        if op % 2 == 1:
            b = op // 2
            i = bsites[b, 0]
            j = bsites[b, 1]
            # an up spin moving i -> j counts +1 in the bond's direction
            d = 1 if s[i] == 1 else -1
            if bdir[b] == 0:
                wx += d
            else:
                wy += d
            s[i] = -s[i]
            s[j] = -s[j]
            ms += 2 * stag[i] * s[i]        # each spin changes by s_new; the two staggered changes are equal
            mqr += cq[i] * s[i] + cq[j] * s[j]
            mqi += sq[i] * s[i] + sq[j] * s[j]
        ms2sum += ms * ms
        sumr += mqr
        sumi += mqi
        sq_sum += mqr * mqr + mqi * mqi
        if k % step_c == 0 and cnt_c < nslice_c:
            c = 0.0
            for x in range(L):
                for y in range(L):
                    a = x + y * L
                    bb = (x + r) % L + ((y + r) % L) * L
                    c += s[a] * s[bb]
            csum += 0.25 * c / N
            cnt_c += 1
        k += 1
    if n > 0:
        ms2 = ms2sum / n
        chi = beta / (n * (n + 1.0)) * (sumr * sumr + sumi * sumi + sq_sum) / N
    else:
        ms2 = ms * ms
        chi = beta * (mqr * mqr + mqi * mqi) / N
    if cnt_c == 0:
        c = 0.0
        for x in range(L):
            for y in range(L):
                c += s[x + y * L] * s[(x + r) % L + ((y + r) % L) * L]
        csum = 0.25 * c / N
        cnt_c = 1
    bad = 0
    for i in range(N):
        if s[i] != spin[i]:
            bad = 1          # the propagated state did not return to the stored one
    if wx % L != 0 or wy % L != 0:
        bad += 2             # a net current that is not a whole winding
    return ms2 / N, csum / cnt_c, (wx / L) ** 2 + (wy / L) ** 2, chi, bad


def run(L, beta, sweeps, seed, nbins=100, therm=None, state_dir=None):
    bsites, bdir, stag = lattice(L)
    N = L * L
    xs = np.arange(N) % L
    cq = np.cos(2 * np.pi * xs / L)
    sq = np.sin(2 * np.pi * xs / L)
    ck = os.path.join(state_dir, "chain.npz") if state_dir else None
    seed_rng(seed)
    rng = np.random.default_rng(seed)
    if ck and os.path.exists(ck):
        c = np.load(ck)
        spin, ops, n = c["spin"].copy(), c["ops"].copy(), int(c["n"])
        bins = [tuple(x) for x in c["bins"]]
        seed_rng(int(c["reseed"]))
        resumed = True
    else:
        spin = np.array([1 if (stag[i] > 0) else -1 for i in range(N)], dtype=np.int64)   # Neel, m_z = 0
        M = max(20, int(beta * N / 4))
        ops = np.full(M, -1, dtype=np.int64)
        n = 0
        therm = therm if therm is not None else max(2000, sweeps // 10)
        for t in range(therm):
            n = sweep(spin, ops, bsites, beta, n, N)
            if n > 0.75 * ops.size:                 # grow the expansion cutoff (Sandvik's l), only while equilibrating
                new = np.full(int(n * 4 / 3) + 10, -1, dtype=np.int64)
                new[:ops.size] = ops
                ops = new
        bins = []
        resumed = False
    per_bin = max(1, sweeps // nbins)
    invalid = np.zeros(4, dtype=np.int64)   # counts of [valid, not periodic, fractional winding, both]
    t0 = time.time()
    last = time.time()
    while len(bins) < nbins:
        acc = np.zeros(5)
        for t in range(per_bin):
            n = sweep(spin, ops, bsites, beta, n, N)
            ms2, cr, w2, chi, bad = measure(spin, ops, bsites, bdir, stag, L, beta, n, cq, sq, 8)
            acc += (n, ms2, cr, w2, chi)
            invalid[bad] += 1
        bins.append(tuple(acc / per_bin))
        if ck and time.time() - last > 300:
            reseed = int(rng.integers(2 ** 31))
            seed_rng(reseed)
            np.savez(ck + ".tmp.npz", spin=spin, ops=ops, n=n, bins=np.array(bins), reseed=reseed)
            os.replace(ck + ".tmp.npz", ck)
            last = time.time()
    B = np.array(bins)
    out = {"n": B[:, 0], "S_pipi": B[:, 1], "C_half": B[:, 2], "w2": B[:, 3], "chi_q": B[:, 4]}
    E = -out["n"] / (beta * N) + 0.5
    return {"L": L, "beta": beta, "N": N, "E": E, "S": out["S_pipi"], "C": out["C_half"],
            "rho": 3 * out["w2"] / (4 * beta), "chi": 1.5 * out["chi_q"], "resumed": resumed,
            "seconds": time.time() - t0, "cutoff": int(ops.size), "invalid": invalid.tolist()}


def summary(r):
    def me(x):
        return float(np.mean(x)), float(np.std(x, ddof=1) / np.sqrt(len(x)))
    return {k: me(r[k]) for k in ("E", "S", "C", "rho", "chi")}


def check():
    """L = 4 against exact diagonalization: E and S(pi,pi) from the paper's Table I; the stiffness from the
    `ed-twist` step (rho_s = (3/2)(1/N) d^2E0/dphi^2 = 0.278286). The paper quotes 0.04840 for its finite-size
    rho_s(4), a different normalization at finite L (all definitions agree as L -> infinity)."""
    r = run(4, 32.0, 200000, 7, nbins=100)
    s = summary(r)
    exact = {"E": -0.701780, "S": 1.47481, "rho": 0.278286}
    out = {k: {"qmc": s[k][0], "err": s[k][1], "exact": v, "z": (s[k][0] - v) / s[k][1]} for k, v in exact.items()}
    out["configurations"] = {"valid": r["invalid"][0], "not_periodic": r["invalid"][1],
                             "fractional_winding": r["invalid"][2], "both": r["invalid"][3]}
    out["ok"] = all(abs(v["z"]) < 4 for k, v in out.items() if isinstance(v, dict) and "z" in v) and \
        sum(r["invalid"][1:]) == 0
    return out


if __name__ == "__main__":
    if sys.argv[1] == "check":
        res = check()
        print(json.dumps(res))
        sys.exit(0 if res["ok"] else 1)
    L, beta, sweeps, seed = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    r = run(L, beta, sweeps, seed, state_dir=os.environ.get("FLOWER_STATE_DIR"))
    np.savez("bins.npz", **{k: r[k] for k in ("E", "S", "C", "rho", "chi")}, L=L, beta=beta)
    s = summary(r)
    print(json.dumps({"L": L, "beta": beta, "seed": seed, "sweeps": sweeps, "resumed": r["resumed"],
                      "seconds": round(r["seconds"], 1), "cutoff": r["cutoff"],
                      **{k: s[k][0] for k in s}, **{k + "_err": s[k][1] for k in s}}))
