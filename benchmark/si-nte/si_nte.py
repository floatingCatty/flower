"""Quantum ESPRESSO driver for the Si quasi-harmonic study (Rignanese, Michenaud & Gonze, PRB 53, 4488, 1996).

The paper: LDA (Ceperley-Alder), norm-conserving Hamann pseudopotential, 10 Ha plane waves, 10 special k points
(the 4x4x4 shifted mesh), DFPT dynamical matrices -> interatomic force constants -> phonons.
Here: QE with the classic LDA (Perdew-Zunger fit of Ceperley-Alder) norm-conserving Si.pz-vbc.UPF.

  python3 si_nte.py scf     A ECUT_RY K NPROC        -> {"a": .., "energy_ry": ..}
  python3 si_nte.py phonon  A ECUT_RY K Q NPROC      -> frequencies at X and L, phonon DOS (phdos.dat)
"""

import json
import os
import re
import subprocess
import sys

PSEUDO = "Si.upf"   # fetched by get_pseudo.py (the pseudopotential is a plan input)


def pw_input(a: float, ecut: float, k: int, prefix: str = "si") -> str:
    return f"""&control
  calculation = 'scf', prefix = '{prefix}', outdir = './tmp', pseudo_dir = './', tprnfor = .true., tstress = .true.
/
&system
  ibrav = 2, celldm(1) = {a:.6f}, nat = 2, ntyp = 1, ecutwfc = {ecut}
/
&electrons
  conv_thr = 1.0d-12
/
ATOMIC_SPECIES
  Si 28.0855 {PSEUDO}
ATOMIC_POSITIONS alat
  Si 0.00 0.00 0.00
  Si 0.25 0.25 0.25
K_POINTS automatic
  {k} {k} {k} 1 1 1
"""


def run(cmd: str, inp: str, out: str):
    with open(inp) as fi, open(out, "w") as fo:
        r = subprocess.run(cmd, shell=True, stdin=fi, stdout=fo, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        tail = open(out).read()[-2000:]
        raise SystemExit(f"{cmd} < {inp} failed ({r.returncode}):\n{tail}")


def mpi(np_: int, exe: str) -> str:
    return f"mpirun --oversubscribe -np {np_} {exe}" if np_ > 1 else exe


def scf(a: float, ecut: float, k: int, np_: int) -> dict:
    open("scf.in", "w").write(pw_input(a, ecut, k))
    run(mpi(np_, "pw.x -nk 2" if np_ > 1 else "pw.x"), "scf.in", "scf.out")
    txt = open("scf.out").read()
    e = float(re.findall(r"^!\s+total energy\s+=\s+(-?[\d.]+) Ry", txt, re.M)[-1])
    p = float(re.findall(r"P=\s*(-?[\d.]+)", txt)[-1])
    return {"a": a, "energy_ry": e, "pressure_kbar": p, "ecut_ry": ecut, "k": k}


def phonon(a: float, ecut: float, k: int, q: int, np_: int) -> dict:
    res = scf(a, ecut, k, np_)
    open("ph.in", "w").write(f"""phonons of Si
&inputph
  prefix = 'si', outdir = './tmp', fildyn = 'si.dyn', tr2_ph = 1.0d-16, ldisp = .true.,
  nq1 = {q}, nq2 = {q}, nq3 = {q}
/
""")
    run(mpi(np_, "ph.x -nk 2" if np_ > 1 else "ph.x"), "ph.in", "ph.out")
    open("q2r.in", "w").write("&input\n  fildyn = 'si.dyn', zasr = 'crystal', flfrc = 'si.fc'\n/\n")
    run("q2r.x", "q2r.in", "q2r.out")
    # TA(X), TA(L) (and all branches) at Gamma, X, L; without and with the acoustic sum rule
    freqs = {}
    for asr in ("no", "crystal"):
        open(f"md_{asr}.in", "w").write(f"""&input
  asr = '{asr}', flfrc = 'si.fc', flfrq = 'si_{asr}.freq'
/
3
0.0 0.0 0.0
1.0 0.0 0.0
0.5 0.5 0.5
""")
        run("matdyn.x", f"md_{asr}.in", f"md_{asr}.out")
        freqs[asr] = read_freq(f"si_{asr}.freq.gp")
    # phonon density of states on a fine mesh (for the free energy)
    open("dos.in", "w").write("""&input
  asr = 'crystal', flfrc = 'si.fc', dos = .true., fldos = 'phdos.dat', nk1 = 40, nk2 = 40, nk3 = 40, deltaE = 0.5
/
""")
    run("matdyn.x", "dos.in", "dos.out")
    G, X, L = freqs["no"]
    res.update({"q": q, "gamma_cm": G, "X_cm": X, "L_cm": L,
                "TA_X_cm": X[0], "TA_L_cm": L[0], "TA_X_asr_cm": freqs["crystal"][1][0],
                "TA_L_asr_cm": freqs["crystal"][2][0], "LO_gamma_cm": G[-1]})
    return res


def read_freq(path: str):
    rows = [list(map(float, ln.split())) for ln in open(path) if ln.strip()]
    return [sorted(r[1:]) for r in rows]


def eos(ecut: float, k: int, np_: int, alist: str) -> dict:
    """The same EOS points in one job, for a convergence study: energies at several lattice constants."""
    a_s = [float(x) for x in alist.split(",")]
    E, P = [], []
    for a in a_s:
        r = scf(a, ecut, k, np_)
        E.append(r["energy_ry"])
        P.append(r["pressure_kbar"])
    out = {"ecut_ry": ecut, "k": k, "a": a_s, "energy_ry": E, "pressure_kbar": P}
    json.dump(out, open("eos.json", "w"), indent=1)
    return {"ecut_ry": ecut, "n": len(a_s), "e_min_ry": min(E)}


def main(argv):
    mode = argv[1]
    if mode == "eos":
        out = eos(float(argv[2]), int(argv[3]), int(argv[4]), argv[5])
        out["dir"] = os.getcwd()
        print(json.dumps(out))
        return
    a, ecut, k = float(argv[2]), float(argv[3]), int(argv[4])
    if mode == "scf":
        out = scf(a, ecut, k, int(argv[5]))
    else:
        out = phonon(a, ecut, k, int(argv[5]), int(argv[6]))
    out["dir"] = os.getcwd()
    json.dump(out, open("result.json", "w"), indent=1)
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv)
