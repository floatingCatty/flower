"""Fetch the paper (arXiv:cond-mat/9602026) and extract Table I, <Sz(0) Sz(r)> at r = L/2, from its text layer.

  python3 paper.py        writes paper.pdf, paper.txt, table1.json; prints the outputs JSON
"""
import hashlib
import json
import re
import subprocess
import urllib.request

ARXIV = "cond-mat/9602026"
COLUMNS = [-0.25, 0.0, 0.1, 0.2, "crit"]          # Table I's J2 columns; "crit" = the paper's J2crit
ROW = re.compile(r"^\s*(\d{1,2})\s+" + r"\s+".join([r"(-?0\.\d{7})"] * 5) + r"\s*$")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "flower-reproduction/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main():
    pdf = get("https://arxiv.org/pdf/" + ARXIV)
    open("paper.pdf", "wb").write(pdf)
    subprocess.run(["pdftotext", "-layout", "paper.pdf", "paper.txt"], check=True)
    rows = {}
    for line in open("paper.txt", encoding="utf8", errors="replace"):
        m = ROW.match(line)
        if m:
            rows[int(m.group(1))] = [float(x) for x in m.groups()[1:]]
    if sorted(rows) != list(range(4, 17)):
        raise SystemExit("Table I not found intact: rows %s" % sorted(rows))
    table = {"columns": COLUMNS, "r": sorted(rows), "values": [rows[r] for r in sorted(rows)]}
    json.dump(table, open("table1.json", "w"), indent=1)
    return {"sha256_pdf": hashlib.sha256(pdf).hexdigest(), "table1_rows": len(rows)}


if __name__ == "__main__":
    print(json.dumps(main()))
