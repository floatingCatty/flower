"""Tables II and III of Slevin & Ohtsuki (1999) from paper.txt (pdftotext -layout): the critical disorder W_c,
Lambda_c and nu with their 95 % confidence intervals for the box (B), Gaussian (G) and Lorentz (L) distributions,
with corrections to scaling (II) and without them, for L >= Lmin (III). Writes tables.json; prints the outputs.

The paper writes an interval as 16.54(53,56): the lower and upper bounds share the leading digits of the value.
"""
import hashlib
import json
import re
import sys


def ci(v, lo, hi):
    """16.54(53,56) -> (16.54, 16.53, 16.56); 1.57(55,59) -> (1.57, 1.55, 1.59); -2.8(3.3,2.3) -> negative."""
    x = float(v)
    dec = len(v.split(".")[1]) if "." in v else 0
    def bound(b):
        if "." in b:                              # a full number, as in -2.8(3.3,2.3): the magnitude is given
            return -float(b) if x < 0 else float(b)
        head = v[:len(v) - len(b)]
        return float(head + b)
    return {"value": x, "lo": bound(lo), "hi": bound(hi)}


text = open("paper.txt", encoding="utf8", errors="replace").read()
NUM = r"(-?\d+\.\d+)\((-?[\d.]+),\s*([\d.]+)\)"
t2, t3 = {}, {}
for m in re.finditer(r"^\s*([BGL])\s+" + r"\s+".join([NUM] * 4) + r"\s*$", text, re.M):
    d = m.group(1)
    g = m.groups()[1:]
    t2[d] = {"Wc": ci(*g[0:3]), "Lc": ci(*g[3:6]), "nu": ci(*g[6:9]), "y": ci(*g[9:12])}
for m in re.finditer(r"^\s*([BGL])\s+(\d+)\s+\[([\d.]+),([\d.]+)\]\s+" + r"\s+".join([NUM] * 3) + r"\s*$", text, re.M):
    d = m.group(1)
    g = m.groups()[4:]
    t3[d] = {"Lmin": int(m.group(2)), "W_range": [float(m.group(3)), float(m.group(4))],
             "Wc": ci(*g[0:3]), "Lc": ci(*g[3:6]), "nu": ci(*g[6:9])}
sizes = [int(x) for x in re.search(r"system sizes used were L = ([\d, ]+)", text).group(1).replace(" ", "").split(",")]
if sorted(t2) != ["B", "G", "L"] or sorted(t3) != ["B", "G", "L"]:
    sys.exit(f"tables not found intact: II {sorted(t2)}, III {sorted(t3)}")
json.dump({"table2": t2, "table3": t3, "sizes": sizes}, open("tables.json", "w"), indent=1)
print(json.dumps({"sha256_pdf": hashlib.sha256(open("paper.pdf", "rb").read()).hexdigest(), "sizes": sizes,
                  "box_Wc": t2["B"]["Wc"], "box_nu": t2["B"]["nu"], "box_Lc": t2["B"]["Lc"]}))
