"""The paper and its pseudopotentials (Singh-Miller & Marzari, PRB 80, 235407 (2009), arXiv:0801.1077v2).

  python3 sources.py        writes paper.pdf, paper.txt, tables.json, pseudo/*.UPF; prints the outputs JSON

The tables (PBE, this paper's own values, 13-layer slabs) are transcribed below and each number is checked against
the PDF's text layer (pdftotext -layout), next to its row label: Table I (a0, B), II (layer relaxations d12, d23,
d34 in %), III (surface energies: unrelaxed and relaxed in eV/atom, relaxed in J/m^2), IV (work functions, eV).
Ti(0001) is left out (fcc metals only).

The pseudopotentials are the paper's (its Ref. 83), from the Quantum ESPRESSO library. Two of them now carry other
names: Pt.pbe-rrkjus.UPF is Pt.pbe-nd-rrkjus.UPF, Au.pbe-d-rrkjus.UPF is Au.pbe-nd-rrkjus.UPF; the valence
configurations the paper states (Pd 4d9 5s1, Pt 5d9 6s1, Au 5d10 6s1, Al 3s2 3p1) are checked in each file.
"""
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

PAPER = "https://arxiv.org/pdf/0801.1077v2"
UPF = "https://pseudopotentials.quantum-espresso.org/upf_files/"
PSEUDO = {"Al": ("Al.pbe-rrkj.UPF", 3, ["3S", "3P"]), "Pd": ("Pd.pbe-nd-rrkjus.UPF", 10, ["4D", "5S"]),
          "Pt": ("Pt.pbe-nd-rrkjus.UPF", 10, ["5D", "6S"]), "Au": ("Au.pbe-nd-rrkjus.UPF", 11, ["5D", "6S"])}

TABLE1 = {"Al": (4.06, 74), "Pd": (3.98, 163), "Pt": (3.99, 246), "Au": (4.16, 140)}          # a0 (A), B (GPa)
# face: (d12, d23, d34 %), (sigma unrelaxed, relaxed eV/atom, relaxed J/m^2), work function eV
FACES = {
    "Al": {"111": ((+1.04, -0.54, +0.19), (0.30, 0.30, 0.67), 4.02),
           "100": ((+1.73, +0.47, -0.27), (0.45, 0.44, 0.86), 4.30),
           "110": ((-5.59, +2.20, -1.29), (0.70, 0.68, 0.93), 4.09)},
    "Pd": {"111": ((+0.25, -0.34, +0.10), (0.56, 0.56, 1.31), 5.25),
           "100": ((-1.30, -0.00, +0.35), (0.74, 0.74, 1.49), 5.11),
           "110": ((-8.49, +3.47, -0.19), (1.11, 1.08, 1.55), 4.87)},
    "Pt": {"111": ((+0.85, -0.56, -0.15), (0.65, 0.65, 1.49), 5.69),
           "100": ((-2.37, -0.55, +0.29), (0.91, 0.90, 1.81), 5.66),
           "110": ((-15.03, +7.61, -1.70), (1.38, 1.30, 1.85), 5.26)},
    "Au": {"111": ((-0.04, -1.86, -1.40), (0.35, 0.35, 0.74), 5.15),
           "100": ((-1.51, +0.33, +0.24), (0.46, 0.46, 0.85), 5.10),
           "110": ((-12.94, +7.83, -2.66), (0.71, 0.69, 0.90), 5.04)},
}


def fetch(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "flower-benchmark"})
    data = urllib.request.urlopen(req, timeout=120).read()
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def check_tables(text):
    """every transcribed number must appear in the text layer, on a line that carries its row label."""
    missing = []
    lines = text.splitlines()

    def near(label, value):
        for i, ln in enumerate(lines):
            if re.search(label, ln) and value in " ".join(lines[i:i + 2]):
                return True
        return False
    for el, (a0, B) in TABLE1.items():
        if not near(r"^\s*%s\s+%.2f" % (el, a0), " %d " % B):
            missing.append(("I", el))
    for el, faces in FACES.items():
        for face, (d, s, w) in faces.items():
            lab = r"(%s )?\(%s\)" % (el, face)
            for v in d:
                if not near(lab, "%+.2f" % v):
                    missing.append(("II", el, face, v))
            for v in s:
                if not near(lab, "%.2f" % v):
                    missing.append(("III", el, face, v))
            if not near(lab, "%.2fa" % w):
                missing.append(("IV", el, face, w))
    return missing


def main():
    out = {"paper_sha256": fetch(PAPER, "paper.pdf")}
    subprocess.run(["pdftotext", "-layout", "paper.pdf", "paper.txt"], check=True)
    missing = check_tables(Path("paper.txt").read_text(errors="replace"))
    if missing:
        sys.exit("transcribed values not found in the text layer: %s" % missing)
    Path("pseudo").mkdir(exist_ok=True)
    for el, (name, zval, shells) in PSEUDO.items():
        out["sha256_" + name] = fetch(UPF + name, "pseudo/" + name)
        head = Path("pseudo/" + name).read_text(errors="replace")[:20000]
        z = float(re.search(r'z_valence="\s*([\d.eE+-]+)"', head).group(1))
        conf = re.findall(r"^\s*(\d[SPDF])\s+\d+\s+\d\s+([\d.]+)", head, re.M)
        occ = {nl: float(o) for nl, o in conf if float(o) > 0}
        if abs(z - zval) > 1e-6 or sorted(occ) != sorted(shells):
            sys.exit(f"{name}: z_valence {z}, occupied shells {occ}; the paper states {zval} electrons in {shells}")
    tables = {"table1": {el: {"a0": a, "B": B} for el, (a, B) in TABLE1.items()},
              "faces": {f"{el}{face}": {"el": el, "face": face, "d": list(d), "sigma_u": s[0], "sigma_r": s[1],
                                        "sigma_r_Jm2": s[2], "phi": w}
                        for el, faces in FACES.items() for face, (d, s, w) in faces.items()},
              "pseudo": {el: p[0] for el, p in PSEUDO.items()},
              "settings": {"ecutwfc": 32, "ecutrho": 512, "smearing": "mv", "degauss": 0.02, "vacuum": 16.0,
                           "layers": 13}}
    json.dump(tables, open("tables.json", "w"), indent=1)
    out.update(n_faces=len(tables["faces"]), n_checked=sum(4 + 3 for f in FACES.values() for _ in f) + 2 * len(TABLE1))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
