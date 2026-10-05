"""Tables 1-3 of the paper (C11, C12, C44 at 300 K, GPa, and the % errors vs experiment, per potential file) and
each table's experimental reference, from paper.txt (pdftotext -layout). Writes tables.json; prints the outputs.

A row is "complete" with six numbers (C11, err, C12, err, C44, err). The paper's text has rows that are not:
a value missing (Ni_smf7: no C11), a row without a potential name, a row with only C11 (Ni1_Mendelev). Those
are kept with their raw numbers and complete=false; only complete rows become claims.
"""
import hashlib
import json
import re
import sys

text = open("paper.txt", encoding="utf8", errors="replace").read()
METALS = {1: "Cu", 2: "Al", 3: "Ni"}
NUM = re.compile(r"^\d+(\.\d+)?%?$")
SUFFIX = re.compile(r"^(.*?\.(?:eam\.alloy|eam\.fs|eam|setfl|set))(\d+)$")

tables = {}
for k, metal in METALS.items():
    start = text.index(f"Table {k} Accuracy")
    end = text.find("Table", start + 10)
    end = min(x for x in (end, text.find("Simulation results based on", start)) if x > 0)
    block = text[start:end]
    head = re.sub(r"\s+", " ", block[:400])
    ref = {c: float(v) for c, v in re.findall(r"(C11|C12|C44)\s*=\s*([\d.]+)", head)}
    rows = []
    for line in block.splitlines()[1:]:
        toks = line.split()
        if not toks or toks[0] in ("C11=", "Copper", "Aluminum", "Nickel") or "Gpa" in line or "GPa" in line:
            continue
        nums = []
        while toks and NUM.match(toks[-1]):
            nums.insert(0, toks.pop())
        if not nums:
            continue
        name, cite = None, None
        if toks:
            name = " ".join(toks)
            m = SUFFIX.match(name)
            if m:                                         # the citation number glued to the file name
                name, cite = m.group(1), int(m.group(2))
            elif nums[0].isdigit():                       # a pure integer right after the name: the citation
                cite = int(nums.pop(0))
        vals = [float(x.rstrip("%")) for x in nums]
        row = {"potential": name, "cite": cite, "raw": nums, "complete": len(vals) == 6 and name is not None}
        if row["complete"]:
            row.update(C11=vals[0], C11_err=vals[1], C12=vals[2], C12_err=vals[3], C44=vals[4], C44_err=vals[5])
        rows.append(row)
    tables[metal] = {"experiment": ref, "rows": rows}

if [len(t["rows"]) for t in tables.values()] != [17, 24, 19] or any(len(t["experiment"]) != 3 for t in tables.values()):
    sys.exit("tables not found intact: %s" % {m: (len(t["rows"]), t["experiment"]) for m, t in tables.items()})
json.dump(tables, open("tables.json", "w"), indent=1)
print(json.dumps({"sha256_pdf": hashlib.sha256(open("paper.pdf", "rb").read()).hexdigest(),
                  "rows": {m: len(t["rows"]) for m, t in tables.items()},
                  "complete": {m: sum(r["complete"] for r in t["rows"]) for m, t in tables.items()},
                  "experiment": {m: t["experiment"] for m, t in tables.items()}}))
