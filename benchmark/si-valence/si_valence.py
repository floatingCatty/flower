"""Si valence band at Gamma: shared geometry, k-points, code drivers, parsers and analysis.

Standard library only (runs with any python3 on the remote machine); numpy/matplotlib are imported
only by the local analysis/plot functions.

Quantities, all with spin-orbit coupling, at the experimental lattice constant (a0 = 5.431 A):
* Delta_so = E(Gamma8) - E(Gamma7), the split-off gap;
* HH, LH, SO effective masses along [100] and [111], from the band curvature at three small |k|
  (fit E(k) - E(0) = -hbar^2 k^2 / 2m + c k^4), and the Luttinger parameters
  gamma1 = (1/m_hh + 1/m_lh)/2, gamma2 = (1/m_lh - 1/m_hh)/4 along [100], gamma3 the same along [111];
* under 1 % biaxial (001) tensile strain (eps_xx = eps_yy = 0.01, eps_zz = -2 C12/C11 eps_xx): the
  HH-LH splitting at Gamma (E1 - E2 of the top three Kramers pairs) and the gap to the third (E1 - E3).

Common result format written by every method (eig.json):
  {"method": ..., "spinor": true, "nelec": <valence electrons counted by the method>,
   "sets": {"unstrained": {"kpts": [[kx,ky,kz] in 2pi/a0, ...], "eig": [[eV per state ...] per k]},
            "strained":   {"kpts": [[0,0,0]], "eig": [[...]]}}, "notes": "..."}
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

A0 = 5.431            # experimental lattice constant (A)
BOHR = 0.529177210903  # A
HA = 27.211386245988  # eV
RY = HA / 2
HB2M = 3.80998212     # hbar^2 / (2 m0) in eV A^2
C11, C12 = 165.77, 63.93  # GPa, experiment
EXX = 0.01
EZZ = -2.0 * C12 / C11 * EXX
SMALL_K = (0.005, 0.01, 0.02)  # |k| in units of 2pi/a0

# ---------------------------------------------------------------------------- geometry and k-points

def strain_tensor(strained: bool):
    return (EXX, EXX, EZZ) if strained else (0.0, 0.0, 0.0)


def lattice(strained: bool):
    """fcc primitive vectors (A), possibly strained, and the two atoms in fractional coordinates."""
    e = strain_tensor(strained)
    base = [(0.0, 0.5, 0.5), (0.5, 0.0, 0.5), (0.5, 0.5, 0.0)]
    vecs = [tuple(A0 * v[i] * (1 + e[i]) for i in range(3)) for v in base]
    frac = [(0.0, 0.0, 0.0), (0.25, 0.25, 0.25)]  # no internal relaxation for (001) biaxial strain
    return vecs, frac


def kpoint_sets() -> dict:
    """Cartesian k-points in units of 2pi/a0 for each geometry."""
    ks = [[0.0, 0.0, 0.0]]
    for s in SMALL_K:
        ks.append([s, 0.0, 0.0])
    for s in SMALL_K:
        t = s / math.sqrt(3)
        ks.append([t, t, t])
    return {"unstrained": ks, "strained": [[0.0, 0.0, 0.0]]}


def cart_to_frac(k_cart, vecs):
    """k (2pi/a0 units, Cartesian) -> fractional coordinates of the reciprocal lattice of vecs (A)."""
    # k_frac_i = k . a_i / (2pi) with k in A^-1  ->  (k_cart * 2pi/a0) . a_i / 2pi = k_cart . a_i / a0
    return [sum(k_cart[j] * vecs[i][j] for j in range(3)) / A0 for i in range(3)]


def write_eig(path, method, sets, nelec, notes=""):
    Path(path).write_text(json.dumps({"method": method, "spinor": True, "nelec": nelec, "sets": sets,
                                      "notes": notes}, indent=1))


def run(cmd, log, cwd=None):
    with open(log, "w") as fh:
        p = subprocess.run(cmd, shell=True, stdout=fh, stderr=subprocess.STDOUT, cwd=cwd)
    if p.returncode != 0:
        tail = Path(log).read_text()[-2000:]
        raise SystemExit(f"command failed ({p.returncode}): {cmd}\n{tail}")


# ---------------------------------------------------------------------------- Quantum ESPRESSO

def qe_input(calc, vecs, frac, pseudo, kblock, nbnd=None, ecut=60):
    v = "\n".join(" ".join(f"{x / A0:.10f}" for x in vec) for vec in vecs)
    at = "\n".join(f"Si {f[0]:.6f} {f[1]:.6f} {f[2]:.6f}" for f in frac)
    sysextra = f"  nbnd = {nbnd}\n" if nbnd else ""
    return f"""&control
  calculation = '{calc}', prefix = 'si', outdir = './tmp', pseudo_dir = './'
/
&system
  ibrav = 0, celldm(1) = {A0 / BOHR:.10f}, nat = 2, ntyp = 1, ecutwfc = {ecut},
  noncolin = .true., lspinorb = .true., occupations = 'fixed'
{sysextra}/
&electrons
  conv_thr = 1e-11
/
ATOMIC_SPECIES
Si 28.0855 {pseudo}
CELL_PARAMETERS alat
{v}
ATOMIC_POSITIONS crystal
{at}
{kblock}
"""


def qe_eigs(xml_path):
    """Eigenvalues (eV) per k from data-file-schema.xml (full precision, Hartree in the file)."""
    text = Path(xml_path).read_text()
    out = []
    for m in re.finditer(r"<eigenvalues[^>]*>(.*?)</eigenvalues>", text, re.S):
        out.append([float(x) * HA for x in m.group(1).split()])
    return out


def run_qe(pseudo, np_=4, kmesh=8):
    sets = {}
    for name, ks in kpoint_sets().items():
        strained = name == "strained"
        vecs, frac = lattice(strained)
        d = Path(f"qe_{name}")
        d.mkdir(exist_ok=True)
        os.system(f"cp {pseudo} {d}/")
        scf = qe_input("scf", vecs, frac, Path(pseudo).name, f"K_POINTS automatic\n{kmesh} {kmesh} {kmesh} 0 0 0")
        (d / "scf.in").write_text(scf)
        run(f"mpirun -np {np_} pw.x -in scf.in", d / "scf.out", cwd=d)
        kb = "K_POINTS tpiba\n" + str(len(ks)) + "\n" + "\n".join(f"{k[0]:.10f} {k[1]:.10f} {k[2]:.10f} 1" for k in ks)
        (d / "bands.in").write_text(qe_input("bands", vecs, frac, Path(pseudo).name, kb, nbnd=16))
        run(f"mpirun -np {np_} pw.x -in bands.in", d / "bands.out", cwd=d)
        sets[name] = {"kpts": ks, "eig": qe_eigs(d / "tmp" / "si.save" / "data-file-schema.xml")}
    write_eig("eig.json", "QE 7.5 (PW, PBE, FR ONCV)", sets, nelec=8,
              notes=f"pw.x noncolin+lspinorb, ecutwfc 60 Ry, scf {kmesh}^3; pseudo {Path(pseudo).name}")


# ---------------------------------------------------------------------------- ABACUS (plane waves)

def abacus_stru(vecs, frac, pseudo, orbital=None):
    v = "\n".join(" ".join(f"{x / A0:.10f}" for x in vec) for vec in vecs)
    at = "\n".join(f"{f[0]:.6f} {f[1]:.6f} {f[2]:.6f} 0 0 0" for f in frac)
    orb = f"NUMERICAL_ORBITAL\n{orbital}\n\n" if orbital else ""
    return (f"ATOMIC_SPECIES\nSi 28.0855 {pseudo}\n\n{orb}LATTICE_CONSTANT\n{A0 / BOHR:.10f}\n\n"
            f"LATTICE_VECTORS\n{v}\n\nATOMIC_POSITIONS\nDirect\n\nSi\n0.0\n2\n{at}\n")


def abacus_input(calc, nbands=16, basis="pw"):
    lines = ["INPUT_PARAMETERS", "suffix si", f"calculation {calc}", f"basis_type {basis}", "pseudo_dir ./",
             "orbital_dir ./",
             "ecutwfc 60", "nspin 4", "lspinorb 1", "noncolin 0", "scf_thr 1e-10", "smearing_method fixed",
             f"nbands {nbands}", "symmetry 0"]
    if calc == "nscf":
        lines += ["init_chg file", "out_band 1"]
    else:
        lines += ["out_chg 1"]
    return "\n".join(lines) + "\n"


def abacus_eigs(out_dir, nk):
    """Eigenvalues (eV) per k of the nscf run. BANDS_1.dat (one row per k: index, path coordinate, energies) is
    written by every nscf with out_band; istate.info may still be the SCF's (LCAO nscf does not rewrite it), so a
    file is accepted only if it holds EXACTLY nk k-points: a mismatch must never pass silently."""
    p = Path(out_dir) / "BANDS_1.dat"
    if p.exists():
        rows = [ln.split() for ln in p.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        if len(rows) == nk:
            return [[float(x) for x in r[2:]] for r in rows]
    p = Path(out_dir) / "istate.info"
    if p.exists():
        blocks, cur = [], None
        for line in p.read_text().splitlines():
            if "Kpoint" in line:
                cur = []
                blocks.append(cur)
                continue
            f = line.split()
            if cur is not None and len(f) >= 2 and re.fullmatch(r"\d+", f[0]):
                cur.append(float(f[1]))
        if len(blocks) == nk:
            return blocks
    raise SystemExit(f"no eigenvalue file with exactly {nk} k-points in {out_dir}: {os.listdir(out_dir)}")


def run_abacus(pseudo, np_=4, kmesh=8, orbital=None):
    basis = "lcao" if orbital else "pw"
    sets = {}
    for name, ks in kpoint_sets().items():
        vecs, frac = lattice(name == "strained")
        d = Path(f"abacus_{basis}_{name}")
        d.mkdir(exist_ok=True)
        os.system(f"cp {pseudo} {orbital or ''} {d}/")
        (d / "STRU").write_text(abacus_stru(vecs, frac, Path(pseudo).name, Path(orbital).name if orbital else None))
        (d / "INPUT").write_text(abacus_input("scf", basis=basis))
        (d / "KPT").write_text(f"K_POINTS\n0\nGamma\n{kmesh} {kmesh} {kmesh} 0 0 0\n")
        run(f"mpirun -np {np_} abacus", d / "scf.log", cwd=d)
        (d / "INPUT").write_text(abacus_input("nscf", basis=basis))
        kd = [cart_to_frac(k, vecs) for k in ks]
        (d / "KPT").write_text("K_POINTS\n" + str(len(kd)) + "\nDirect\n"
                               + "\n".join(f"{k[0]:.10f} {k[1]:.10f} {k[2]:.10f} 1" for k in kd) + "\n")
        run(f"mpirun -np {np_} abacus", d / "nscf.log", cwd=d)
        sets[name] = {"kpts": ks, "eig": abacus_eigs(d / "OUT.si", len(ks))}
    write_eig("eig.json", f"ABACUS 3.9 ({basis.upper()}, PBE, FR ONCV)", sets, nelec=8,
              notes=f"basis_type {basis}, nspin 4 + lspinorb, ecutwfc 60 Ry, scf {kmesh}^3; pseudo {Path(pseudo).name}"
                    + (f", orbitals {Path(orbital).name}" if orbital else ""))


# ---------------------------------------------------------------------------- DFTB+

def dftb_input(vecs, frac, skdir, xi_p, kblock, scc_read=False):
    v = "\n".join(" ".join(f"{x:.10f}" for x in vec) for vec in vecs)
    at = "\n".join(f"{i + 1} 1 {f[0]:.8f} {f[1]:.8f} {f[2]:.8f}" for i, f in enumerate(frac))
    return f"""Geometry = GenFormat {{
2 F
Si
{at}
0.0 0.0 0.0
{v}
}}
Hamiltonian = DFTB {{
  SCC = Yes
  SCCTolerance = 1e-10
  {"ReadInitialCharges = Yes" if scc_read else ""}
  {"MaxSCCIterations = 1" if scc_read else ""}
  {"ConvergentSCCOnly = No" if scc_read else ""}
  MaxAngularMomentum {{ Si = "p" }}
  SlaterKosterFiles = Type2FileNames {{
    Prefix = "{skdir}/"
    Separator = "-"
    Suffix = ".skf"
  }}
  SpinOrbit = {{ Si [eV] = {{ 0.0 {xi_p:.8f} }} }}  # one constant per shell: s, p
  Filling = Fermi {{ Temperature [K] = 1 }}
{kblock}
}}
Options {{ WriteResultsTag = Yes }}
Analysis {{ }}
ParserOptions {{ ParserVersion = 14 }}
"""


def dftb_eigs(path, nk):
    """eigenvalues from results.tag (Hartree, full precision): 'eigenvalues :real:3:nstate,nk,nspin'."""
    text = Path(path).read_text()
    m = re.search(r"^eigenvalues\s*:real:3:(\d+),(\d+),(\d+)\s*\n(.*?)(?=^\S)", text, re.S | re.M)
    if not m:
        raise SystemExit("no eigenvalues in results.tag")
    ns, nks = int(m.group(1)), int(m.group(2))
    vals = [float(x) * HA for x in m.group(4).split()]
    return [vals[i * ns:(i + 1) * ns] for i in range(min(nk, nks))]


def run_dftb(skdir, xi_p, kmesh=8):
    sets = {}
    for name, ks in kpoint_sets().items():
        vecs, frac = lattice(name == "strained")
        d = Path(f"dftb_{name}")
        d.mkdir(exist_ok=True)
        fold = f"  KPointsAndWeights = SupercellFolding {{\n {kmesh} 0 0\n 0 {kmesh} 0\n 0 0 {kmesh}\n 0.0 0.0 0.0\n  }}"
        (d / "dftb_in.hsd").write_text(dftb_input(vecs, frac, skdir, xi_p, fold))
        run("dftb+", d / "scf.log", cwd=d)
        kf = [cart_to_frac(k, vecs) for k in ks]
        kb = "  KPointsAndWeights = {\n" + "\n".join(f"   {k[0]:.10f} {k[1]:.10f} {k[2]:.10f} 1.0" for k in kf) + "\n  }"
        (d / "dftb_in.hsd").write_text(dftb_input(vecs, frac, skdir, xi_p, kb, scc_read=True))
        run("dftb+", d / "bands.log", cwd=d)
        sets[name] = {"kpts": ks, "eig": dftb_eigs(d / "results.tag", len(ks))}
    write_eig("eig.json", "DFTB+ 25.1 (pbc-0-3, SOC)", sets, nelec=8,
              notes=f"SCC DFTB, pbc-0-3 Slater-Koster set, on-site SOC xi_p(Si) = {xi_p:.5f} eV from the QE ld1.x "
                    f"all-electron atom; scf {kmesh}^3")


# ---------------------------------------------------------------------------- atom: spin-orbit constant

def run_atom_soc():
    """Scalar->full relativistic all-electron Si atom (QE ld1.x, PBE): 3p1/2 vs 3p3/2 -> xi = 2/3 split."""
    Path("ld1.in").write_text("""&input
  title = 'Si', zed = 14., rel = 2, config = '[Ne] 3s2 3p2', iswitch = 1, dft = 'PBE'
/
""")
    run("ld1.x < ld1.in", "ld1.out")
    levels = {}
    for line in Path("ld1.out").read_text().splitlines():
        # "     3 1 0.5 3P 1( 2.00)        -0.3006        -0.1503        -4.0899"  (n l j nl ... Ry Ha eV)
        m = re.match(r"\s*3\s+1\s+([\d.]+)\s+3P\s+\d\(\s*[\d.]+\)\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)", line)
        if m:
            levels.setdefault(m.group(1), float(m.group(2)))  # j -> eigenvalue in eV
    if "0.5" not in levels or "1.5" not in levels:
        raise SystemExit("could not find 3p1/2 and 3p3/2 levels in ld1.out:\n" + Path("ld1.out").read_text()[-3000:])
    split = levels["1.5"] - levels["0.5"]
    xi = 2.0 / 3.0 * split
    Path("atom.json").write_text(json.dumps({"e_3p12_eV": levels["0.5"], "e_3p32_eV": levels["1.5"],
                                             "split_eV": split, "xi_p_eV": xi}))
    print(json.dumps({"xi_p_eV": xi, "split_eV": split, "summary": f"Si atom 3p spin-orbit split {split * 1000:.2f} meV, "
                      f"xi_p = {xi * 1000:.2f} meV"}))


# ---------------------------------------------------------------------------- analysis (local)

def valence_top6(eig, nelec, spinor=True):
    """The six highest occupied spinor states (Gamma25' / Gamma8 + Gamma7 family), highest first."""
    occ = sorted(eig)[:nelec] if spinor else None
    return sorted(occ, reverse=True)[:6]


def pairs(top6):
    """Kramers pairs -> 3 energies (mean of each pair), highest first."""
    return [(top6[0] + top6[1]) / 2, (top6[2] + top6[3]) / 2, (top6[4] + top6[5]) / 2]


def fit_mass(ks, dE):
    """Least squares dE = b k^2 + c k^4 (k in A^-1) -> m*/m0 = -HB2M / b (holes: b < 0 -> positive mass)."""
    s22 = sum(k ** 4 for k in ks); s24 = sum(k ** 6 for k in ks); s44 = sum(k ** 8 for k in ks)
    t2 = sum(e * k ** 2 for k, e in zip(ks, dE)); t4 = sum(e * k ** 4 for k, e in zip(ks, dE))
    det = s22 * s44 - s24 * s24
    b = (t2 * s44 - t4 * s24) / det
    return -HB2M / b


def analyse(eig_doc: dict) -> dict:
    nel = eig_doc["nelec"]
    un = eig_doc["sets"]["unstrained"]
    kk = un["kpts"]
    top = [pairs(valence_top6(e, nel)) for e in un["eig"]]
    g = top[0]
    res = {"method": eig_doc["method"], "notes": eig_doc.get("notes", ""),
           "dso_meV": (g[0] - g[2]) * 1000, "gamma8_split_meV": (g[0] - g[1]) * 1000}
    kn = [math.sqrt(sum(c * c for c in k)) * 2 * math.pi / A0 for k in kk]
    i100 = [i for i, k in enumerate(kk) if k[1] == 0 and k[2] == 0 and k[0] > 0]
    i111 = [i for i, k in enumerate(kk) if k[0] > 0 and abs(k[0] - k[1]) < 1e-12 and abs(k[1] - k[2]) < 1e-12]
    for tag, idx in (("100", i100), ("111", i111)):
        ks = [kn[i] for i in idx]
        for b, name in enumerate(("hh", "lh", "so")):
            res[f"m_{name}_{tag}"] = fit_mass(ks, [top[i][b] - g[b] for i in idx])
    res["gamma1"] = (1 / res["m_hh_100"] + 1 / res["m_lh_100"]) / 2
    res["gamma2"] = (1 / res["m_lh_100"] - 1 / res["m_hh_100"]) / 4
    res["gamma3"] = (1 / res["m_lh_111"] - 1 / res["m_hh_111"]) / 4
    st = eig_doc["sets"].get("strained")
    if st:
        s = pairs(valence_top6(st["eig"][0], nel))
        res["strain_split12_meV"] = (s[0] - s[1]) * 1000
        res["strain_split13_meV"] = (s[0] - s[2]) * 1000
    return res


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "qe":
        run_qe(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 4)
    elif what == "abacus":
        run_abacus(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 4)
    elif what == "abacus-lcao":
        run_abacus(sys.argv[2], int(sys.argv[4]) if len(sys.argv) > 4 else 4, orbital=sys.argv[3])
    elif what == "dftb":
        run_dftb(sys.argv[2], float(sys.argv[3]))
    elif what == "atom":
        run_atom_soc()
    elif what == "analyse":
        print(json.dumps(analyse(json.loads(Path(sys.argv[2]).read_text())), indent=1))
    else:
        raise SystemExit(f"unknown: {what}")
