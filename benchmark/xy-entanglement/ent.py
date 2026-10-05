"""Entanglement entropy of a block of L spins in the ground state of the XY chain in a field (Vidal, Latorre, Rico,
Kitaev 2003), by Majorana correlation matrices.

H = - sum_l [ (1+g)/2 sx_l sx_{l+1} + (1-g)/2 sy_l sy_{l+1} + h sz_l ]   (g: anisotropy gamma, h = 1/a in the paper)

Jordan-Wigner: c_{2l} = (prod_{m<l} sz_m) sx_l, c_{2l+1} = (prod_{m<l} sz_m) sy_l, H = (i/4) sum A_jk c_j c_k with A
real antisymmetric; the ground state has <c_j c_k> = delta_jk + i G_jk with G the orthogonal polar factor of A. A
block's entropy is sum_m H2((1 + nu_m)/2) over the L positive eigenvalues nu_m of i G restricted to its 2L Majoranas
(log2, as the paper). The infinite chain: the same polar factor taken in Fourier space, mode by mode, and
transformed back (the block of a translation-invariant chain is block Toeplitz).

  python3 ent.py check                        the method against exact diagonalization and a long finite chain
  python3 ent.py curve G H LMAX               S_L, L = 1..LMAX, of the infinite chain: prints {"L": [...], "S": [...]}
  python3 ent.py points G H L1,L2,...         S_L at the given L only (large blocks), prints {"L": [...], "S": [...]}
"""
import json
import sys

import numpy as np

NPHI = 2 ** 18                                  # Fourier grid of the infinite chain


def couplings(N, g, h, periodic=False):
    A = np.zeros((2 * N, 2 * N))
    J, K = (1 + g) / 2, (1 - g) / 2
    for l in range(N):
        A[2 * l, 2 * l + 1] += 2 * h            # -h sz = i h c_{2l} c_{2l+1}
        if l + 1 < N:
            A[2 * l + 1, 2 * l + 2] += 2 * J    # -J sx sx = i J c_{2l+1} c_{2l+2}
            A[2 * l, 2 * l + 3] -= 2 * K        # -K sy sy = -i K c_{2l} c_{2l+3}
    return A - A.T


def polar(A):
    u, s, vt = np.linalg.svd(A)
    return u @ vt


def entropy_of(Gblock):
    # the spectrum of i G is symmetric (+-nu): take its upper half. Selecting nu > 0 instead miscounts exact zero
    # modes (the XX chain at h = 0, odd blocks), which rounding pushes to either side: one bit too many or too few.
    nu = np.sort(np.linalg.eigvalsh(1j * Gblock).real)[Gblock.shape[0] // 2:]
    nu = np.clip(nu, 0, 1 - 1e-15)
    p = (1 + nu) / 2
    q = 1 - p
    return float(-np.sum(p * np.log2(p) + np.where(q > 0, q * np.log2(np.where(q > 0, q, 1)), 0)))


def finite_chain_entropies(N, g, h, start, Ls):
    G = polar(couplings(N, g, h))
    return [entropy_of(G[2 * start:2 * (start + L), 2 * start:2 * (start + L)]) for L in Ls]


def infinite_symbol(g, h, lmax):
    """G_{(l, a), (m, b)} = Gd[m - l][a, b] for the infinite chain, d = -lmax..lmax. The cell-to-cell couplings
    A0[d] are read off the finite chain's matrix (the same definition as the check), A(phi) = sum_d A0[d] e^{i phi d},
    its polar factor is taken mode by mode and transformed back."""
    A = couplings(5, g, h)
    A0 = {d: A[4:6, 2 * (2 + d):2 * (2 + d) + 2] for d in (-1, 0, 1)}
    phi = 2 * np.pi * np.arange(NPHI) / NPHI
    Am = sum(A0[d][None, :, :] * np.exp(1j * phi * d)[:, None, None] for d in (-1, 0, 1))
    u, s, vt = np.linalg.svd(Am)
    P = u @ vt
    F = np.fft.fft(P, axis=0) / NPHI            # Gd[k] = (1/N) sum_n P(phi_n) e^{-i phi_n k}
    return {int(k): F[k % NPHI].real for k in range(-lmax, lmax + 1)}


def block_from_symbol(Gd, L):
    idx = np.arange(L)
    D = idx[None, :] - idx[:, None]                     # m - l
    stack = np.array([Gd[d] for d in range(-(L - 1), L)])
    blocks = stack[D + L - 1]                           # (L, L, 2, 2)
    return blocks.transpose(0, 2, 1, 3).reshape(2 * L, 2 * L)


def curve(g, h, lmax):
    Gd = infinite_symbol(g, h, lmax)
    return [entropy_of(block_from_symbol(Gd, L)) for L in range(1, lmax + 1)]


def ed_entropies(N, g, h, Ls):
    """Exact diagonalization of the open chain; entropy of the leftmost L spins."""
    from scipy.sparse import csr_matrix, identity, kron
    from scipy.sparse.linalg import eigsh
    sx = csr_matrix([[0, 1], [1, 0]], dtype=complex)
    sy = csr_matrix([[0, -1j], [1j, 0]], dtype=complex)
    sz = csr_matrix([[1, 0], [0, -1]], dtype=complex)

    def op(o, i):
        out = identity(1, format="csr", dtype=complex)
        for k in range(N):
            out = kron(out, o if k == i else identity(2, format="csr", dtype=complex), format="csr")
        return out
    H = 0
    for l in range(N):
        H = H - h * op(sz, l)
        if l + 1 < N:
            H = H - (1 + g) / 2 * op(sx, l) @ op(sx, l + 1) - (1 - g) / 2 * op(sy, l) @ op(sy, l + 1)
    w, v = eigsh(H, k=2, which="SA")
    psi = v[:, np.argmin(w)]
    if abs(w[1] - w[0]) < 1e-9:
        raise SystemExit("degenerate ground state; choose other parameters for the check")
    out = []
    for L in Ls:
        M = psi.reshape(2 ** L, 2 ** (N - L))
        p = np.linalg.svd(M, compute_uv=False) ** 2
        p = p[p > 1e-16]
        out.append(float(-np.sum(p * np.log2(p))))
    return out


def check():
    res, worst = [], 0.0
    for g, h in ((1.0, 1.0), (0.5, 0.7), (1.0, 1.3), (0.0, 0.4), (0.0, 0.0)):   # (0, 0): exact zero modes
        N, Ls = 12, [1, 2, 3, 4, 5, 6]
        a, b = ed_entropies(N, g, h, Ls), finite_chain_entropies(N, g, h, 0, Ls)
        dev = max(abs(x - y) for x, y in zip(a, b))
        worst = max(worst, dev)
        res.append({"g": g, "h": h, "ed_vs_majorana": dev})
    assert worst < 1e-8, res
    # the infinite chain against the middle of a long open chain (gapped: the edges are far)
    inf_dev = 0.0
    for g, h in ((1.0, 1.3), (0.5, 0.7)):
        N, Ls = 4000, [1, 5, 20, 60]
        fin = finite_chain_entropies(N, g, h, N // 2 - 30, Ls)
        Gd = infinite_symbol(g, h, 60)
        infv = [entropy_of(block_from_symbol(Gd, L)) for L in Ls]
        inf_dev = max(inf_dev, max(abs(x - y) for x, y in zip(fin, infv)))
    assert inf_dev < 1e-6, inf_dev
    print(json.dumps({"ok": True, "ed_vs_majorana_max": worst, "infinite_vs_long_chain_max": inf_dev, "cases": res}))


if __name__ == "__main__":
    if sys.argv[1] == "check":
        check()
    elif sys.argv[1] == "points":
        g, h = float(sys.argv[2]), float(sys.argv[3])
        Ls = [int(x) for x in sys.argv[4].split(",")]
        Gd = infinite_symbol(g, h, max(Ls))
        print(json.dumps({"g": g, "h": h, "L": Ls, "S": [entropy_of(block_from_symbol(Gd, L)) for L in Ls]}))
    else:
        g, h, lmax = float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
        print(json.dumps({"g": g, "h": h, "L": list(range(1, lmax + 1)), "S": curve(g, h, lmax)}))
