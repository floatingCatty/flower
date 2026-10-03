r"""ABACUS helpers for the Si DOS / Fermi-level benchmark.

Used two ways:
  * as a module by forgeflow ``function`` nodes (``call: abacus_si:<fn>``);
  * as a CLI by ``shell`` nodes (``python abacus_si.py write-input|parse ...``).

Runs under the ABACUS conda env Python (numpy, scipy, matplotlib).

Fermi level from the DOS
------------------------
E_F is the root of   N(mu) = \int g(E) f(E; mu, T) dE - N_e = 0,
with f the Fermi-Dirac occupation and N_e the valence electron count.  For an insulator the
occupations of the band tails are tiny (~exp(-Eg/2kT)), so N(mu) is evaluated as
    N(mu) - N_e = [N_below(E_s) - N_e] + n_el(mu) - n_hole(mu)
around a split energy E_s, which avoids the catastrophic cancellation of computing
``sum(g*f) - N_e`` directly.  ABACUS prints the histogram weights with 6 significant digits, so
N_below carries a ~1e-5 rounding error that would swamp n_el/n_hole (~1e-7 at 300 K in Si); when
E_s sits in an empty gap of the histogram, N_below is snapped to N_e (the gap is exactly charge-neutral).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

RY_TO_EV = 13.605693122994
KB_EV = 8.617333262e-5

STRU_TEMPLATE = """ATOMIC_SPECIES
Si 28.085 {pp}

NUMERICAL_ORBITAL
{orb}

LATTICE_CONSTANT
{alat}

LATTICE_VECTORS
0.5 0.5 0.0
0.5 0.0 0.5
0.0 0.5 0.5

ATOMIC_POSITIONS
Direct

Si
0.0
2
0.00 0.00 0.00 0 0 0
0.25 0.25 0.25 0 0 0
"""


# ----------------------------------------------------------------------------- function nodes
def prepare(pp_dir: str, pseudo: str, orbital: str, alat_bohr: float, ctx: dict) -> dict:
    """Write the diamond-Si STRU and count valence electrons from the pseudopotential header."""
    pp_dir = os.path.expanduser(pp_dir)
    upf = Path(pp_dir, pseudo)
    orb = Path(pp_dir, orbital)
    for f in (upf, orb):
        if not f.is_file():
            raise FileNotFoundError(f"{f} not found (check the pp_dir / pseudo / orbital inputs)")
    m = re.search(r'z_valence\s*=\s*"\s*([0-9.Ee+-]+)\s*"', upf.read_text(errors="replace"))
    if not m:
        raise ValueError(f"no z_valence in {upf}")
    zval = float(m.group(1))
    natoms = 2
    stru = Path(ctx["workdir"], "STRU")
    stru.write_text(STRU_TEMPLATE.format(pp=pseudo, orb=orbital, alat=alat_bohr))
    a_ang = alat_bohr * 0.529177210903
    return {"stru": str(stru), "pp_dir": pp_dir, "zval": zval, "nelec": zval * natoms, "natoms": natoms,
            "alat_angstrom": round(a_ang, 5),
            "summary": f"diamond Si, a = {a_ang:.4f} Å, {natoms} atoms, N_e = {zval * natoms:g} (z_valence {zval:g})"}


def fermi_from_dos(out_dir: str, nelec: float, temperature_K: float, kmesh: int, ef_abacus_eV: float,
                   ctx: dict) -> dict:
    """Fermi level by integrating the ABACUS DOS against the Fermi-Dirac occupation."""
    import numpy as np

    e_raw, n_raw = _read_raw_dos(Path(out_dir, "DOS1"))
    d = np.loadtxt(Path(out_dir, "DOS1_smearing.dat"))
    e_sm, g_sm = d[:, 0], d[:, 1]
    de = float(np.median(np.diff(e_sm)))
    kT = KB_EV * temperature_K

    total_raw = float(n_raw.sum())
    total_sm = float(np.trapezoid(g_sm, e_sm) if hasattr(np, "trapezoid") else np.trapz(g_sm, e_sm))

    # T = 0 picture from the unbroadened histogram: the empty window where the count below equals N_e
    vbm, cbm, insulator = _find_gap(e_raw, n_raw, nelec, de)
    gap = cbm - vbm
    split = 0.5 * (vbm + cbm)
    ef_raw = _solve_fermi(e_raw, n_raw, nelec, kT, split, snap=insulator)
    ef_sm = _solve_fermi(e_sm, g_sm * de, nelec, kT, split, snap=insulator)
    n_check = _count(e_raw, n_raw, ef_raw, kT)

    plot = _plot_dos(ctx["workdir"], e_sm, g_sm, e_raw, n_raw, de, nelec, kmesh, temperature_K,
                     ef_raw, ef_sm, ef_abacus_eV, vbm, cbm)
    res = {
        "kmesh": kmesh, "temperature_K": temperature_K, "nelec": nelec,
        "ef_fd_eV": round(ef_raw, 5),                 # main result: FD x unbroadened DOS
        "ef_fd_smeared_dos_eV": round(ef_sm, 5),      # same, with ABACUS's Gaussian-broadened DOS
        "ef_abacus_eV": ef_abacus_eV,
        "vbm_eV": round(vbm, 4), "cbm_eV": round(cbm, 4), "gap_eV": round(gap, 4),
        "midgap_eV": round(0.5 * (vbm + cbm), 5),
        "ef_minus_midgap_meV": round(1000 * (ef_raw - 0.5 * (vbm + cbm)), 2),
        "insulator": insulator,
        "electrons_at_ef": round(n_check, 4),          # as printed (6-digit weights)
        "dos_states_total_raw": round(total_raw, 6), "dos_states_total_smeared": round(total_sm, 4),
        "plot": plot,
        "summary": (f"k={kmesh}^3: E_F(FD, {temperature_K:g} K) = {ef_raw:.4f} eV, gap = {gap:.3f} eV "
                    f"[{vbm:.3f}, {cbm:.3f}], ABACUS E_F = {ef_abacus_eV:.4f} eV"),
    }
    Path(ctx["workdir"], "fermi.json").write_text(json.dumps(res, indent=2))
    return res


def converge(results: list, tol_meV: float, ctx: dict) -> dict:
    """Collect E_F vs k-mesh, judge k-point convergence, and plot it."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = sorted([r for r in results if r], key=lambda r: r["kmesh"])
    if not rows:
        raise ValueError("no successful DOS/Fermi results to compare")
    ks = [r["kmesh"] for r in rows]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.6), dpi=130)
    a1.plot(ks, [r["ef_fd_eV"] for r in rows], "o-", label="FD integration (raw DOS)")
    a1.plot(ks, [r["ef_fd_smeared_dos_eV"] for r in rows], "s--", label="FD integration (smeared DOS)")
    a1.plot(ks, [r["ef_abacus_eV"] for r in rows], "^:", label="ABACUS E_F (nscf)")
    a1.fill_between(ks, [r["vbm_eV"] for r in rows], [r["cbm_eV"] for r in rows], color="0.85",
                    label="gap [VBM, CBM]")
    a1.set_xlabel("k-mesh n (n×n×n)")
    a1.set_ylabel("energy / eV")
    a1.legend(fontsize=7, frameon=False)
    a1.set_title("Fermi level vs k-mesh")
    a2.plot(ks, [r["gap_eV"] for r in rows], "o-", color="C3")
    a2.set_xlabel("k-mesh n (n×n×n)")
    a2.set_ylabel("DOS gap / eV")
    a2.set_title("Band gap from DOS vs k-mesh")
    fig.tight_layout()
    path = os.path.join(ctx["workdir"], "convergence.png")
    fig.savefig(path)

    last = rows[-1]
    delta = abs(rows[-1]["ef_fd_eV"] - rows[-2]["ef_fd_eV"]) * 1000 if len(rows) > 1 else float("nan")
    ok = len(rows) > 1 and delta <= tol_meV
    table = [{k: r[k] for k in ("kmesh", "ef_fd_eV", "ef_fd_smeared_dos_eV", "ef_abacus_eV", "vbm_eV",
                                "cbm_eV", "gap_eV", "ef_minus_midgap_meV")} for r in rows]
    return {"table": table, "ef_eV": last["ef_fd_eV"], "gap_eV": last["gap_eV"], "kmesh": last["kmesh"],
            "delta_last_meV": round(delta, 3), "converged": ok, "plot": path,
            "summary": (f"E_F = {last['ef_fd_eV']:.4f} eV at k={last['kmesh']}^3 "
                        f"(Δ vs previous mesh {delta:.1f} meV, {'converged' if ok else 'NOT converged'} "
                        f"at {tol_meV:g} meV); gap {last['gap_eV']:.3f} eV")}


# ----------------------------------------------------------------------------- numerics
def _read_raw_dos(path: Path):
    """ABACUS v3.9 ``DOS1``: line 1 = #points, line 2 = #k, then ``E_upper  states_in_bin``."""
    import numpy as np

    d = np.loadtxt(path, skiprows=2)
    de = float(np.median(np.diff(d[:, 0])))
    return d[:, 0] - de / 2, d[:, 1]               # bin centres, states per bin


def _find_gap(e, n, nelec, de, tol=1e-3):
    """(VBM, CBM, insulator) from a histogram DOS.

    Candidate gaps are maximal runs of exactly-empty bins between occupied ones; the one whose
    cumulative count below is closest to N_e (within ``tol``, which exceeds the rounding error of
    the printed weights but not a single k-point's weight) is the band gap.  No such run: a metal,
    and VBM = CBM = the energy where the count crosses N_e.
    """
    import numpy as np

    occ = np.where(n > 0)[0]
    cum = np.cumsum(n)
    best = None
    for a, b in zip(occ[:-1], occ[1:]):
        if b - a > 1:                                # bins a+1 .. b-1 are empty
            miss = abs(cum[a] - nelec)
            if miss < tol and (best is None or miss < best[0]):
                best = (miss, a, b)
    if best is not None:
        _, a, b = best
        return float(e[a] + de / 2), float(e[b] - de / 2), True
    i = int(np.searchsorted(cum, nelec))
    return float(e[i]), float(e[i]), False


def _occ(e, mu, kT):
    from scipy.special import expit

    return expit(-(e - mu) / kT)


def _count(e, n, mu, kT):
    return float((n * _occ(e, mu, kT)).sum())


def _solve_fermi(e, n, nelec, kT, split, snap=False):
    """Root of N(mu) = N_e, with weights ``n`` = states per energy point."""
    import numpy as np
    from scipy.optimize import brentq
    from scipy.special import expit

    lo, hi = e < split, e >= split
    base = float(n[lo].sum()) - nelec
    if snap and abs(base) < 1e-3:
        base = 0.0

    def resid(mu):
        n_el = float((n[hi] * expit(-(e[hi] - mu) / kT)).sum())
        n_ho = float((n[lo] * expit((e[lo] - mu) / kT)).sum())
        return base + n_el - n_ho

    a, b = float(e.min()), float(e.max())
    return brentq(resid, a, b, xtol=1e-9, rtol=1e-14, maxiter=500) if np.sign(resid(a)) != np.sign(resid(b)) \
        else float("nan")


def _plot_dos(wd, e_sm, g_sm, e_raw, n_raw, de, nelec, kmesh, T, ef_raw, ef_sm, ef_ab, vbm, cbm):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(7, 3.8), dpi=130)
    ax.plot(e_sm, g_sm, lw=1, color="C0", label="DOS (ABACUS, Gaussian-broadened)")
    ax.fill_between(e_raw, n_raw / de, step="mid", color="C0", alpha=0.18, label="DOS (histogram)")
    ax.axvspan(vbm, cbm, color="0.9", zorder=0)
    ax.axvline(ef_raw, color="C3", lw=1.3, label=f"E_F FD {T:g} K = {ef_raw:.3f} eV")
    ax.axvline(ef_ab, color="k", lw=1, ls=":", label=f"E_F ABACUS = {ef_ab:.3f} eV")
    ax2 = ax.twinx()
    ax2.plot(e_raw, np.cumsum(n_raw), color="C2", lw=1)
    ax2.axhline(nelec, color="C2", lw=0.6, ls="--")
    ax2.set_ylabel("N(E) integrated states", color="C2")
    ax.set_xlim(e_sm.min(), min(e_sm.max(), cbm + 8))
    ax.set_ylim(bottom=0)
    ax.set_xlabel("E / eV")
    ax.set_ylabel("DOS / (states/eV/cell)")
    ax.set_title(f"Si DOS, k = {kmesh}×{kmesh}×{kmesh}")
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    fig.tight_layout()
    path = os.path.join(wd, f"dos_k{kmesh}.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ----------------------------------------------------------------------------- shell-node CLI
def write_input(args):
    """write-input MODE KMESH PP_DIR [READ_DIR] -> INPUT + KPT in cwd."""
    mode, kmesh, pp_dir = args[0], int(args[1]), args[2]
    lines = ["INPUT_PARAMETERS", "suffix si", f"calculation {mode}", f"pseudo_dir {pp_dir}",
             f"orbital_dir {pp_dir}", "basis_type lcao", "ks_solver genelpa", "ecutwfc 100", "scf_thr 1e-8",
             "smearing_method gaussian", "smearing_sigma 0.002", "mixing_beta 0.4"]
    if mode == "scf":
        lines += ["out_chg 1"]
    else:
        lines += ["init_chg file", f"read_file_dir {args[3]}", "nbands 16", "out_dos 1",
                  "dos_edelta_ev 0.01", "dos_sigma 0.05"]
    Path("INPUT").write_text("\n".join(lines) + "\n")
    Path("KPT").write_text(f"K_POINTS\n0\nGamma\n{kmesh} {kmesh} {kmesh} 0 0 0\n")


def parse(args):
    """parse OUT_DIR MODE -> JSON on stdout (energies in eV)."""
    out_dir, mode = Path(args[0]).resolve(), args[1]
    log = (out_dir / f"running_{mode}.log").read_text(errors="replace")
    res = {"out_dir": str(out_dir)}
    m = re.findall(r"EFERMI\s*=\s*([-0-9.Ee+]+)\s*eV", log)
    if not m:
        raise SystemExit(f"no EFERMI in {out_dir}/running_{mode}.log — did ABACUS finish?")
    res["ef_abacus_eV"] = float(m[-1])
    m = re.search(r"AUTOSET number of electrons:\s*=\s*([0-9.]+)", log)
    res["nelec_abacus"] = float(m.group(1)) if m else None
    m = re.search(r"nkstot_ibz\s*=\s*(\d+)", log)
    res["nk_ibz"] = int(m.group(1)) if m else None
    if mode == "scf":
        m = re.findall(r"!FINAL_ETOT_IS\s+([-0-9.Ee+]+)\s*eV", log)
        res["etot_eV"] = float(m[-1]) if m else None
        res["converged"] = "charge density convergence is achieved" in log
        res["summary"] = f"SCF E_tot = {res['etot_eV']} eV, E_F = {res['ef_abacus_eV']:.4f} eV"
    else:
        res["summary"] = f"nscf DOS on {res['nk_ibz']} k-points, ABACUS E_F = {res['ef_abacus_eV']:.4f} eV"
    print(json.dumps(res))


if __name__ == "__main__":
    cmd, rest = sys.argv[1], sys.argv[2:]
    {"write-input": write_input, "parse": parse}[cmd](rest)
