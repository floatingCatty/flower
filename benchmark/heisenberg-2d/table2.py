"""Table II of the paper (QMC at beta = 8L: -E, S(pi,pi), C(L/2,L/2) for L = 4..16) from paper.txt, with the
errors in parentheses as numbers; writes table2.json and prints the outputs JSON (sha256 of paper.pdf)."""
import hashlib
import json
import re

NUM = r"(\d+\.\d+)\((\d+)\)"


def value(v, e):
    digits = len(v.split(".")[1])
    return float(v), int(e) * 10 ** -digits


text = re.sub(r"\s+", " ", open("paper.txt", encoding="utf8", errors="replace").read())
rows = {}
for m in re.finditer(r"(?<![\d.])(4|6|8|10|12|14|16) " + " ".join([NUM] * 3), text):
    L = int(m.group(1))
    if L in rows:
        continue
    g = m.groups()[1:]
    (mE, dE), (S, dS), (C, dC) = value(g[0], g[1]), value(g[2], g[3]), value(g[4], g[5])
    rows[L] = {"E": -mE, "E_err": dE, "S": S, "S_err": dS, "C": C, "C_err": dC}
if sorted(rows) != [4, 6, 8, 10, 12, 14, 16]:
    raise SystemExit("Table II not found intact: %s" % sorted(rows))
json.dump({str(k): v for k, v in sorted(rows.items())}, open("table2.json", "w"), indent=1)
print(json.dumps({"sha256_pdf": hashlib.sha256(open("paper.pdf", "rb").read()).hexdigest(),
                  "table2_rows": len(rows)}))
