"""Projected Hartree-Fock for mirror-symmetric TTG, written for this reproduction (not the authors' code).

The scheme of the paper (Christos, Sachdev & Scheurer; script selfconsistent_TTG_eps10_repo.py on Dataverse):
  * the 4 bands nearest charge neutrality per valley and spin (2 flat, mirror-even; 2 Dirac, mirror-odd);
  * flavour-symmetric state (no symmetry breaking): one valley computed, the other by time reversal, 4 flavours;
  * Hartree (G != 0) and Fock from the occupied active states, no reference-density subtraction;
  * gate-screened Coulomb V(q) = e^2 / (2 eps eps0 q) (1 - exp(-2 q d)), eps = 10, d = 40 nm;
  * parameters: theta = 1.53 deg, w_AB = 0.414 hbar v kD theta, w_AA = (8/11) w_AB, hbar v = 0.660 eV nm.
Differences from the authors' implementation: uniform (shifted) Monkhorst-Pack grid, mixing to convergence
instead of 13 plain iterations, and the converged self-energy evaluated on a fine grid (one-shot, using the
coarse-grid density matrix) for the spectra instead of broadening the coarse-grid levels.

python3 ttg_hf.py '{"nu": 0, "nk": 12, "nk_fine": 36}'
"""
from __future__ import annotations

import json
import os
import math
import sys
import time

import numpy as np

from ttg import TTG, dos, fit_two_peaks, half_max_width

E2_EPS0 = 4 * math.pi / 137.036 * 197.327   # e^2/eps0 = 4 pi alpha hbar c, in eV nm


def coulomb(q, eps_r: float, d: float):
    q = np.asarray(q, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = E2_EPS0 / (2 * eps_r * q) * (1 - np.exp(-2 * q * d))
    return np.where(q < 1e-9, E2_EPS0 / (2 * eps_r) * 2 * d, v)   # eV nm^2


def hf_model(theta: float = 1.53, alpha: float = 0.414, kappa: float = 8 / 11, hv: float = 0.660,
             a: float = 0.142 * math.sqrt(3), cut: float = 4.0) -> TTG:
    kD = 4 * math.pi / (3 * a)
    w1 = alpha * hv * kD * math.radians(theta)
    return TTG(theta, w1=w1, w0=kappa * w1, hv=hv, a=a, cut=cut)


class ProjectedHF:
    def __init__(self, m: TTG, nk: int, gshells: float = 2.0, eps_r: float = 10.0, d: float = 40.0,
                 hartree: bool = True, fock: bool = True, subtract: str = "none", active: str = "mirror",
                 hartree_shells: float | None = None, c2zt: bool = True):
        """subtract: "none" (the paper's scheme: density of the occupied active states) or "cnp" (relative to
        the charge-neutral reference P0 = 1/2 on the active bands)."""
        self.m, self.nk, self.eps_r, self.d = m, nk, eps_r, d
        self.hartree, self.fock, self.subtract, self.active_mode = hartree, fock, subtract, active
        self.c2zt = c2zt
        self.k = m.grid(nk, shift=0.5 / nk)          # avoids the Dirac points (k = 0, q1)
        self.N = len(self.k)
        self.e0, self.C = self._active(self.k)          # (N, 4) eV, (N, nb, 4): 2 flat + 2 Dirac
        bl = np.linalg.norm(m.b[0])
        mn = [(i, j) for i in range(-4, 5) for j in range(-4, 5)]
        self.G = [(i, j) for i, j in mn if np.linalg.norm(i * m.b[0] + j * m.b[1]) <= gshells * bl + 1e-9]
        self.Gvec = np.array([i * m.b[0] + j * m.b[1] for i, j in self.G])
        self.g0 = self.G.index((0, 0))
        self.gneg = [self.G.index((-i, -j)) for i, j in self.G]
        key = {(int(i), int(j), int(l)): s for s, ((i, j), l) in enumerate(zip(m.ij, m.layer))}
        # basis row b of a state at k + G  <-  row shift[g][b] of the state at k (plane-wave relabelling)
        self.shift = []
        for gi, gj in self.G:
            idx = np.full(m.nb, -1)
            for s in range(m.nsite):
                t = key.get((int(m.ij[s][0]) + gi, int(m.ij[s][1]) + gj, int(m.layer[s])))
                if t is not None:
                    idx[2 * s], idx[2 * s + 1] = 2 * t, 2 * t + 1
            self.shift.append(idx)
        self.Lam = self.form_factors(self.C)                       # (N, N, nG, 4, 4)
        # C2zT (complex conjugation x sublattice swap, k -> k) in the band basis: S_k = C^+ X conj(C).
        # The paper's state has "no additional symmetry breaking"; without this constraint the iteration slides
        # into a C2zT-broken (sublattice-polarised) state with a ~60 meV exchange gap.
        swap = np.arange(m.nb) ^ 1
        self.S = np.einsum("kbm,kbn->kmn", np.conj(self.C), np.conj(self.C)[:, swap, :])
        q = self.k[None, :, None, :] + self.Gvec[None, None] - self.k[:, None, None, :]
        self.V = coulomb(np.linalg.norm(q, axis=-1), eps_r, d)      # (N, N, nG)
        self.VG = coulomb(np.linalg.norm(self.Gvec, axis=1), eps_r, d)
        if hartree_shells is not None:   # the authors' Hartree runs over the first shell of G only
            self.VG = np.where(np.linalg.norm(self.Gvec, axis=1) <= hartree_shells * bl + 1e-9, self.VG, 0.0)
        self.pref = 1.0 / (self.N * m.area)

    def _active(self, k):
        if self.active_mode == "index":   # the authors' choice: the 4 bands nearest CNP by energy index
            e, v = self.m.solve(k)
            band = np.arange(self.m.nsite - 2, self.m.nsite + 2)
            return e[:, band], v[:, :, band]
        e, C, _ = self.m.active(k)
        return e, C

    def shifted(self, C, g):
        idx = self.shift[g]
        out = np.zeros_like(C)
        ok = idx >= 0
        out[:, ok, :] = C[:, idx[ok], :]
        return out

    def form_factors(self, Ck):
        """Lam[k, k', g]_{m a} = sum_Q conj(Ck[k](Q)) C[k'](Q + G_g)  (Ck: states at k, C: grid states)."""
        n1, n2 = len(Ck), self.N
        L = np.empty((n1, n2, len(self.G), 4, 4), complex)
        A = np.conj(Ck).transpose(0, 2, 1).reshape(n1 * 4, -1)    # (n1*4, nb)
        for g in range(len(self.G)):
            B = self.shifted(self.C, g).transpose(1, 0, 2).reshape(self.m.nb, n2 * 4)
            L[:, :, g] = (A @ B).reshape(n1, 4, n2, 4).transpose(0, 2, 1, 3)
        return L

    def density(self, P):
        """n(G) per moire cell area, 4 flavours: 4/(N A) sum_k Tr(P_k Lam[k,k,G])."""
        diag = self.Lam[np.arange(self.N), np.arange(self.N)]     # (N, nG, 4, 4)
        return 4 * self.pref * np.einsum("kab,kgba->g", P, diag)

    def self_energy(self, P, Lam=None, V=None, diagLam=None):
        """Hartree + Fock in the band basis; Lam/V for other (e.g. fine-grid) k points."""
        Lam = self.Lam if Lam is None else Lam
        V = self.V if V is None else V
        if self.subtract == "cnp":
            P = P - 0.5 * np.eye(4)[None]
        X = np.einsum("klgma,lab->klgmb", Lam, P)
        SF = -self.pref * np.einsum("klg,klgmb,klgnb->kmn", V, X, np.conj(Lam))
        nG = self.density(P)
        nG[self.g0] = 0.0
        dL = self.Lam[np.arange(self.N), np.arange(self.N)] if diagLam is None else diagLam   # (n, nG, 4, 4)
        SH = np.einsum("g,kgmn->kmn", self.VG * nG, dL[:, self.gneg])
        S = SF * self.fock + SH * self.hartree
        return 0.5 * (S + np.conj(S.transpose(0, 2, 1))), nG

    @staticmethod
    def occupy(E, nu, T):
        """Chemical potential and occupations for filling nu (4 flavours; 2 of 4 bands per flavour at CNP)."""
        target = (2 + nu / 4) * E.shape[0]
        lo, hi = E.min() - 1, E.max() + 1
        for _ in range(100):
            mu = 0.5 * (lo + hi)
            f = 1 / (np.exp(np.clip((E - mu) / T, -60, 60)) + 1)
            lo, hi = (mu, hi) if f.sum() < target else (lo, mu)
        return mu, f

    def run(self, nu=0.0, T=5e-4, mix=0.4, tol=1e-7, maxit=300, log=print):
        E = self.e0.copy()
        mu, f = self.occupy(E, nu, T)
        P = np.einsum("ka,ka->ka", f, np.ones_like(f))[:, :, None] * np.eye(4)[None]   # band basis, diagonal
        hist = []
        for it in range(maxit):
            S, nG = self.self_energy(P)
            H = np.einsum("ka,ab->kab", self.e0, np.eye(4)) + S
            E, U = np.linalg.eigh(H)
            mu, f = self.occupy(E, nu, T)
            Pn = np.einsum("kam,km,kbm->kab", U, f, np.conj(U))
            if self.c2zt:
                Pn = 0.5 * (Pn + self.S @ np.conj(Pn) @ np.conj(np.transpose(self.S, (0, 2, 1))))
            dP = float(np.max(abs(Pn - P)))
            P = mix * Pn + (1 - mix) * P
            hist.append(dP)
            if it % 10 == 0 or dP < tol:
                log(f"it {it:3d}  dP {dP:.2e}  mu {1000 * mu:8.3f} meV")
            if dP < tol:
                break
        self.P, self.E, self.U, self.mu, self.nG = P, E, U, mu, nG
        # diagnostics comparable with the authors' run: mean self-energy per active band (flat v, flat c,
        # Dirac v, Dirac c; meV, incl. Hartree), and the mean occupation of each band
        diag = np.real(np.einsum("kaa->ka", S)).mean(axis=0) * 1000
        occ = np.real(np.einsum("kaa->ka", P)).mean(axis=0)
        return {"iterations": it + 1, "converged": dP < tol, "dP": dP, "mu_meV": 1000 * mu, "history": hist,
                "sigma_diag_meV": [float(x) for x in diag], "occupation": [float(x) for x in occ],
                "sigma_order": ["flat v", "flat c", "Dirac v", "Dirac c"]}

    def fine(self, nf: int, chunk: int = 64):
        """Converged mean field on a fine grid: HF energies, top-layer weights and mirror parity."""
        m = self.m
        kf = m.grid(nf, shift=0.5 / nf)
        Es, tops, pars = [], [], []
        mt, mb = m.layer_mask(1), m.layer_mask(2)
        for s in range(0, len(kf), chunk):
            k = kf[s:s + chunk]
            e, Cf = self._active(k)
            Lam = self.form_factors(Cf)
            q = self.k[None, :, None, :] + self.Gvec[None, None] - k[:, None, None, :]
            V = coulomb(np.linalg.norm(q, axis=-1), self.eps_r, self.d)
            dL = np.empty((len(k), len(self.G), 4, 4), complex)          # <k| e^{iGr} |k> within the fine set
            A = np.conj(Cf).transpose(0, 2, 1)
            for g in range(len(self.G)):
                dL[:, g] = A @ self.shifted(Cf, g)
            S, _ = self.self_energy(self.P, Lam=Lam, V=V, diagLam=dL)
            H = np.einsum("ka,ab->kab", e, np.eye(4)) + S
            E, U = np.linalg.eigh(H)
            psi = np.einsum("kbm,kmn->kbn", Cf, U)
            Es.append(E)
            tops.append(np.sum(abs(psi[:, mt]) ** 2, axis=1))
            pars.append(2 * np.real(np.sum(np.conj(psi[:, mt]) * psi[:, mb], axis=1)))
        return kf, np.concatenate(Es), np.concatenate(tops), np.concatenate(pars)


def spectra(E, top, par, mu, etas_meV=(0.5, 4.0, 5.0)):
    """Top-layer LDOS relative to mu, flat-band VHS positions/widths, and two-Lorentzian fits per broadening."""
    eg = np.arange(-120, 120.05, 0.1)
    Em = (E - mu) * 1000
    order = np.argsort(-par, axis=1)[:, :2]
    rows = np.arange(len(E))[:, None]
    pair = np.sort(np.take_along_axis(Em, order, axis=1), axis=1)
    pair_idx = np.take_along_axis(order, np.argsort(np.take_along_axis(Em, order, axis=1), axis=1), axis=1)
    out = {"flat_bandwidth_meV": float(pair[:, 1].max() - pair[:, 0].min()),
           "flat_width_v_meV": float(np.ptp(pair[:, 0])), "flat_width_c_meV": float(np.ptp(pair[:, 1]))}
    curves = {}
    for eta in etas_meV:
        ld = dos(Em / 1000, top, eg / 1000, eta / 1000, kind="lorentz")
        dv = dos(pair[:, [0]] / 1000, top[rows, pair_idx[:, [0]]], eg / 1000, eta / 1000, kind="lorentz")
        dc = dos(pair[:, [1]] / 1000, top[rows, pair_idx[:, [1]]], eg / 1000, eta / 1000, kind="lorentz")
        ev, wv = half_max_width(eg, dv, eg[np.argmax(dv)])
        ec, wc = half_max_width(eg, dc, eg[np.argmax(dc)])
        r = {"e_v": ev, "e_c": ec, "sep": ec - ev, "fwhm_v": wv, "fwhm_c": wc, "fwhm_mean": 0.5 * (wv + wc)}
        try:
            r["fit"] = fit_two_peaks(eg, ld, window=(min(ev, ec) - 35, max(ev, ec) + 35),
                                     guess=[0.05, 0, 0.8, ev, max(wv, 2), 0.8, ec, max(wc, 2)])
        except Exception as ex:  # noqa: BLE001
            r["fit"] = {"error": str(ex)}
        out[f"eta_{eta:g}"] = r
        curves[f"ldos_eta_{eta:g}"] = ld
    return out, eg, curves


def main(cfg: dict) -> dict:
    t0 = time.time()
    nu = float(cfg.get("nu", 0.0))
    m = hf_model(**{k: cfg[k] for k in ("theta", "alpha", "kappa", "hv", "cut") if k in cfg})
    hf = ProjectedHF(m, int(cfg.get("nk", 12)), gshells=float(cfg.get("gshells", 2.0)),
                     eps_r=float(cfg.get("eps_r", 10.0)), d=float(cfg.get("d_gate", 40.0)),
                     hartree=bool(cfg.get("hartree", True)), fock=bool(cfg.get("fock", True)),
                     subtract=str(cfg.get("subtract", "none")), active=str(cfg.get("active", "mirror")),
                     hartree_shells=cfg.get("hartree_shells"), c2zt=bool(cfg.get("c2zt", True)))
    print(f"basis {m.nb}, k {hf.N}, G {len(hf.G)}, w1 {1000 * m.w1:.1f} meV, w0 {1000 * m.w0:.1f} meV", flush=True)
    conv = hf.run(nu=nu, T=float(cfg.get("T", 5e-4)), mix=float(cfg.get("mix", 0.4)),
                  maxit=int(cfg.get("maxit", 300)))
    kf, E, top, par = hf.fine(int(cfg.get("nk_fine", 36)))
    res, eg, curves = spectra(E, top, par, hf.mu)
    # single-particle: the same fine grid without the self-energy
    E0, psi0, _ = m.active(kf)
    top0 = np.sum(abs(psi0[:, m.layer_mask(1)]) ** 2, axis=1)
    par0 = 2 * np.real(np.sum(np.conj(psi0[:, m.layer_mask(1)]) * psi0[:, m.layer_mask(2)], axis=1))
    mu0 = ProjectedHF.occupy(E0, nu, 5e-4)[0]
    res0, _, curves0 = spectra(E0, top0, par0, mu0)
    np.savez_compressed("hf.npz", egrid_meV=eg, **curves, **{"sp_" + k: v for k, v in curves0.items()},
                        E_meV=(E - hf.mu) * 1000, top=top, parity=par, kf=kf)
    out = {"label": cfg.get("label", ""), "hartree": hf.hartree, "fock": hf.fock, "subtract": hf.subtract,
           "c2zt": hf.c2zt, "c2zt_unitarity_err": float(np.max(np.abs(hf.S @ np.conj(np.transpose(hf.S, (0, 2, 1)))
                                                                       - np.eye(4)[None]))),
           "nu": nu, "theta": m.theta_deg, "w1_meV": 1000 * m.w1, "w0_meV": 1000 * m.w0, "hv_eVnm": m.hv,
           "eps_r": hf.eps_r, "d_gate_nm": hf.d, "nk": hf.nk, "nk_fine": int(cfg.get("nk_fine", 36)),
           "nG": len(hf.G), "basis": m.nb, "convergence": {k: v for k, v in conv.items() if k != "history"},
           "hf": res, "sp": res0, "seconds": time.time() - t0, "nG_density": {str(g): [float(abs(x))]
                                                                              for g, x in zip(hf.G, hf.nG)}}
    return out


if __name__ == "__main__":
    cfg = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    out = main(cfg)
    json.dump(out, open("hf.json", "w"), indent=1)
    h, s = out["hf"], out["sp"]
    print(json.dumps({"dir": os.getcwd(), "nu": out["nu"], "converged": out["convergence"]["converged"],
                      "hf_sep_meV": h["eta_0.5"]["sep"], "hf_fwhm_meV": h["eta_0.5"]["fwhm_mean"],
                      "hf_fit_sep_eta4_meV": h["eta_4"]["fit"].get("sep"),
                      "hf_fit_fwhm_eta4_meV": h["eta_4"]["fit"].get("fwhm_mean"),
                      "sp_sep_meV": s["eta_0.5"]["sep"], "sp_fwhm_meV": s["eta_0.5"]["fwhm_mean"]}))
