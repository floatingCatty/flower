"""S22 noncovalent interaction energies (Jurecka, Sponer, Cerny & Hobza, PCCP 8, 1985 (2006)).

  python3 s22.py data S22.py                -> s22.json: geometries (dimer, monomer split), names, reference values
  python3 s22.py calc s22.json INDEX LEVEL  -> counterpoise-corrected interaction energy at LEVEL (kcal/mol)

LEVEL (frozen core, density fitting for SCF and MP2):
  mp2-adz   HF and MP2 correlation, aug-cc-pVDZ   } extrapolated: MP2/CBS = HF(aTZ) + corr(aDZ->aTZ, X^-3)
  mp2-atz   HF and MP2 correlation, aug-cc-pVTZ   }
  ccsdt-dz  CCSD(T) - MP2 in cc-pVDZ (no DF)        the higher-order correction, as the paper does in a small basis
The paper's reference: MP2/CBS (aTZ->aQZ for most complexes) + [CCSD(T) - MP2] in a smaller basis.
"""
import json
import re
import sys
import time

HARTREE_KCAL = 627.509474


def parse(path):
    txt = open(path).read()
    refs = {}
    for name in ("S220", "S22A", "S22B"):
        refs[name] = {int(i): float(v) for i, v in
                      re.findall(r"BIND_%s\['%%s-%%s' %% \(dbse,\s*(\d+)\)\]\s*=\s*(-?[\d.]+)" % name, txt)}
    tags = {int(i): t for i, t in re.findall(r"TAGL\['%s-%s' % \(dbse,\s*(\d+)\)\]\s*=\s*'''?\"?([^'\"]+)", txt)}
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
        out.append({"index": i, "name": tags.get(i, str(i)).strip(), "A": frags[0], "B": frags[1],
                    "ref_2006": refs["S220"].get(i), "ref_S22A": refs["S22A"].get(i), "ref_S22B": refs["S22B"].get(i)})
    return sorted(out, key=lambda r: r["index"])


def atoms(frag, ghost=False):
    return "; ".join(("X-" if ghost else "") + f"{el} {x} {y} {z}" for el, x, y, z in frag)


def energies(atom_str, level, max_mem):
    from pyscf import gto, scf, mp, cc
    basis = {"mp2-adz": "aug-cc-pvdz", "mp2-atz": "aug-cc-pvtz", "ccsdt-dz": "cc-pvdz"}[level]
    m = gto.M(atom=atom_str, basis=basis, verbose=0, max_memory=max_mem)
    nfc = sum(1 if z > 2 else 0 for z in m.atom_charges()) + sum(4 if z > 10 else 0 for z in m.atom_charges())
    if level == "ccsdt-dz":
        mf = scf.RHF(m).run(conv_tol=1e-10)
        pt = mp.MP2(mf, frozen=nfc).run()
        c = cc.CCSD(mf, frozen=nfc).run(conv_tol=1e-8)
        et = c.ccsd_t()
        return {"hf": mf.e_tot, "mp2_corr": pt.e_corr, "ccsd_corr": c.e_corr, "t": et,
                "delta": (c.e_corr + et) - pt.e_corr, "nao": m.nao}
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
        json.dump(recs, open("s22.json", "w"), indent=1)
        print(json.dumps({"n": len(recs), "with_ref": sum(r["ref_2006"] is not None for r in recs)}))
    else:
        mem = int(sys.argv[5]) if len(sys.argv) > 5 else 16000
        r = calc(sys.argv[2], int(sys.argv[3]), sys.argv[4], mem)
        json.dump(r, open("result.json", "w"), indent=1)
        print(json.dumps(r))
