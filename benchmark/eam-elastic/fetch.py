"""The potential files of Tables 1-3, fetched by their exact file name: from the LAMMPS repository's potentials/ at
a fixed release tag, else from a download link of that name on the NIST Interatomic Potentials Repository's Cu, Al
and Ni pages (the paper's two sources). Writes potentials/<file> and potentials.json ({file: {metals, sha256,
source}}); prints the outputs.

  python3 fetch.py TABLES_JSON TAG
Names found in neither are listed as missing; they are not guessed.
"""
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

tables, tag = json.load(open(sys.argv[1])), sys.argv[2]
want = {}
for metal, t in tables.items():
    for r in t["rows"]:
        if r["potential"]:
            want.setdefault(r["potential"], []).append(metal)


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=120).read()


nist = {}
for el in ("Cu", "Al", "Ni"):
    base = f"https://www.ctcms.nist.gov/potentials/system/{el}/"
    for href in re.findall(r'href="([^"]*/Download/[^"]+)"', get(base).decode("utf8", "replace")):
        nist.setdefault(href.rsplit("/", 1)[-1], urllib.parse.urljoin(base, href))   # links are relative to the page
os.makedirs("potentials", exist_ok=True)
found, missing = {}, []
for name, metals in sorted(want.items()):
    url = f"https://raw.githubusercontent.com/lammps/lammps/{tag}/potentials/{name}"
    try:
        data = get(url)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        if name not in nist:
            missing.append(name)
            continue
        url = nist[name]
        data = get(url)
    open(os.path.join("potentials", name), "wb").write(data)
    found[name] = {"metals": metals, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "source": url}
json.dump(found, open("potentials.json", "w"), indent=1)
print(json.dumps({"tag": tag, "n_found": len(found), "from_nist": sum("nist.gov" in v["source"] for v in found.values()), "n_missing": len(missing), "missing": missing,
                  "pairs": sum(len(v["metals"]) for v in found.values()),
                  "items": [{"metal": m, "pot": f} for f, v in sorted(found.items()) for m in v["metals"]]}))
