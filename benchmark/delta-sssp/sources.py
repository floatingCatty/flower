"""Everything the study starts from, pinned and hashed:
- the Delta package (Lejaeghere et al.): CIFs of the 71 elemental crystals, WIEN2k.txt (all-electron V0, B0, B1),
  history/ (published results of other codes, incl. "QE with SSSP Efficiency"), calcDelta.py, eosfit.py;
- SSSP 1.1 PBE efficiency (Prandini et al. 2018, the version of its Tables 4-5): the pseudopotentials and their recommended cutoffs;
- the SSSP paper (arXiv:1806.05609).

  python3 sources.py      writes delta/, sssp/, sssp.json, paper.pdf, paper.txt; prints the outputs JSON
"""
import hashlib
import io
import json
import subprocess
import tarfile
import urllib.request
import zipfile

DELTA_COMMIT = "f9b6693763dea92d43ee48f172c02bc506876f06"
DELTA_ZIP = "https://github.com/molmod/DeltaCodesDFT/archive/%s.zip" % DELTA_COMMIT
SSSP = "https://archive.materialscloud.org/records/x7v00-pnh34/files/SSSP_1.1_PBE_efficiency"
PAPER = "https://arxiv.org/pdf/1806.05609v2"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "flower-reproduction/1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    out = {}
    z = get(DELTA_ZIP)
    out["delta_zip_sha256"] = sha(z)
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        for name in zf.namelist():
            rel = name.split("/", 1)[1] if "/" in name else ""
            if not rel or name.endswith("/"):
                continue
            path = "delta/" + rel
            import os
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(zf.read(name))
    with tarfile.open("delta/history.tar.gz") as t:
        t.extractall("delta")
    tgz = get(SSSP + ".tar.gz?download=1")
    meta = get(SSSP + ".json?download=1")
    out["sssp_tar_sha256"], out["sssp_json_sha256"] = sha(tgz), sha(meta)
    with tarfile.open(fileobj=io.BytesIO(tgz)) as t:
        t.extractall("sssp")
    open("sssp.json", "wb").write(meta)
    pdf = get(PAPER)
    open("paper.pdf", "wb").write(pdf)
    subprocess.run(["pdftotext", "-layout", "paper.pdf", "paper.txt"], check=True)
    out["paper_sha256"] = sha(pdf)
    lib = json.loads(meta)
    import os
    out["n_cifs"] = len([f for f in os.listdir("delta/CIFs") if f.endswith(".cif")])
    out["n_pseudos"] = len(lib)
    out["n_wien2k"] = sum(1 for ln in open("delta/WIEN2k.txt") if ln.strip() and not ln.startswith("#"))
    return out


if __name__ == "__main__":
    print(json.dumps(main()))
