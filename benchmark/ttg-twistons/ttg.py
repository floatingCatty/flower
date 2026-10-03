"""Continuum model of mirror-symmetric twisted trilayer graphene (TTG), written for this reproduction.

Turkel et al., Science 376, 193 (2022): top (T) and bottom (B) layers aligned, middle layer (M) twisted by
theta. Conventions follow the authors' single-particle script (Dataverse doi:10.7910/DVN/QWIFR1,
SP_continuum.txt) so the two can be compared number for number, but the code is independent and vectorised:
  * plane-wave basis: middle layer on Q = G, outer layers on Q = G - q1 (G a moire reciprocal vector);
  * layer l rotated by l*theta/2 (l = -1 middle, +1 outer) and strained by l*eps/2 (uniaxial heterostrain
    along phi, Poisson ratio 0.16, gauge field beta = 3.14), momentum (I + M)(k + Q + A_strain);
  * Dirac term -hv (k_x - i k_y) on the A-B element; interlayer T_j with w0 (AA) and w1 (AB);
  * D: potential difference top - bottom (top +D/2, bottom -D/2).
One valley (K) is diagonalised; K' follows by time reversal (E_K'(k) = E_K(-k)), so densities of states are
4 x (one valley, one spin) and are reported per flavour.

Energies in eV, momenta in 1/nm.
"""
from __future__ import annotations

import json
import os
import math
import sys

import numpy as np

HBAR_EV_S = 6.582119569e-16
A_GRAPHENE = 0.246  # nm, as in the authors' continuum script
POISSON = 0.16
BETA = 3.14


def rot(x: float) -> np.ndarray:
    return np.array([[math.cos(x), -math.sin(x)], [math.sin(x), math.cos(x)]])


def twist_strain(theta: float, phi: float, eps: float) -> np.ndarray:
    """Small-angle rotation theta plus uniaxial strain eps along phi (Poisson contraction)."""
    return rot(-phi) @ np.diag([eps, -POISSON * eps]) @ rot(phi) + np.array([[0.0, -theta], [theta, 0.0]])


class TTG:
    """Mirror-symmetric TTG, valley K. `H(k)` is linear in k: H = H0 + kx Hx + ky Hy."""

    def __init__(self, theta_deg: float, w1: float, w0: float, hv: float, *, a: float = A_GRAPHENE,
                 eps: float = 0.0, phi: float = 0.0, D: float = 0.0, cut: float = 4.0):
        self.theta_deg, self.w1, self.w0, self.hv, self.a = theta_deg, w1, w0, hv, a
        self.eps, self.phi, self.D, self.cut = eps, phi, D, cut
        th = math.radians(theta_deg)
        kd = 4 * math.pi / (3 * a)
        kD = [np.array([kd, 0.0]), -kd * np.array([0.5, math.sqrt(3) / 2]), kd * np.array([-0.5, math.sqrt(3) / 2])]
        Et = twist_strain(th, phi, eps)
        self.q = np.array([Et @ kD[0], Et @ kD[2], Et @ kD[1]])   # q1, q2, q3 (authors' order)
        self.b = np.array([self.q[1] - self.q[2], self.q[0] - self.q[2]])  # moire reciprocal basis
        self.ktheta = float(np.max(np.linalg.norm(self.q, axis=1)))
        self.area = (2 * math.pi) ** 2 / abs(np.linalg.det(self.b))  # moire cell, nm^2
        R = math.sqrt(3) * self.ktheta * cut
        n = int(cut * 3) + 3
        ij = np.array([(i, j) for i in range(-n, n + 1) for j in range(-n, n + 1)])
        G = ij @ self.b
        c = -self.q[0] / 2   # centre between the two Dirac points (k = 0 and k = q1)
        mid = np.linalg.norm(G - c, axis=1) <= R
        out = np.linalg.norm(G - self.q[0] - c, axis=1) <= R
        # sites: (Q vector, layer 0=middle 1=top 2=bottom, integer label (i, j, layer))
        Qm, Qo = G[mid], G[out] - self.q[0]
        self.Q = np.concatenate([Qm, Qo, Qo])
        self.layer = np.concatenate([np.zeros(len(Qm), int), np.ones(len(Qo), int), np.full(len(Qo), 2)])
        self.ij = np.concatenate([ij[mid], ij[out], ij[out]])
        self.nsite = len(self.Q)
        self.nb = 2 * self.nsite
        self._build()

    # ------------------------------------------------------------------ Hamiltonian
    def _build(self):
        ns, nb = self.nsite, self.nb
        H0 = np.zeros((nb, nb), complex)
        Hx = np.zeros((nb, nb), complex)
        Hy = np.zeros((nb, nb), complex)
        th = math.radians(self.theta_deg)
        A = math.sqrt(3) * BETA / (2 * self.a)
        for i in range(ns):
            l = -1 if self.layer[i] == 0 else 1
            M = twist_strain(l * th / 2, self.phi, l * self.eps / 2)
            E = (M + M.T) / 2
            shift = A * np.array([E[0, 0] - E[1, 1], -2 * E[0, 1]])
            IM = np.eye(2) + M
            c0 = IM @ (self.Q[i] + shift)
            # -hv (kx - i ky) with (kx, ky) = IM @ k + c0
            H0[2 * i, 2 * i + 1] = -self.hv * (c0[0] - 1j * c0[1])
            Hx[2 * i, 2 * i + 1] = -self.hv * (IM[0, 0] - 1j * IM[1, 0])
            Hy[2 * i, 2 * i + 1] = -self.hv * (IM[0, 1] - 1j * IM[1, 1])
            if self.layer[i] == 1:
                H0[2 * i, 2 * i] = H0[2 * i + 1, 2 * i + 1] = self.D / 4   # doubled by + h.c. below
            elif self.layer[i] == 2:
                H0[2 * i, 2 * i] = H0[2 * i + 1, 2 * i + 1] = -self.D / 4
        w = np.exp(2j * math.pi / 3)
        u, up = self.w0, self.w1
        T = [np.array([[u, up], [up, u]]),
             np.array([[u, up / w], [up * w, u]]),
             np.array([[u, up * w], [up / w, u]])]
        key = {(round(x, 6), round(y, 6)): i for i, (x, y) in enumerate(self.Q[self.layer == 0])}
        for i in np.where(self.layer > 0)[0]:
            for p in range(3):
                x, y = self.Q[i] + self.q[p]
                j = key.get((round(x, 6), round(y, 6)))
                if j is not None:
                    H0[2 * i:2 * i + 2, 2 * j:2 * j + 2] = T[p]
        self.H0 = H0 + H0.conj().T
        self.Hx = Hx + Hx.conj().T
        self.Hy = Hy + Hy.conj().T

    def H(self, k) -> np.ndarray:
        k = np.atleast_2d(k)
        return self.H0[None] + k[:, 0, None, None] * self.Hx[None] + k[:, 1, None, None] * self.Hy[None]

    def solve(self, k, vectors: bool = True, chunk: int = 48):
        """Eigenvalues (nk, nb) and optionally eigenvectors (nk, nb, nb) for k (nk, 2)."""
        k = np.atleast_2d(k)
        vals, vecs = [], []
        for s in range(0, len(k), chunk):
            if vectors:
                e, v = np.linalg.eigh(self.H(k[s:s + chunk]))
                vals.append(e)
                vecs.append(v)
            else:
                vals.append(np.linalg.eigvalsh(self.H(k[s:s + chunk])))
        return (np.concatenate(vals), np.concatenate(vecs)) if vectors else np.concatenate(vals)

    def active(self, k, chunk: int = 48):
        """The 4 bands at charge neutrality, D = 0 only: 2 flat (mirror-even sector) + 2 Dirac (mirror-odd).
        The mirror M_z (top <-> bottom) block-diagonalises H; picking by energy index instead would sometimes take
        a remote band where the Dirac cone rises above it. Returns energies (nk, 4) [v_flat, c_flat, v_D, c_D],
        states (nk, nb, 4) in the layer basis, and the flat/Dirac flags."""
        assert self.D == 0, "mirror sectors decouple only at D = 0"
        W = self._mirror_basis()
        ne = int(np.sum(self._even))
        k = np.atleast_2d(k)
        Es, Cs = [], []
        for s in range(0, len(k), chunk):
            H = np.conj(W.T)[None] @ self.H(k[s:s + chunk]) @ W[None]
            ee, ve = np.linalg.eigh(H[:, :ne, :ne])
            eo, vo = np.linalg.eigh(H[:, ne:, ne:])
            ie, io = [ne // 2 - 1, ne // 2], [(self.nb - ne) // 2 - 1, (self.nb - ne) // 2]
            Es.append(np.concatenate([ee[:, ie], eo[:, io]], axis=1))
            Cs.append(np.concatenate([W[None, :, :ne] @ ve[:, :, ie], W[None, :, ne:] @ vo[:, :, io]], axis=2))
        return np.concatenate(Es), np.concatenate(Cs), np.array([True, True, False, False])

    def _mirror_basis(self):
        if getattr(self, "_W", None) is None:
            nb = self.nb
            cols_e, cols_o = [], []
            top = np.where(self.layer == 1)[0]
            bot = np.where(self.layer == 2)[0]     # same order of Q as top (built from the same list)
            for i in np.where(self.layer == 0)[0]:
                for c in (0, 1):
                    v = np.zeros(nb)
                    v[2 * i + c] = 1
                    cols_e.append(v)
            for it, ib in zip(top, bot):
                for c in (0, 1):
                    v = np.zeros(nb)
                    v[2 * it + c], v[2 * ib + c] = 1 / math.sqrt(2), 1 / math.sqrt(2)
                    cols_e.append(v)
                    w = np.zeros(nb)
                    w[2 * it + c], w[2 * ib + c] = 1 / math.sqrt(2), -1 / math.sqrt(2)
                    cols_o.append(w)
            self._W = np.array(cols_e + cols_o).T.astype(complex)
            self._even = np.array([True] * len(cols_e) + [False] * len(cols_o))
        return self._W

    def layer_mask(self, layer: int) -> np.ndarray:
        return np.repeat(self.layer == layer, 2)

    # ------------------------------------------------------------------ k-space
    def grid(self, n: int, shift: float = 0.0) -> np.ndarray:
        """n x n uniform grid over the moire Brillouin zone (parallelogram of b1, b2)."""
        f = (np.arange(n) + shift) / n
        return np.array([i * self.b[0] + j * self.b[1] for i in f for j in f])

    def path(self, npts: int = 60):
        """K - K' - Gamma - M - K (the path of the paper's Fig. 4D/E). Dirac points: 0 (middle) and q1 (outer)."""
        K, Kp = np.zeros(2), self.q[0].copy()
        Gam = rot(math.pi / 3) @ Kp        # equidistant (k_theta) from both Dirac points: a hexagon centre
        M = (K + Kp) / 2                   # mid-point of the K-K' edge
        nodes = [K, Kp, Gam, M, K]
        pts, xs, ticks, x0 = [], [], [0.0], 0.0
        for a_, b_ in zip(nodes[:-1], nodes[1:]):
            L = np.linalg.norm(b_ - a_)
            m = max(2, int(npts * L / self.ktheta))
            t = np.linspace(0, 1, m, endpoint=False)
            pts.extend(a_ + (b_ - a_) * tt for tt in t)
            xs.extend(x0 + L * t)
            x0 += L
            ticks.append(x0)
        pts.append(nodes[-1])
        xs.append(x0)
        return np.array(pts), np.array(xs) / self.ktheta, np.array(ticks) / self.ktheta, ["K", "K'", "Γ", "M", "K"]


# ---------------------------------------------------------------------- spectra
def dos(energies: np.ndarray, weights: np.ndarray, egrid: np.ndarray, sigma: float, kind: str = "gauss",
        nk: int | None = None) -> np.ndarray:
    """Broadened (L)DOS per moire cell per flavour per eV: energies/weights (nk, nbands)."""
    nk = nk or energies.shape[0]
    e = energies.ravel()
    w = weights.ravel()
    pad = (8 if kind == "gauss" else 400) * sigma
    keep = (e > egrid[0] - pad) & (e < egrid[-1] + pad)
    e, w = e[keep], w[keep]
    out = np.zeros_like(egrid)
    for s in range(0, len(e), 20000):
        d = egrid[:, None] - e[None, s:s + 20000]
        if kind == "gauss":
            out += (np.exp(-0.5 * (d / sigma) ** 2) / (sigma * math.sqrt(2 * math.pi))) @ w[s:s + 20000]
        else:
            out += (sigma / math.pi / (d ** 2 + sigma ** 2)) @ w[s:s + 20000]
    return out / nk


def filling_to_mu(energies: np.ndarray, n_cnp: int, nu: float, T: float = 1e-4) -> float:
    """Chemical potential (eV) at filling nu (electrons per moire cell, 4 flavours) for bands energies (nk, nb);
    n_cnp bands per k are filled at charge neutrality."""
    nk = energies.shape[0]
    target = n_cnp + nu / 4.0
    e = np.sort(energies.ravel())
    lo, hi = e[0] - 1, e[-1] + 1
    for _ in range(200):
        mu = 0.5 * (lo + hi)
        x = np.clip((e - mu) / T, -60, 60)
        n = np.sum(1 / (np.exp(x) + 1)) / nk
        lo, hi = (mu, hi) if n < target else (lo, mu)
    return 0.5 * (lo + hi)


def lorentz2(E, c0, c1, a1, e1, g1, a2, e2, g2):
    """Linear background + two Lorentzians (g = FWHM)."""
    L = lambda a, e0, g: a * (0.5 * g) ** 2 / ((E - e0) ** 2 + (0.5 * g) ** 2)
    return c0 + c1 * E + L(a1, e1, g1) + L(a2, e2, g2)


def fit_two_peaks(E, y, window=(-45.0, 45.0), guess=None, background: str = "linear"):
    """Fit background + two Lorentzians to a spectrum (E in meV). background: "linear", "constant" or "none".
    Returns dict with peak positions, FWHMs, separation (meV); peaks ordered by energy."""
    from scipy.optimize import curve_fit
    m = (E >= window[0]) & (E <= window[1])
    x, yy = E[m], y[m] / np.max(y[m])
    if guess is None:
        i = int(np.argmax(yy))
        guess = [0.1, 0.0, 0.8, x[i] - 9, 15, 0.8, x[i] + 9, 15]
    lo = [-1, -1, 0, window[0], 0.2, 0, window[0], 0.2]
    hi = [2, 1, 5, window[1], 80, 5, window[1], 80]
    eps = 1e-12
    if background in ("constant", "none"):
        lo[1], hi[1], guess = -eps, eps, [guess[0], 0.0, *guess[2:]]
    if background == "none":
        lo[0], hi[0], guess = -eps, eps, [0.0, *guess[1:]]
    p, _ = curve_fit(lorentz2, x, yy, p0=guess, bounds=(lo, hi), maxfev=40000)
    pk = sorted([(p[3], p[4], p[2]), (p[6], p[7], p[5])])
    resid = float(np.sqrt(np.mean((lorentz2(x, *p) - yy) ** 2)))
    return {"e_v": pk[0][0], "fwhm_v": pk[0][1], "e_c": pk[1][0], "fwhm_c": pk[1][1],
            "sep": pk[1][0] - pk[0][0], "fwhm_mean": 0.5 * (pk[0][1] + pk[1][1]), "rms": resid,
            "params": [float(v) for v in p]}


def half_max_width(E, y, e0):
    """Full width at half maximum of the peak of y nearest to e0, by linear interpolation (meV in, meV out)."""
    i = int(np.argmin(abs(E - e0)))
    # climb to the local maximum
    while 0 < i < len(y) - 1 and (y[i + 1] > y[i] or y[i - 1] > y[i]):
        i = i + 1 if y[i + 1] > y[i] else i - 1
    h = y[i] / 2
    l = i
    while l > 0 and y[l] > h:
        l -= 1
    r = i
    while r < len(y) - 1 and y[r] > h:
        r += 1
    el = E[l] + (h - y[l]) * (E[l + 1] - E[l]) / (y[l + 1] - y[l]) if y[l + 1] != y[l] else E[l]
    er = E[r - 1] + (h - y[r - 1]) * (E[r] - E[r - 1]) / (y[r] - y[r - 1]) if y[r] != y[r - 1] else E[r]
    return float(E[i]), float(er - el)


# ---------------------------------------------------------------------- parameter sets from the paper
def hv_from(v_ms: float) -> float:
    return HBAR_EV_S * v_ms * 1e9   # eV nm


SP2_HV = 1.3 * 2.1354 * A_GRAPHENE          # authors' SP_continuum.txt: nu = 1.3 * 2.1354 * a
PARAMS = {
    # single particle, ab initio-like monolayer velocity (SP1) and +30 % (SP2); w1 = 105 meV, w0 = 0.83 w1
    "SP1": dict(w1=0.105, w0=0.105 * 0.83, hv=SP2_HV / 1.3),
    "SP2": dict(w1=0.105, w0=0.105 * 0.83, hv=SP2_HV),
}


def model(params: str, theta: float, **kw) -> TTG:
    return TTG(theta, **PARAMS[params], **kw)


# ---------------------------------------------------------------------- one calculation (CLI)
def spectrum(params: str, theta: float, eps: float = 0.0, phi: float = 0.0, D: float = 0.0, nk: int = 48,
             sigma_meV: float = 0.5, cut: float = 4.0, emax_meV: float = 250.0, de_meV: float = 0.1,
             fillings=(), band_path: bool = True, label: str = "") -> dict:
    """Top-layer LDOS (what STM measures), the band-resolved flat-band DOS, VHS separation/widths at CNP,
    and optionally the chemical potential at given fillings and the band structure along K-K'-G-M-K."""
    m = TTG(theta, **PARAMS[params], eps=eps, phi=phi, D=D, cut=cut)
    ks = m.grid(nk, shift=0.5 / nk)         # K over the whole zone = K' (time reversal), also with strain
    e, v = m.solve(ks)
    ns = m.nsite
    top = np.sum(np.abs(v[:, m.layer_mask(1), :]) ** 2, axis=1)       # (nk, nb)
    mid = np.sum(np.abs(v[:, m.layer_mask(0), :]) ** 2, axis=1)
    # mirror parity <M_z> = 2 Re <top|bottom> (Q of top and bottom sites coincide)
    it, ib = m.layer_mask(1), m.layer_mask(2)
    parity = 2 * np.real(np.sum(np.conj(v[:, it, :]) * v[:, ib, :], axis=1))
    eg = np.arange(-emax_meV, emax_meV + de_meV / 2, de_meV)
    s = sigma_meV / 1000
    e_meV = e * 1000
    ldos_top = dos(e, top, eg / 1000, s)
    # the four bands nearest CNP: two mirror-even flat bands and two mirror-odd Dirac bands (at D = 0)
    # (energy order mixes them: the Dirac cone crosses the flat bands), so pick the flat pair per k by parity
    rows = np.arange(len(e))[:, None]
    if D == 0:
        # mirror symmetry (also with the symmetric heterostrain): project exactly in the mirror-even sector.
        # Picking by parity fails where the Dirac cone is degenerate with a flat band (mixed eigenvectors).
        ea, Ca, _ = m.active(ks)
        e_flat = ea[:, :2]
        top_flat = np.sum(np.abs(Ca[:, m.layer_mask(1), :2]) ** 2, axis=1)
        pc = np.array([1.0, 1.0])
    else:
        # D mixes the sectors: the two most mirror-even states nearest zero, one per side, among the central 8
        central = np.arange(ns - 4, ns + 4)
        ec = e[:, central]
        par = parity[:, central]
        below = np.where(np.arange(8)[None] < 4, par, -np.inf).argmax(axis=1)
        above = np.where(np.arange(8)[None] >= 4, par, -np.inf).argmax(axis=1)
        nearest_b = np.where(np.arange(8)[None] < 4, np.where(par > 0.2, ec, -np.inf), -np.inf)
        nearest_a = np.where(np.arange(8)[None] >= 4, np.where(par > 0.2, -ec, -np.inf), -np.inf)
        ok_b, ok_a = np.isfinite(nearest_b.max(axis=1)), np.isfinite(nearest_a.max(axis=1))
        below = np.where(ok_b, nearest_b.argmax(axis=1), below)
        above = np.where(ok_a, nearest_a.argmax(axis=1), above)
        pair = central[np.stack([below, above], axis=1)]
        e_flat, top_flat = e[rows, pair], top[rows, pair]                # (nk, 2): valence, conduction
        pc = parity[rows, pair].mean(axis=0)
    dos_vb = dos(e_flat[:, [0]], top_flat[:, [0]], eg / 1000, s)
    dos_cb = dos(e_flat[:, [1]], top_flat[:, [1]], eg / 1000, s)
    e_vb, w_vb = half_max_width(eg, dos_vb, eg[np.argmax(dos_vb)])
    e_cb, w_cb = half_max_width(eg, dos_cb, eg[np.argmax(dos_cb)])
    out = {
        "label": label, "params": params, "theta": theta, "eps": eps, "phi": phi, "D": D, "nk": nk, "cut": cut,
        "sigma_meV": sigma_meV, "nbasis": m.nb, "area_nm2": m.area,
        "hv_eVnm": m.hv, "v_ms": m.hv / (HBAR_EV_S * 1e9), "w1_meV": m.w1 * 1000, "w0_meV": m.w0 * 1000,
        "e_cnp_meV": float(0.5 * (np.max(e_meV[:, ns - 1]) + np.min(e_meV[:, ns]))),
        "flat_bandwidth_meV": float(1000 * (np.max(e_flat[:, 1]) - np.min(e_flat[:, 0]))),
        "flat_width_v_meV": float(1000 * np.ptp(e_flat[:, 0])), "flat_width_c_meV": float(1000 * np.ptp(e_flat[:, 1])),
        "vhs": {"e_v": e_vb, "e_c": e_cb, "sep": e_cb - e_vb, "fwhm_v": w_vb, "fwhm_c": w_cb,
                "fwhm_mean": 0.5 * (w_vb + w_cb)},
        "flat_parity": [float(pc[0]), float(pc[1])],
        "egrid_meV": [float(eg[0]), float(de_meV), len(eg)],
    }
    try:
        out["fit"] = fit_two_peaks(eg, ldos_top, window=(min(e_vb, e_cb) - 30, max(e_vb, e_cb) + 30),
                                   guess=[0.05, 0, 0.8, e_vb, max(w_vb, 1), 0.8, e_cb, max(w_cb, 1)])
    except Exception as ex:  # noqa: BLE001
        out["fit"] = {"error": str(ex)}
    if fillings:
        out["fillings"] = {}
        for nu in fillings:
            mu = filling_to_mu(e, ns, nu) * 1000
            out["fillings"][str(nu)] = {"mu_meV": mu, "e_v_minus_mu": e_vb - mu, "e_c_minus_mu": e_cb - mu}
    np.savez_compressed("spectrum.npz", egrid_meV=eg, ldos_top=ldos_top, dos_vb=dos_vb, dos_cb=dos_cb,
                        ldos_mid=dos(e, mid, eg / 1000, s))
    if band_path:
        kp, xs, ticks, labels = m.path()
        ep = m.solve(kp, vectors=False) * 1000
        sel = np.arange(ns - 6, ns + 6)
        np.savez_compressed("bands.npz", x=xs, ticks=ticks, e_meV=ep[:, sel], labels=np.array(labels))
    # the full band energies near CNP for later analysis (resonance, fillings)
    np.savez_compressed("eigs.npz", e_meV=e_meV[:, ns - 8:ns + 8], top=top[:, ns - 8:ns + 8], n_below=8,
                        e_flat_meV=e_flat * 1000)
    return out


def main(argv):
    """python3 ttg.py '{"params": "SP2", "theta": 1.55, ...}'  -> writes result.json, spectrum.npz, eigs.npz"""
    cfg = json.loads(argv[1]) if len(argv) > 1 else {}
    res = spectrum(**cfg)
    with open("result.json", "w") as f:
        json.dump(res, f, indent=1)
    v = res["vhs"]
    print(json.dumps({"dir": os.getcwd(), "vhs_sep_meV": v["sep"], "fwhm_mean_meV": v["fwhm_mean"],
                      "flat_bandwidth_meV": res["flat_bandwidth_meV"],
                      "fit_sep_meV": res["fit"].get("sep"), "fit_fwhm_meV": res["fit"].get("fwhm_mean")}))


if __name__ == "__main__":
    main(sys.argv)
