"""Semi-empirical and semi-classical models of the Si valence band, all with spin-orbit coupling.

usage: python models.py tb|epm|kp   ->  eig.json in the common format of si_valence.py

* tb  : nearest-neighbour sp3d5s* tight binding, Si parameters of Boykin, Klimeck & Oyafuso,
        PRB 69, 115201 (2004), Table IV (room temperature). Spin-orbit: delta * L.sigma on p with
        delta = Delta/3. Strain: two-centre integrals scaled as (d0/d)^2 (Harrison), on-site energies
        unchanged (the d-shift strain parameters of the NEMO model are not used).
* epm : local empirical pseudopotential, Cohen & Bergstresser, PRB 141, 789 (1966): V(sqrt3) = -0.21,
        V(sqrt8) = 0.04, V(sqrt11) = 0.08 Ry at a = 5.43 A. For strain the form factors are a cubic
        spline in q^2 through V(0) = -2/3 E_F, the three values and 0 beyond q^2 = 13 (2pi/a)^2, scaled
        by the atomic volume. Spin-orbit: -i mu (K x K').sigma S(G), mu FITTED to Delta_so = 44 meV.
* kp  : 6-band Luttinger-Kohn + Bir-Pikus with experimental gamma1..3, Delta_so, b, d (reference).
"""
import json
import math
import sys

import numpy as np

import si_valence as sv

HB2M = sv.HB2M
TWO_PI_A = 2 * math.pi / sv.A0  # A^-1 per (2pi/a0)

# ============================================================================ tight binding (sp3d5s*)

TB = {  # Boykin, Klimeck, Oyafuso PRB 69 115201 (2004), Table IV, Si (eV)
    "Es": -2.15168, "Ep": 4.22925, "Es*": 19.11650, "Ed": 13.78950, "delta": 0.01989,
    "ss": -1.95933, "s*s*": -4.24135, "ss*": -1.52230, "sp": 3.02562, "s*p": 3.15565,
    "sd": -2.28485, "s*d": -0.80993, "ppS": 4.10364, "ppP": -1.51801, "pdS": -1.35554,
    "pdP": 2.38479, "ddS": -1.68136, "ddP": 2.58880, "ddD": -1.81400,
}
ORB = ["s", "x", "y", "z", "xy", "yz", "zx", "x2y2", "z2", "s*"]
L_OF = {"s": 0, "s*": 0, "x": 1, "y": 1, "z": 1, "xy": 2, "yz": 2, "zx": 2, "x2y2": 2, "z2": 2}


def _dquad():
    """Real d orbitals as symmetric quadratic forms r^T Q r (normalised so tr(Qa Qb) = delta_ab)."""
    Q = {}
    def sym(i, j):
        m = np.zeros((3, 3)); m[i, j] = m[j, i] = 1 / math.sqrt(2); return m
    Q["xy"], Q["yz"], Q["zx"] = sym(0, 1), sym(1, 2), sym(2, 0)
    Q["x2y2"] = np.diag([1, -1, 0]) / math.sqrt(2)
    Q["z2"] = np.diag([-1, -1, 2]) / math.sqrt(6)
    return Q


DQ = _dquad()
DNAMES = ["xy", "yz", "zx", "x2y2", "z2"]


def _rotation_to(dhat):
    """Orthogonal R with R e_z = dhat."""
    z = np.asarray(dhat, float); z /= np.linalg.norm(z)
    t = np.array([1.0, 0, 0]) if abs(z[0]) < 0.9 else np.array([0, 1.0, 0])
    x = t - z * np.dot(t, z); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.column_stack([x, y, z])


def _bond_frame(V):
    """Matrix <a|H|b> in the bond frame (bond along +z from atom 1 to atom 2), basis ORB."""
    H = np.zeros((10, 10))
    i = {o: k for k, o in enumerate(ORB)}
    H[i["s"], i["s"]] = V["ss"]; H[i["s*"], i["s*"]] = V["s*s*"]
    H[i["s"], i["s*"]] = H[i["s*"], i["s"]] = V["ss*"]          # homonuclear: s s* = s* s
    for s_ in ("s", "s*"):
        vsp = V["sp"] if s_ == "s" else V["s*p"]
        vsd = V["sd"] if s_ == "s" else V["s*d"]
        H[i[s_], i["z"]] = vsp; H[i["z"], i[s_]] = -vsp           # odd parity
        H[i[s_], i["z2"]] = H[i["z2"], i[s_]] = vsd               # even parity
    H[i["z"], i["z"]] = V["ppS"]; H[i["x"], i["x"]] = H[i["y"], i["y"]] = V["ppP"]
    H[i["z"], i["z2"]] = V["pdS"]; H[i["z2"], i["z"]] = -V["pdS"]
    for p_, d_ in (("x", "zx"), ("y", "yz")):
        H[i[p_], i[d_]] = V["pdP"]; H[i[d_], i[p_]] = -V["pdP"]
    H[i["z2"], i["z2"]] = V["ddS"]
    H[i["zx"], i["zx"]] = H[i["yz"], i["yz"]] = V["ddP"]
    H[i["xy"], i["xy"]] = H[i["x2y2"], i["x2y2"]] = V["ddD"]
    return H


def _basis_rotation(R):
    """C with bond-frame orbitals psi_a = sum_b C[a, b] phi_b (lab orbitals)."""
    C = np.zeros((10, 10))
    C[0, 0] = C[9, 9] = 1.0
    C[1:4, 1:4] = R.T
    for a, na in enumerate(DNAMES):
        Qr = R @ DQ[na] @ R.T
        for b, nb in enumerate(DNAMES):
            C[4 + a, 4 + b] = np.trace(Qr @ DQ[nb])
    return C


def _hop(d, scale):
    V = {k: v * scale for k, v in TB.items() if k not in ("Es", "Ep", "Es*", "Ed", "delta")}
    C = _basis_rotation(_rotation_to(d))
    return C.T @ _bond_frame(V) @ C


def _soc_block(delta):
    """delta * L.sigma on the p orbitals, basis (orbital x spin) with spin fastest."""
    Lx = np.array([[0, 0, 0], [0, 0, -1j], [0, 1j, 0]])
    Ly = np.array([[0, 0, 1j], [0, 0, 0], [-1j, 0, 0]])
    Lz = np.array([[0, -1j, 0], [1j, 0, 0], [0, 0, 0]])
    sx = np.array([[0, 1], [1, 0]]); sy = np.array([[0, -1j], [1j, 0]]); sz = np.array([[1, 0], [0, -1]])
    p = delta * (np.kron(Lx, sx) + np.kron(Ly, sy) + np.kron(Lz, sz))
    H = np.zeros((20, 20), complex)
    H[2:8, 2:8] = p  # orbitals x, y, z are ORB indices 1..3 -> spinor rows 2..7
    return H


def tb_hamiltonian(kvec, strained):
    e = sv.strain_tensor(strained)
    d0 = np.array([[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]]) * sv.A0 / 4
    ds = d0 * (1 + np.array(e))
    onsite = np.diag([TB["Es"]] + [TB["Ep"]] * 3 + [TB["Ed"]] * 5 + [TB["Es*"]])
    hAC = np.zeros((10, 10), complex)
    for dj in ds:
        scale = (np.linalg.norm(d0[0]) / np.linalg.norm(dj)) ** 2
        hAC += np.exp(1j * np.dot(kvec, dj)) * _hop(dj, scale)
    I2 = np.eye(2)
    H = np.zeros((40, 40), complex)
    H[:20, :20] = np.kron(onsite, I2) + _soc_block(TB["delta"])
    H[20:, 20:] = np.kron(onsite, I2) + _soc_block(TB["delta"])
    H[:20, 20:] = np.kron(hAC, I2)
    H[20:, :20] = H[:20, 20:].conj().T
    return H


def run_tb():
    sets = {}
    for name, ks in sv.kpoint_sets().items():
        eig = [np.linalg.eigvalsh(tb_hamiltonian(np.array(k) * TWO_PI_A, name == "strained")).tolist() for k in ks]
        sets[name] = {"kpts": ks, "eig": eig}
    # sanity numbers reported with the result: indirect gap and its position on Gamma-X
    xs = np.linspace(0.70, 1.0, 61)
    cb = [np.linalg.eigvalsh(tb_hamiltonian(np.array([x, 0, 0]) * TWO_PI_A, False))[8] for x in xs]
    vbm = max(sets["unstrained"]["eig"][0][:8])
    gap = min(cb) - vbm
    sv.write_eig("eig.json", "tight binding sp3d5s* (Boykin 2004)", sets, nelec=8,
                 notes=f"nearest-neighbour sp3d5s* + SOC, Si parameters of PRB 69 115201 Table IV; indirect gap "
                       f"{gap:.3f} eV at {xs[int(np.argmin(cb))]:.2f} Gamma-X (paper: 1.131 eV at 0.813); strain: "
                       f"Harrison (d0/d)^2 scaling of two-centre integrals only")


# ============================================================================ empirical pseudopotential

CB66 = {3: -0.21, 8: 0.04, 11: 0.08}   # Ry, symmetric form factors at |G|^2 (2pi/a)^2
A_CB = 5.43


def _vq_spline():
    """Cubic spline of V(q^2) through V(0) = -2/3 E_F and the CB66 form factors; 0 from q^2 = 13."""
    from scipy.interpolate import CubicSpline
    n = 32 / A_CB ** 3                                   # valence electron density (A^-3)
    ef = HB2M * (3 * math.pi ** 2 * n) ** (2 / 3) / sv.RY  # Ry
    x = [0, 3, 8, 11, 13, 16]
    y = [-2 / 3 * ef, CB66[3], CB66[8], CB66[11], 0.0, 0.0]
    return CubicSpline(x, y, bc_type="clamped")


def _gvecs(cut2=21):
    r = range(-5, 6)
    g = [np.array([h, k, l]) for h in r for k in r for l in r]
    # fcc reciprocal lattice in 2pi/a units: all-even or all-odd integer triples
    g = [v for v in g if (v % 2 == 0).all() or (v % 2 == 1).all()]
    return np.array([v for v in g if v @ v <= cut2], float)


def epm_hamiltonian(kvec, strained, mu, spline, G0):
    """kvec in 2pi/a0 units. Returns H in Ry (plane waves x spin)."""
    e = np.array(sv.strain_tensor(strained))
    G = G0 / (1 + e)                       # reciprocal vectors of the strained cell (2pi/a0 units)
    K = G + kvec
    n = len(G)
    scale_q = (2 * math.pi / sv.A0) ** 2 / (2 * math.pi / A_CB) ** 2  # q^2 in CB66's (2pi/a) units
    vol = np.prod(1 + e)
    tau = np.array([0.125, 0.125, 0.125]) * (1 + e)  # half-bond, in a0 units
    dG = G[:, None, :] - G[None, :, :]
    q2 = np.sum(dG ** 2, axis=2) * scale_q
    V = np.where(q2 < 13, spline(np.clip(q2, 0, 16)), 0.0) / vol
    S = np.cos(2 * math.pi * dG @ tau)
    H0 = V * S
    np.fill_diagonal(H0, np.sum(K ** 2, axis=1) * (2 * math.pi / sv.A0) ** 2 * HB2M / sv.RY)
    # spin-orbit: -i mu (K_i x K_j) . sigma S(G_i - G_j)
    cr = np.cross(K[:, None, :], K[None, :, :])
    sig = [np.array([[0, 1], [1, 0]]), np.array([[0, -1j], [1j, 0]]), np.array([[1, 0], [0, -1]])]
    H = np.kron(H0, np.eye(2)).astype(complex)
    for c in range(3):
        H += np.kron(-1j * mu * cr[:, :, c] * S, sig[c])
    return H


def _epm_eigs(k, strained, mu, spline, G0):
    return np.linalg.eigvalsh(epm_hamiltonian(np.array(k, float), strained, mu, spline, G0)) * sv.RY


def run_epm(target_dso=0.0441):
    spline, G0 = _vq_spline(), _gvecs()
    def dso(mu):
        e = sorted(_epm_eigs([0, 0, 0], False, mu, spline, G0))[:8]
        return e[7] - e[2]
    lo, hi = 0.0, 0.01
    for _ in range(60):  # bisection on mu (Ry)
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if dso(mid) < target_dso else (lo, mid)
    mu = (lo + hi) / 2
    sets = {}
    for name, ks in sv.kpoint_sets().items():
        sets[name] = {"kpts": ks, "eig": [_epm_eigs(k, name == "strained", mu, spline, G0).tolist() for k in ks]}
    sv.write_eig("eig.json", "empirical pseudopotential (Cohen-Bergstresser 1966)", sets, nelec=8,
                 notes=f"local EPM, {len(G0)} plane waves, spin-orbit mu = {mu * 1000:.4f} mRy fitted to "
                       f"Delta_so = {target_dso * 1000:.1f} meV (Delta_so is an input here, not a prediction)")


# ============================================================================ 6-band k.p (Luttinger-Kohn + Bir-Pikus)

KP = {"g1": 4.285, "g2": 0.339, "g3": 1.446, "dso": 0.0441, "b": -2.10, "d": -4.85, "av": 2.38}


def kp_hamiltonian(k, strain):
    """6x6 in (x, y, z) p-like orbitals x spin; energies of electrons in eV (valence bands negative)."""
    g1, g2, g3 = KP["g1"], KP["g2"], KP["g3"]
    L, M, N = -(g1 + 4 * g2) * HB2M, -(g1 - 2 * g2) * HB2M, -6 * g3 * HB2M
    kx, ky, kz = k
    H = np.array([[L * kx * kx + M * (ky * ky + kz * kz), N * kx * ky, N * kx * kz],
                  [N * kx * ky, L * ky * ky + M * (kx * kx + kz * kz), N * ky * kz],
                  [N * kx * kz, N * ky * kz, L * kz * kz + M * (kx * kx + ky * ky)]])
    exx, eyy, ezz = strain
    l, m = KP["av"] + 2 * KP["b"], KP["av"] - KP["b"]
    H = H + np.diag([l * exx + m * (eyy + ezz), l * eyy + m * (exx + ezz), l * ezz + m * (exx + eyy)])
    Lx = np.array([[0, 0, 0], [0, 0, -1j], [0, 1j, 0]])
    Ly = np.array([[0, 0, 1j], [0, 0, 0], [-1j, 0, 0]])
    Lz = np.array([[0, -1j, 0], [1j, 0, 0], [0, 0, 0]])
    sx = np.array([[0, 1], [1, 0]]); sy = np.array([[0, -1j], [1j, 0]]); sz = np.array([[1, 0], [0, -1]])
    so = KP["dso"] / 3 * (np.kron(Lx, sx) + np.kron(Ly, sy) + np.kron(Lz, sz))
    return np.kron(H, np.eye(2)) + so


def run_kp():
    sets = {}
    for name, ks in sv.kpoint_sets().items():
        st = sv.strain_tensor(name == "strained")
        sets[name] = {"kpts": ks, "eig": [np.linalg.eigvalsh(kp_hamiltonian(np.array(k) * TWO_PI_A, st)).tolist()
                                          for k in ks]}
    sv.write_eig("eig.json", "k.p 6-band (experimental parameters)", sets, nelec=6,
                 notes="Luttinger-Kohn + Bir-Pikus; gamma1,2,3 = 4.285, 0.339, 1.446; Delta_so = 44.1 meV; "
                       "b = -2.10 eV, d = -4.85 eV (shear deformation potentials; d enters only for shear strain)")


if __name__ == "__main__":
    {"tb": run_tb, "epm": run_epm, "kp": run_kp}[sys.argv[1]]()
    print(json.dumps(sv.analyse(json.load(open("eig.json"))), indent=1))
