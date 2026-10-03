"""Run the authors' released Hartree-Fock pipeline (Dataverse: save_vectors_TTG_repo.py ->
selfconsistent_TTG_eps10_repo.py -> DOS_compare.py, input 96 = w_AA/w_AB = 8/11, D = 0) unchanged except for
their cluster paths (/n/...), then measure the VHS separation and widths of its LDOS like everything else.

python3 authors_hf.py [INDEX]   (the three scripts in the working directory)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from ttg import fit_two_peaks, half_max_width

SCRIPTS = ["save_vectors_TTG_repo.py", "selfconsistent_TTG_eps10_repo.py", "DOS_compare.py"]


def patch(name: str) -> str:
    src = Path(name).read_text(encoding="utf8", errors="replace")
    # every hard-coded directory of the authors' cluster -> the working directory
    out, n = re.subn(r"'/n/[^']*/", "'./", src)
    Path("patched_" + name).write_text(out)
    return f"{name}: {n} paths"


def main(index: int = 96) -> dict:
    notes = [patch(s) for s in SCRIPTS]
    times = {}
    for s in SCRIPTS:
        t0 = time.time()
        with open(f"{s}.log", "w") as log:
            r = subprocess.run([sys.executable, "patched_" + s, str(index)], stdout=log, stderr=subprocess.STDOUT)
        times[s] = round(time.time() - t0, 1)
        if r.returncode != 0:
            tail = Path(f"{s}.log").read_text()[-3000:]
            raise SystemExit(f"{s} failed (exit {r.returncode}):\n{tail}")
    dos = np.real(np.load("DOSFinalcompare.npy"))
    E = np.linspace(-80, 80, len(dos))       # DOS_compare.py: energyStep = linspace(-rangeE, rangeE, 1600)
    y = dos / dos.max()
    i = int(np.argmax(y))
    fit = fit_two_peaks(E, y, window=(-60, 60), guess=[0.05, 0, 0.8, E[i] - 8, 12, 0.8, E[i] + 8, 12])
    pk, w = half_max_width(E, y, E[i])
    np.savez_compressed("authors_hf.npz", E=E, ldos=dos)
    return {"index": index, "notes": notes, "seconds": times, "fit": fit, "sep_meV": fit["sep"],
            "fwhm_meV": fit["fwhm_mean"], "main_peak_meV": pk, "main_peak_fwhm_meV": w,
            "broadening": "Lorentzian eta = 5 meV (in DOS_compare.py)"}


if __name__ == "__main__":
    res = main(int(sys.argv[1]) if len(sys.argv) > 1 else 96)
    json.dump(res, open("authors_hf.json", "w"), indent=1)
    print(json.dumps({"sep_meV": res["sep_meV"], "fwhm_meV": res["fwhm_meV"], "seconds": res["seconds"]}))
