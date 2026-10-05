"""A24 noncovalent interaction energies (Rezac & Hobza, JCTC 9, 2151 (2013), doi:10.1021/ct400057w) near the basis
set limit, counterpoise corrected, frozen core.

  python3 a24.py data A24.py                -> a24.json: 24 dimers (fragments A, B), names, the reference values
  python3 a24.py calc a24.json INDEX LEVEL  -> the interaction energy components at LEVEL (kcal/mol), result.json

LEVEL:
  mp2-atz    HF and MP2 correlation, aug-cc-pVTZ (density fitting)  } MP2/CBS = HF(aQZ) + corr(aTZ->aQZ, X^-3)
  mp2-aqz    HF and MP2 correlation, aug-cc-pVQZ (density fitting)  }
  ccsdt-atz  CCSD(T) - MP2 in aug-cc-pVTZ (no density fitting)        the higher-order correction
CCSD(T)/CBS = MP2/CBS + [CCSD(T) - MP2]/aTZ. The reference adds corrections beyond CCSD(T)/CBS (higher
excitations, core correlation, relativity; up to about 0.1 kcal/mol), which this run does not compute.
"""
import json
import re
import sys
import time

HARTREE_KCAL = 627.509474


def parse(path):
    txt = open(path).read()
    ref = {int(i): float(v) for i, v in re.findall(r"BIND\['%s-%s'\s*% \(dbse,\s*(\d+)\s*\)\]\s*=\s*(-?[\d.]+)", txt)}
    tags = {int(i): t.strip() for i, t in re.findall(r"TAGL\['%s-%s'\s*% \(dbse,\s*(\d+)\)\]\s*=\s*\"\"\"([^\"]+)\"\"\"", txt)}
    out = []
    for i, body in re.findall(r"GEOS\['%s-%s-dimer' % \(dbse, '(\d+)'\)\] = qcdb.Molecule\(\"\"\"(.*?)\"\"\"\)", txt, re.S):
        frags, cur = [], []
        for ln in body.strip().splitlines():
            ln = ln.strip()
            if ln == "--":
                frags.append(cur)
                cur = []
            elif re.match(r"^[A-Z][a-z]?\s", ln):
                el, x, y, z = ln.split()[:4]
                cur.append([el, float(x), float(y), float(z)])
        frags.append(cur)
        i = int(i)
        out.append({"index": i, "name": tags.get(i, str(i)), "A": frags[0], "B": frags[1], "ref": ref.get(i)})
    return sorted(out, key=lambda r: r["index"])


def atoms(frag, ghost=False):
    return "; ".join(("X-" if ghost else "") + f"{el} {x} {y} {z}" for el, x, y, z in frag)


def energies(atom_str, level, max_mem):
    from pyscf import gto, scf, mp, cc
    basis = {"mp2-atz": "aug-cc-pvtz", "mp2-aqz": "aug-cc-pvqz", "ccsdt-atz": "aug-cc-pvtz"}[level]
    m = gto.M(atom=atom_str, basis=basis, verbose=0, max_memory=max_mem)
    z = m.atom_charges()
    nfc = int(sum(1 for q in z if q > 2) + sum(4 for q in z if q > 10))
    if level == "ccsdt-atz":
        mf = scf.RHF(m).run(conv_tol=1e-10)
        pt = mp.MP2(mf, frozen=nfc).run()
        c = cc.CCSD(mf, frozen=nfc).run(conv_tol=1e-8)
        et = c.ccsd_t()
        return {"hf": mf.e_tot, "mp2_corr": pt.e_corr, "delta": (c.e_corr + et) - pt.e_corr, "nao": m.nao}
    from pyscf.mp import dfmp2
    mf = scf.RHF(m).density_fit(auxbasis=basis + "-jkfit").run(conv_tol=1e-10)
    e2 = dfmp2.DFMP2(mf, frozen=nfc).run().e_corr
    return {"hf": mf.e_tot, "mp2_corr": e2, "nao": m.nao}


def calc(data_path, index, level, max_mem=16000):
    rec = next(r for r in json.load(open(data_path)) if r["index"] == index)
    t0 = time.time()
    d = energies(atoms(rec["A"]) + "; " + atoms(rec["B"]), level, max_mem)
    a = energies(atoms(rec["A"]) + "; " + atoms(rec["B"], ghost=True), level, max_mem)
    b = energies(atoms(rec["A"], ghost=True) + "; " + atoms(rec["B"]), level, max_mem)
    out = {"index": index, "name": rec["name"], "level": level, "nao": d["nao"], "seconds": round(time.time() - t0, 1)}
    for k in ("hf", "mp2_corr", "delta"):
        if k in d:
            out[f"{k}_int_kcal"] = (d[k] - a[k] - b[k]) * HARTREE_KCAL
    return out


if __name__ == "__main__":
    if sys.argv[1] == "data":
        recs = parse(sys.argv[2])
        if len(recs) != 24 or any(r["ref"] is None for r in recs):
            sys.exit(f"A24 not found intact: {len(recs)} dimers")
        json.dump(recs, open("a24.json", "w"), indent=1)
        print(json.dumps({"n": len(recs), "names": [r["name"] for r in recs]}))
    else:
        mem = int(sys.argv[5]) if len(sys.argv) > 5 else 16000
        r = calc(sys.argv[2], int(sys.argv[3]), sys.argv[4], mem)
        json.dump(r, open("result.json", "w"), indent=1)
        print(json.dumps(r))
