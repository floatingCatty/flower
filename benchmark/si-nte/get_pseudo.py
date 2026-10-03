"""Fetch the Si pseudopotential as Si.upf and record where it came from.

  python3 get_pseudo.py vbc        the classic LDA (PZ) norm-conserving Si.pz-vbc.UPF (QE pseudopotential library)
  python3 get_pseudo.py dojo-lda   PseudoDojo v0.4 LDA (PW) scalar-relativistic standard, ONCVPSP (Hamann 2013)
"""
import hashlib
import json
import sys
import tarfile
import urllib.request

SOURCES = {
    "vbc": ("https://pseudopotentials.quantum-espresso.org/upf_files/Si.pz-vbc.UPF", None),
    "dojo-lda": ("https://www.pseudo-dojo.org/pseudos/nc-sr-04_pw_standard_upf.tgz", "Si.upf"),
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "flower-reproduction/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main(name):
    url, member = SOURCES[name]
    data = get(url)
    if member:
        open("archive.tgz", "wb").write(data)
        with tarfile.open("archive.tgz") as t:
            m = next(x for x in t.getmembers() if x.name.endswith("/" + member) or x.name == member)
            data = t.extractfile(m).read()
    if b"<UPF" not in data and b"<PP_INFO>" not in data:
        raise SystemExit("not a UPF file")
    open("Si.upf", "wb").write(data)
    return {"pseudo": name, "url": url, "sha256": hashlib.sha256(data).hexdigest()}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1])))
