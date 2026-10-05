"""C11, C12, C44 of one fcc metal with one EAM potential at 300 K by LAMMPS molecular dynamics, two ways.

  python3 elastic.py METAL POTENTIAL_FILE [N_CELLS [PS_PER_STAGE [EPS]]]

0. Equilibration: NPT at 300 K and zero pressure; the box is then fixed at its average size (the thermal lattice
   constant a300) and the crystal re-equilibrated under NVT (Nose-Hoover). Saved as a restart.
1. Static, the linear elastic constants: four strained states in turn, exx = +-EPS and engineering shear
   gamma_yz = +-EPS, each re-equilibrated and sampled; time-averaged stresses (sigma = -p) and central differences:
   C11 = d sigma_xx/d exx, C12 = d (sigma_yy + sigma_zz)/2 / d exx, C44 = d sigma_yz/d gamma_yz. Errors from 10
   block averages per state, in quadrature. Third-order terms cancel in the central difference.
2. The paper's protocol (from the same restart): tension along x at a constant strain rate of 1e-3/ps under NVT
   with y and z fixed, and separately an engineering shear yz at the same rate; slopes of sigma_xx and
   (sigma_yy + sigma_zz)/2 versus exx, and of sigma_yz versus gamma_yz, fitted on [0, emax] for emax = 0.5 %, 1 % and
   2 % ("the linear part" of the curve; the paper does not give its range).
Runs `mpirun -np $FLOWER_CPUS lmp`; writes in.elastic, log.lammps, samples_*.txt, elastic.json; prints the outputs.
The pair style follows the file: *.eam -> eam (funcfl), *.eam.fs -> eam/fs, anything else (setfl: *.eam.alloy,
*.setfl, *.set) -> eam/alloy; in multi-element files the metal is mapped by its symbol.
"""
import json
import os
import subprocess
import sys
import time

import numpy as np

A0 = {"Cu": 3.615, "Al": 4.05, "Ni": 3.52}       # starting guesses only; NPT finds a(300 K)
T = 300.0
RATE = 0.001                                     # 1/ps, the paper's strain rate
EMAX = (0.005, 0.01, 0.02)


def style_and_coeff(path, metal):
    name = os.path.basename(path)
    if name.endswith(".eam"):
        return "eam", f"pair_coeff 1 1 {path}"
    style = "eam/fs" if name.endswith(".eam.fs") else "eam/alloy"
    elems = open(path, errors="replace").read().splitlines()[3].split()[1:]   # line 4: N and the symbols
    if metal not in elems:
        raise SystemExit(f"{name} has no {metal} (elements {elems})")
    return style, f"pair_coeff * * {path} {metal}"


def main(metal, pot, n=10, ps=20.0, eps=0.01):
    t0 = time.time()
    pot = os.path.abspath(pot)
    style, coeff = style_and_coeff(pot, metal)
    steps = int(ps * 1000)                      # 1 fs time step
    model = [f"pair_style {style}", coeff, "neighbor 1.0 bin", "timestep 0.001"]
    L = ["units metal", "boundary p p p", "atom_modify map array",
         f"lattice fcc {A0[metal]}", f"region box prism 0 {n} 0 {n} 0 {n} 0 0 0", "create_box 1 box",
         "create_atoms 1 box", *model,
         "thermo_style custom step temp press pxx pyy pzz pyz lx ly lz yz", "thermo 1000",
         f"velocity all create {2 * T} 4928459 mom yes rot yes dist gaussian",
         f"fix npt all npt temp {T} {T} 0.1 aniso 0 0 1.0", f"run {steps}",
         "variable lxv equal lx", "variable lyv equal ly", "variable lzv equal lz",
         f"fix box all ave/time 10 {steps // 10} {steps} v_lxv v_lyv v_lzv", f"run {steps}",
         "variable L equal (f_box[1]+f_box[2]+f_box[3])/3", "variable Lfix equal ${L}",
         'print "A300 $(v_Lfix/' + str(n) + ':%.6f)"', "unfix box", "unfix npt",
         "change_box all x final 0 ${Lfix} y final 0 ${Lfix} z final 0 ${Lfix} remap units box",
         f"fix nvt all nvt temp {T} {T} 0.1", f"run {steps // 2}", "write_restart eq.restart",
         "variable pxx equal pxx", "variable pyy equal pyy", "variable pzz equal pzz", "variable pyz equal pyz"]
    # 1. static: strained states relative to the cubic box
    for tag, cmd, undo in (
            ("xp", f"change_box all x final 0 $(v_Lfix*(1+{eps})) remap units box", "change_box all x final 0 ${Lfix} remap units box"),
            ("xm", f"change_box all x final 0 $(v_Lfix*(1-{eps})) remap units box", "change_box all x final 0 ${Lfix} remap units box"),
            ("sp", f"change_box all yz final $(v_Lfix*{eps}) remap units box", "change_box all yz final 0 remap units box"),
            ("sm", f"change_box all yz final $(v_Lfix*{-eps}) remap units box", "change_box all yz final 0 remap units box")):
        L += [cmd, f"run {steps // 2}",
              f"fix s all print 10 \"$(step) $(temp) $(v_pxx) $(v_pyy) $(v_pzz) $(v_pyz)\" file samples_{tag}.txt screen no",
              f"run {steps}", "unfix s", undo]
    # 2. the paper's protocol: constant-rate tension and shear from the equilibrated state
    rate_steps = int(round(max(EMAX) / RATE * 1000))
    for tag, deform, strain in (("tension", f"x erate {RATE} remap x", "(lx-v_Lfix)/v_Lfix"),
                                ("shear", f"yz erate {RATE} remap x", "yz/lz")):
        L += ["clear", "read_restart eq.restart", *model,    # variables (Lfix, pxx, ...) survive `clear`
              f"fix nvt all nvt temp {T} {T} 0.1", "variable e equal " + strain,
              f"fix d all deform 1 {deform} units box",
              f"fix s all print 10 \"$(step) $(v_e) $(v_pxx) $(v_pyy) $(v_pzz) $(v_pyz)\" file samples_{tag}.txt screen no",
              f"run {rate_steps}", "unfix s", "unfix d"]
    open("in.elastic", "w").write("\n".join(L) + "\n")
    cpus = os.environ.get("FLOWER_CPUS", "4")
    r = subprocess.run(["mpirun", "--oversubscribe", "-np", cpus, "lmp", "-in", "in.elastic", "-log", "log.lammps",
                        "-screen", "none"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-3000:] + r.stderr[-3000:])
        raise SystemExit(f"lammps failed ({r.returncode})")
    a300 = float(next(l.split()[1] for l in open("log.lammps") if l.startswith("A300 ")))

    def state(tag):                             # mean stress (GPa) and block errors, temperature
        d = np.loadtxt(f"samples_{tag}.txt", comments="#")
        sig = -d[:, 2:6] / 1e4                  # sigma = -p, bar -> GPa
        means = np.array([b.mean(axis=0) for b in np.array_split(sig, 10)])
        return sig.mean(axis=0), means.std(axis=0, ddof=1) / np.sqrt(10), d[:, 1].mean()
    (xp, exp_, tp), (xm, exm, tm) = state("xp"), state("xm")
    (sp, esp, _), (sm, esm, _) = state("sp"), state("sm")
    err = lambda a, b: float(np.hypot(a, b) / (2 * eps))
    out = {"metal": metal, "potential": os.path.basename(pot), "style": style, "atoms": 4 * n ** 3,
           "a300": a300, "T": float((tp + tm) / 2),
           "C11": float((xp[0] - xm[0]) / (2 * eps)), "C11_err": err(exp_[0], exm[0]),
           "C12": float(((xp[1] + xp[2]) - (xm[1] + xm[2])) / 2 / (2 * eps)),
           "C12_err": err(np.hypot(exp_[1], exp_[2]) / 2, np.hypot(exm[1], exm[2]) / 2),
           "C44": float((sp[3] - sm[3]) / (2 * eps)), "C44_err": err(esp[3], esm[3])}

    def slope(x, y, emax):                      # least squares with intercept on 0 <= x <= emax
        m = (x >= 0) & (x <= emax)
        k, cov = np.polyfit(x[m], y[m], 1, cov=True)
        r2 = 1 - np.sum((y[m] - np.polyval(k, x[m])) ** 2) / np.sum((y[m] - y[m].mean()) ** 2)
        return float(k[0]), float(np.sqrt(cov[0, 0])), float(r2)
    ten, sh = np.loadtxt("samples_tension.txt", comments="#"), np.loadtxt("samples_shear.txt", comments="#")
    sx, syz = -ten[:, 2] / 1e4, -(ten[:, 3] + ten[:, 4]) / 2 / 1e4
    for emax in EMAX:
        tag = "%g" % (100 * emax)
        for key, x, y in (("C11", ten[:, 1], sx), ("C12", ten[:, 1], syz), ("C44", sh[:, 1], -sh[:, 5] / 1e4)):
            k, ek, r2 = slope(x, y, emax)
            out[f"{key}_rate_{tag}"], out[f"{key}_rate_{tag}_err"], out[f"{key}_rate_{tag}_r2"] = k, ek, r2
    out.update(ps_per_stage=ps, eps=eps, rate=RATE, seconds=round(time.time() - t0, 1))
    json.dump(out, open("elastic.json", "w"), indent=1)
    print(json.dumps(out))


if __name__ == "__main__":
    a = sys.argv[3:]
    main(sys.argv[1], sys.argv[2], *[f(x) for f, x in zip((int, float, float), a)])
