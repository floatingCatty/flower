"""The experimental numbers the theory is compared with, re-derived from the authors' raw data.

Fig. 2B/2E: AAA-site STS at charge neutrality in the uniform 1.55 deg region, fitted with two Lorentzians
(+ linear background) like the paper: VHS separation (paper: ~18 meV) and mean FWHM (paper: ~23 meV).
2B_specs.txt holds 7 spectra (nu = -3, -2, -1, CNP, 1, 2, 3; CNP is row 3) of 401 samples without an energy
axis; the axis is inferred as -200..200 meV in 1 meV steps (peak spacings and Fig. 2B agree with this).
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

from ttg import fit_two_peaks


def main(dv: str) -> dict:
    spec = np.loadtxt(f"{dv}/2B_specs.txt", delimiter=",")
    E = np.arange(spec.shape[1]) - (spec.shape[1] - 1) / 2       # meV (inferred axis)
    cnp = spec[3]
    # the paper: "fitting our spectra with the sum of two Lorentzian curves" (no background mentioned).
    # Primary: two Lorentzians + constant offset over +-40 meV; the others show how much the choice matters.
    variants = {}
    for bg in ("none", "constant", "linear"):
        for w in (30, 40, 50):
            try:
                f = fit_two_peaks(E, cnp, window=(-w, w), background=bg)
                variants[f"{bg} +-{w}"] = {"sep": f["sep"], "fwhm": f["fwhm_mean"], "rms": f["rms"]}
            except Exception as ex:  # noqa: BLE001
                variants[f"{bg} +-{w}"] = {"error": str(ex)}
    fit = fit_two_peaks(E, cnp, window=(-40, 40), background="constant")
    rows = {}
    for i, nu in enumerate([-3, -2, -1, 0, 1, 2, 3]):
        try:
            f = fit_two_peaks(E, spec[i], window=(-50, 60))
            rows[str(nu)] = {"sep": f["sep"], "fwhm_v": f["fwhm_v"], "fwhm_c": f["fwhm_c"]}
        except Exception as ex:  # noqa: BLE001
            rows[str(nu)] = {"error": str(ex)}
    theta_I = np.loadtxt(f"{dv}/1G_data.txt", delimiter=",")
    np.savez_compressed("expt.npz", E=E, cnp=cnp, spectra=spec)
    ok = [v for v in variants.values() if "sep" in v]
    return {"cnp_sep_meV": fit["sep"], "cnp_fwhm_meV": fit["fwhm_mean"], "cnp_fit": fit, "fit_choice":
            "two Lorentzians + constant, +-40 meV", "variants": variants,
            "sep_range_meV": [min(v["sep"] for v in ok), max(v["sep"] for v in ok)],
            "fwhm_range_meV": [min(v["fwhm"] for v in ok), max(v["fwhm"] for v in ok)], "by_filling": rows,
            "fig1g_theta_I_mean": float(np.mean(theta_I[1])) if theta_I.ndim == 2 else None,
            "fig1g_Lambda_range_nm": [float(np.min(theta_I[0])), float(np.max(theta_I[0]))] if theta_I.ndim == 2
            else None}


if __name__ == "__main__":
    res = main(sys.argv[1])
    json.dump(res, open("expt.json", "w"), indent=1)
    print(json.dumps({"dir": os.getcwd(), **{k: res[k] for k in ("cnp_sep_meV", "cnp_fwhm_meV", "fig1g_theta_I_mean", "sep_range_meV",
                                          "fwhm_range_meV")}}))
