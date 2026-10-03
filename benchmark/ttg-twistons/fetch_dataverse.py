"""Download the paper's replication dataset (Harvard Dataverse doi:10.7910/DVN/QWIFR1, CC0) at a pinned
version, verifying every file against the MD5 the server lists for that version.

python3 fetch_dataverse.py OUT_DIR [VERSION] [NAME ...]   (no names: the theory codes and small data files)
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

DOI = "doi:10.7910/DVN/QWIFR1"
API = "https://dataverse.harvard.edu/api"
DEFAULT = [
    # theory codes
    "SP_continuum.txt", "save_vectors_TTG_repo.py", "selfconsistent_TTG_eps10_repo.py", "DOS_compare.py",
    "Trilayers.jl", "RealSpace.jl", "GrapheneParameters.jl", "example.jl", "plot_relax.m", "1B_code.txt",
    "loc_twist_vs_lambda.m", "local_twist.txt",
    # experimental data the theory is compared with
    "2B_specs.txt", "2CD_specs.txt", "3E_specs.txt", "1G_data.txt",
]


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "flower-reproduction/1 (python urllib)"})
    with urllib.request.urlopen(req, timeout=120) as r:   # the default urllib agent gets HTTP 403
        return r.read()


def main(out: str, version: str = "1.1", names=()) -> dict:
    meta = json.loads(get(f"{API}/datasets/:persistentId/versions/{version}?persistentId={DOI}"))["data"]
    files = {f["dataFile"]["filename"]: f["dataFile"] for f in meta["files"]}
    want = list(names) or DEFAULT
    missing = [n for n in want if n not in files]
    if missing:
        raise SystemExit(f"not in dataset version {version}: {missing}")
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    got = {}
    for n in want:
        f = files[n]
        p = d / n
        md5 = f["checksum"]["value"] if f.get("checksum", {}).get("type") == "MD5" else f.get("md5")
        if not (p.exists() and hashlib.md5(p.read_bytes()).hexdigest() == md5):
            p.write_bytes(get(f"{API}/access/datafile/{f['id']}"))
        h = hashlib.md5(p.read_bytes()).hexdigest()
        if h != md5:
            raise SystemExit(f"{n}: md5 {h} != {md5}")
        got[n] = {"id": f["id"], "md5": md5, "bytes": p.stat().st_size}
    lic = (meta.get("license") or {}).get("name")
    rec = {"doi": DOI, "version": f"{meta['versionNumber']}.{meta['versionMinorNumber']}", "license": lic,
           "files": got}
    (d / "MANIFEST.json").write_text(json.dumps(rec, indent=1))
    return rec


if __name__ == "__main__":
    rec = main(sys.argv[1], *(sys.argv[2:3] or ["1.1"]), names=sys.argv[3:])
    print(json.dumps({"version": rec["version"], "license": rec["license"], "n_files": len(rec["files"])}))
