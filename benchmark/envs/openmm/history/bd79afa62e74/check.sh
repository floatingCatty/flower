# python3 from this environment; OpenMM's CPU platform runs 50 steps of a small rigid-water (SPC/E) box.
set -e
[ "$(command -v python3)" = "$FLOWER_ENV_PREFIX/bin/python3" ]
python3 - <<'PY'
import openmm, openmm.app as app, openmm.unit as u
pdb = app.PDBFile(app.__file__.replace("__init__.py", "data/spce.pdb"))
ff = app.ForceField("spce.xml")
m = app.Modeller(pdb.topology, pdb.positions)
m.addSolvent(ff, model="spce", boxSize=openmm.Vec3(1.8, 1.8, 1.8) * u.nanometer)
s = ff.createSystem(m.topology, nonbondedMethod=app.PME, nonbondedCutoff=0.8 * u.nanometer, constraints=app.HBonds,
                    rigidWater=True)
sim = app.Simulation(m.topology, s, openmm.NoseHooverIntegrator(300 * u.kelvin, 1 / u.picosecond, 0.002 * u.picosecond),
                     openmm.Platform.getPlatformByName("CPU"))
sim.context.setPositions(m.positions)
sim.minimizeEnergy(maxIterations=50)
sim.step(50)
print("openmm", openmm.__version__, "CPU platform: 50 steps of", m.topology.getNumResidues(), "SPC/E waters OK")
PY
