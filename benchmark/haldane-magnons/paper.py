"""The paper (arXiv:cond-mat/9306036v1) and its numbers, each checked against the PDF's text layer.

  python3 paper.py      writes paper.pdf, paper.txt, claims.json; prints the outputs JSON
"""
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

URL = "https://arxiv.org/pdf/cond-mat/9306036v1"
# Table I: -E of the lowest state with S^z_total = M at L = 100 (open chain), M = 1..4 (parities -, +, -, +)
TABLE1 = {1: "138.940086", 2: "138.522461", 3: "138.08557", 4: "137.603"}
FITS = {"gap": "0.4107(1)", "v": "2.49(1)", "a21": "74.7(4)", "c31": "0.823(1)", "a31": "359(5)",
        "sum_n2_3": "4.80(6)", "a41": "1030(150)", "sum_n2_4": "14(2)"}


def value(s):
    """'0.4107(1)' -> (0.4107, 0.0001); '1030(150)' -> (1030, 150)."""
    v, e = s.rstrip(")").split("(")
    dec = len(v.split(".")[1]) if "." in v else 0
    return float(v), float(e) * 10 ** -dec


def main():
    data = urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "flower"}), timeout=60).read()
    Path("paper.pdf").write_bytes(data)
    subprocess.run(["pdftotext", "-layout", "paper.pdf", "paper.txt"], check=True)
    text = Path("paper.txt").read_text(errors="replace")
    missing = [s for s in list(TABLE1.values()) + list(FITS.values()) if s not in text]
    if missing:
        sys.exit(f"not in the paper's text: {missing}")
    claims = {"table1": {str(M): -float(e) for M, e in TABLE1.items()},
              "table1_digits": {str(M): len(e.split(".")[1]) for M, e in TABLE1.items()},
              "fits": {k: value(s) for k, s in FITS.items()}}
    json.dump(claims, open("claims.json", "w"), indent=1)
    print(json.dumps({"sha256": hashlib.sha256(data).hexdigest(), "n_checked": len(TABLE1) + len(FITS),
                      "gap": claims["fits"]["gap"][0], "v": claims["fits"]["v"][0]}))


if __name__ == "__main__":
    main()
