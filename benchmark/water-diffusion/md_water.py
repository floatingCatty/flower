"""Rigid-water MD for the size-dependent self-diffusion coefficient (Tazi et al., JPCM 24, 284117 (2012)).

  python3 md_water.py MODEL N SEED NS [DT_FS=2] [THREADS]   (MODEL: spce | tip4p2005)

N rigid molecules (parameters: the paper's Table 1) at 0.998 g/cm^3, T = 300 K, Nose-Hoover NVT, PME electrostatics
(real-space cutoff min(1.0 nm, 0.45 L)) with the long-range LJ correction. After equilibration, oxygen positions
(unwrapped) are stored every 1 ps; D_PBC comes from the slope of the mean squared displacement (multiple time
origins, fit window 10-100 ps). Checkpoints every 50 ps go to $FLOWER_STATE_DIR: a retried job resumes there.
"""
import json
import math
import os
import sys
import time

import numpy as np

MODELS = {   # d_OH (A), d_OM (A), HOH angle (deg), eps_O (kcal/mol), sigma_O (A), q_H        (paper, Table 1)
    "spce": dict(doh=1.0, dom=0.0, angle=109.471, eps=0.1554, sigma=3.1656, qh=0.4238),
    "tip4p2005": dict(doh=0.9572, dom=0.1546, angle=104.52, eps=0.1852, sigma=3.1589, qh=0.5564),
}
KCAL_KJ = 4.184
MASS_WATER = 18.01528   # g/mol
NA = 6.02214076e23


def forcefield_xml(name: str) -> str:
    p = MODELS[name]
    eps_kj, sig_nm, doh_nm = p["eps"] * KCAL_KJ, p["sigma"] / 10, p["doh"] / 10
    ang = math.radians(p["angle"])
    four = p["dom"] > 0
    qo = 0.0 if four else -2 * p["qh"]
    sites = ""
    types = (f'<Type name="{name}-O" class="OW" element="O" mass="15.99943"/>'
             f'<Type name="{name}-H" class="HW" element="H" mass="1.007947"/>')
    atoms = '<Atom name="O" type="%s-O"/><Atom name="H1" type="%s-H"/><Atom name="H2" type="%s-H"/>' % (name, name, name)
    nb = (f'<Atom type="{name}-O" charge="{qo}" sigma="{sig_nm}" epsilon="{eps_kj}"/>'
          f'<Atom type="{name}-H" charge="{p["qh"]}" sigma="1" epsilon="0"/>')
    if four:
        a = p["dom"] / (2 * p["doh"] * math.cos(ang / 2))          # M = O + a (H1 - O) + a (H2 - O)
        types += f'<Type name="{name}-M" class="MW" mass="0"/>'
        atoms += f'<Atom name="M" type="{name}-M"/>'
        sites = (f'<VirtualSite type="average3" siteName="M" atomName1="O" atomName2="H1" atomName3="H2" '
                 f'weight1="{1 - 2 * a}" weight2="{a}" weight3="{a}"/>')
        nb += f'<Atom type="{name}-M" charge="{-2 * p["qh"]}" sigma="1" epsilon="0"/>'
    return f"""<ForceField>
 <AtomTypes>{types}</AtomTypes>
 <Residues><Residue name="HOH">{atoms}{sites}<Bond atomName1="O" atomName2="H1"/><Bond atomName1="O" atomName2="H2"/></Residue></Residues>
 <HarmonicBondForce><Bond class1="OW" class2="HW" length="{doh_nm}" k="462750.4"/></HarmonicBondForce>
 <HarmonicAngleForce><Angle class1="HW" class2="OW" class3="HW" angle="{ang}" k="836.8"/></HarmonicAngleForce>
 <NonbondedForce coulomb14scale="0.833333" lj14scale="0.5">{nb}</NonbondedForce>
</ForceField>"""


def build(name: str, n: int, seed: int):
    import openmm.app as app
    import openmm.unit as u
    from openmm import Vec3
    p = MODELS[name]
    L = (n * MASS_WATER / (0.998 * NA)) ** (1 / 3) * 1e7      # nm
    top = app.Topology()
    chain = top.addChain()
    el = app.element
    rng = np.random.default_rng(seed)
    k = math.ceil(n ** (1 / 3))
    pos = []
    doh, ang = p["doh"] / 10, math.radians(p["angle"])
    local = np.array([[0, 0, 0], [doh, 0, 0], [doh * math.cos(ang), doh * math.sin(ang), 0]])
    for i in range(n):
        r = top.addResidue("HOH", chain)
        o = top.addAtom("O", el.oxygen, r)
        h1 = top.addAtom("H1", el.hydrogen, r)
        h2 = top.addAtom("H2", el.hydrogen, r)
        top.addBond(o, h1)
        top.addBond(o, h2)
        q, _ = np.linalg.qr(rng.normal(size=(3, 3)))                  # random orientation
        c = (np.array([i % k, (i // k) % k, i // (k * k)]) + 0.5) * L / k
        xyz = c + local @ q.T
        pos += [Vec3(*x) for x in xyz]
        if p["dom"] > 0:
            m = top.addAtom("M", None, r)
            pos.append(Vec3(*xyz[0]))
    top.setPeriodicBoxVectors([Vec3(L, 0, 0), Vec3(0, L, 0), Vec3(0, 0, L)])
    return top, pos * u.nanometer, L


def main(model, n, seed, ns, dt_fs=2.0, threads=None):
    import openmm
    import openmm.app as app
    import openmm.unit as u
    t0 = time.time()
    state_dir = os.environ.get("FLOWER_STATE_DIR") or "."
    with open("water_ff.xml", "w") as f:
        f.write(forcefield_xml(model))
    ff = app.ForceField("water_ff.xml")
    top, pos, L = build(model, n, seed)
    rc = min(1.0, 0.45 * L)
    system = ff.createSystem(top, nonbondedMethod=app.PME, nonbondedCutoff=rc * u.nanometer, rigidWater=True,
                             ewaldErrorTolerance=5e-4)
    for f in system.getForces():
        if isinstance(f, openmm.NonbondedForce):
            f.setUseDispersionCorrection(True)
    integ = openmm.NoseHooverIntegrator(300 * u.kelvin, 1 / u.picosecond, dt_fs * u.femtosecond)
    props = {"Threads": str(threads)} if threads else {}
    sim = app.Simulation(top, system, integ, openmm.Platform.getPlatformByName("CPU"), props)
    oxy = np.array([a.index for a in top.atoms() if a.name == "O"])
    steps_ps = int(round(1000 / dt_fs))
    nframes = int(round(ns * 1000))
    chk, traj_f = os.path.join(state_dir, "state.chk"), os.path.join(state_dir, "traj.npy")
    meta_f = os.path.join(state_dir, "progress.json")
    resumed = False
    if os.path.exists(chk) and os.path.exists(meta_f):
        sim.loadCheckpoint(chk)
        traj = list(np.load(traj_f)) if os.path.exists(traj_f) else []
        done = json.load(open(meta_f))["frames"]
        traj = traj[:done]
        resumed = True
        print(f"resumed from frame {done}", flush=True)
    else:
        sim.context.setPositions(pos)
        sim.minimizeEnergy()
        sim.context.setVelocitiesToTemperature(300 * u.kelvin, seed)
        sim.step(50 * steps_ps)                                         # 50 ps equilibration
        traj = []
    temps = []
    while len(traj) < nframes:
        sim.step(steps_ps)
        st = sim.context.getState(getPositions=True, getEnergy=True)
        traj.append(st.getPositions(asNumpy=True).value_in_unit(u.nanometer)[oxy].astype(np.float32))
        dof = 6 * n - 3
        temps.append(2 * st.getKineticEnergy().value_in_unit(u.kilojoule_per_mole) / (dof * 0.0083144626))
        if len(traj) % 50 == 0 or len(traj) == nframes:
            sim.saveCheckpoint(chk)
            np.save(traj_f, np.array(traj))
            json.dump({"frames": len(traj)}, open(meta_f, "w"))
    X = np.array(traj)                                                  # (frames, n, 3), unwrapped
    lags = np.arange(1, min(201, len(X)))
    msd = np.array([np.mean(np.sum((X[l:] - X[:-l]) ** 2, axis=2)) for l in lags])
    fit = (lags >= 10) & (lags <= 100)
    slope = np.polyfit(lags[fit], msd[fit], 1)[0]                       # nm^2 / ps
    D = slope / 6 * 1e3                                                 # nm^2/ps -> 1e-9 m^2/s
    # error estimate from four independent quarters of the trajectory
    parts = []
    for q in np.array_split(X, 4):
        if len(q) > 110:
            m = np.array([np.mean(np.sum((q[l:] - q[:-l]) ** 2, axis=2)) for l in lags[fit]])
            parts.append(np.polyfit(lags[fit], m, 1)[0] / 6 * 1e3)
    out = {"model": model, "N": n, "seed": seed, "ns": ns, "dt_fs": dt_fs, "L_nm": L, "cutoff_nm": rc,
           "D_pbc": D, "D_err": float(np.std(parts) / math.sqrt(len(parts))) if len(parts) > 1 else None,
           "T_mean": float(np.mean(temps)) if temps else None, "resumed": resumed,
           "seconds": round(time.time() - t0, 1), "dir": os.getcwd()}
    np.save("msd.npy", np.stack([lags, msd]))
    json.dump(out, open("result.json", "w"), indent=1)
    return out


if __name__ == "__main__":
    a = sys.argv
    r = main(a[1], int(a[2]), int(a[3]), float(a[4]), float(a[5]) if len(a) > 5 else 2.0,
             int(a[6]) if len(a) > 6 else None)
    print(json.dumps(r))
