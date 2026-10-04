"""Tables II and V of the paper from its text layer: energies per atom (hartree) of the H10 chain and of the
infinite chain in the minimal STO-6G basis, by bond length R (bohr). Writes tables.json; prints outputs JSON.

Each table is cut out between its column header and its caption, so rows of other tables cannot leak in.
"""
import hashlib
import json
import re

RS = ["1.0", "1.2", "1.4", "1.6", "1.8", "2.0", "2.4", "2.8", "3.2", "3.6"]
TABLES = {
    "II": {"header": "R AFQMC DMET[2] FCI", "caption": "Table II.",
           "cols": ["AFQMC", "DMET", "FCI", "GF2", "SC-GW", "SEET1", "SEET2", "SEET3",
                    "RCCSD", "RCCSD(T)", "UCCSD", "UCCSD(T)", "RHF", "UHF"]},
    "V": {"header": "R AFQMC BDMC DMET", "caption": "Table V.",
          "cols": ["AFQMC", "BDMC", "DMET5", "DMETinf", "DMRG", "SC-GW", "SEET1", "SEET2",
                   "RCCSD", "RCCSD(T)", "UCCSD", "UCCSD(T)", "UHF"]},
}


def value(tok):
    if tok == "N/A":
        return None
    return float(re.sub(r"\(\d+\)$", "", tok))


def main():
    text = open("paper.txt", encoding="utf8", errors="replace").read()
    out = {}
    for name, t in TABLES.items():
        i = text.index(t["header"])
        j = text.index(t["caption"], i)
        rows = {}
        for ln in text[i:j].splitlines():
            tok = ln.split()
            if tok and tok[0] in RS and tok[0] not in rows and len(tok) == len(t["cols"]) + 1:
                rows[tok[0]] = {c: value(v) for c, v in zip(t["cols"], tok[1:])}
        if sorted(rows, key=float) != RS:
            raise SystemExit("Table %s not found intact: rows %s" % (name, sorted(rows)))
        out[name] = rows
    json.dump(out, open("tables.json", "w"), indent=1)
    return {"sha256_pdf": hashlib.sha256(open("paper.pdf", "rb").read()).hexdigest(),
            "rows_II": len(out["II"]), "rows_V": len(out["V"]),
            "fci_1.8": out["II"]["1.8"]["FCI"]}


if __name__ == "__main__":
    print(json.dumps(main()))
