"""Check ttg.py against the authors' own single-particle script (Dataverse SP_continuum.txt).

The authors' script builds the Hamiltonian at import time, then loops over a k grid. Only its set-up and
`gen_ham` are executed here (the text before `nk=64`), with the twist, strain and displacement field
substituted, and the valley-K (xi = +1) eigenvalues nearest charge neutrality compared at a few k points.
Their plane-wave cutoff is centred differently, so agreement is expected to ~0.01 meV, not to round-off.
"""
from __future__ import annotations

import json
import re
import sys

import numpy as np

from ttg import TTG, PARAMS

CASES = [  # (label, theta, eps, phi, D_authors)
    ("1.55 deg", 1.55, 0.0, 0.0, 0.0),
    ("1.45 deg", 1.45, 0.0, 0.0, 0.0),
    ("1.6 deg, 0.55 % strain", 1.6, 0.0055, 0.3, 0.0),
    ("1.55 deg, D", 1.55, 0.0, 0.0, 0.01),
]


def authors_ham(src: str, theta, eps, phi, D):
    head = src.split("nk=64")[0]
    head = re.sub(r"^import pandas.*$|^from scipy import io.*$|^import scipy$|^import matplotlib.*$", "", head,
                  flags=re.M)
    subs = {r"^D = .*$": f"D = {D}", r"^twist_angle = .*$": f"twist_angle = {theta}*np.pi/180",
            r"^strain_angle = .*$": f"strain_angle = {phi}", r"^epsilon = .*$": f"epsilon = {eps}"}
    for pat, rep in subs.items():
        head, n = re.subn(pat, rep, head, flags=re.M)
        assert n == 1, pat
    ns: dict = {}
    exec(compile(head, "SP_continuum.txt", "exec"), ns)
    return ns


def main(src_path: str) -> dict:
    src = open(src_path, encoding="utf8", errors="replace").read()
    rows, worst = [], 0.0
    for label, th, eps, phi, Da in CASES:
        ns = authors_ham(src, th, eps, phi, Da)
        m = TTG(th, **PARAMS["SP2"], eps=eps, phi=phi, D=2 * Da)   # their D is doubled by "+ ham.H"
        q = ns["q"]
        ks = [np.zeros(2), 0.5 * q[0], 0.3 * q[1] + 0.2 * q[2], np.array(ns["b"][0]) * 0.37]
        for k in ks:
            ea = np.linalg.eigvalsh(np.asarray(ns["gen_ham"](k[0], k[1], ns["twist_angle"], 1)))
            em = np.linalg.eigvalsh(m.H(k)[0])
            # all levels within +-200 meV (well inside both cutoffs); "the 8 nearest zero" can split a tie
            ea, em = ea[abs(ea) < 0.2] * 1000, em[abs(em) < 0.2] * 1000
            d = float(np.max(abs(ea - em))) if len(ea) == len(em) else float("inf")
            worst = max(worst, d)
            rows.append({"case": label, "k": [float(x) for x in k], "authors_meV": ea.round(4).tolist(),
                         "ours_meV": em.round(4).tolist(), "max_diff_meV": d})
    return {"max_diff_meV": worst, "agree": worst < 0.05, "rows": rows,
            "basis_authors": int(2 * ns["Nq"]), "basis_ours": int(m.nb)}


if __name__ == "__main__":
    res = main(sys.argv[1])
    json.dump(res, open("check_sp.json", "w"), indent=1)
    print(json.dumps({"max_diff_meV": res["max_diff_meV"], "agree": res["agree"]}))
