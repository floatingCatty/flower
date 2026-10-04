"""Exact diagonalization of the periodic spin-1/2 J1-J2 chain, H = sum_x S_x.S_{x+1} + J2 S_x.S_{x+2} (J1 = 1), in
QuSpin's symmetry blocks: total Sz, momentum k = 0 or pi, site parity p, spin inversion z.

  python3 ed.py sectors L     every (k, p, z) block of Sz = 0 at a few J2: which block holds the ground state, the
                              lowest excited singlet and the lowest triplet (identified by <S^2>)
  python3 ed.py run L         with the block rule from `sectors` (FLOWER_INPUTS: {"rule": {...}}): ground-state
                              energy and <Sz(0) Sz(L/2)> at the paper's J2 values, and the singlet and triplet
                              excitation energies on a J2 grid around J2crit
Prints the outputs JSON (last line); `run` writes result.json and checkpoints each solve in $FLOWER_STATE_DIR.
"""
import gc
import json
import os
import sys
import time

import numpy as np
from scipy.sparse.linalg import LinearOperator, eigsh
from quspin.basis import spin_basis_1d
from quspin.operators import hamiltonian

CORR_J2 = [-0.25, -0.1, 0.0, 0.05, 0.1, 0.15, 0.2, 0.241167, 0.5]     # Tables I and II, J2crit, Majumdar-Ghosh
LEVEL_J2 = [0.20, 0.22, 0.23, 0.235, 0.238, 0.240, 0.241, 0.2415, 0.242, 0.244, 0.247, 0.25, 0.26, 0.28]
SCAN_J2 = [0.0, 0.24, 0.4]
OPTS = dict(dtype=np.float64, check_symm=False, check_herm=False, check_pcon=False)


def basis(L, k, p, z):
    return spin_basis_1d(L, pauli=False, Nup=L // 2, kblock=0 if k == 0 else L // 2, pblock=p, zblock=z)


def bond_ops(L, d, scale=1.0):
    zz = [[scale, i, (i + d) % L] for i in range(L)]
    pm = [[0.5 * scale, i, (i + d) % L] for i in range(L)]
    return [["zz", zz], ["+-", pm], ["-+", pm]]


def pieces(b, L):
    return hamiltonian(bond_ops(L, 1), [], basis=b, **OPTS), hamiltonian(bond_ops(L, 2), [], basis=b, **OPTS)


def lowest(h1, h2, j2, nev=1, v0=None):
    """The nev lowest eigenpairs of h1 + j2 h2 (dense below 400 states, else Lanczos with matvecs only)."""
    n = h1.Ns
    if n <= 400:
        e, v = np.linalg.eigh(h1.toarray() + j2 * h2.toarray())
        return e[:nev], v[:, :nev]
    op = LinearOperator((n, n), matvec=lambda x: h1.dot(x) + j2 * h2.dot(x), dtype=np.float64)
    e, v = eigsh(op, k=nev, which="SA", tol=1e-13, v0=v0, ncv=max(20, 2 * nev + 1), maxiter=20000)
    o = np.argsort(e)
    return e[o], v[:, o]


def spin_of(b, L, v):
    """S from <S^2> = 3L/4 + sum over ordered pairs i != j of <S_i . S_j>."""
    ops = []
    for d in range(1, L):
        ops += bond_ops(L, d)
    s2 = hamiltonian(ops, [], basis=b, **OPTS)
    x = 3 * L / 4 + float(s2.expt_value(v).real)
    s = (-1 + np.sqrt(1 + 4 * x)) / 2
    if abs(s - round(s)) > 1e-6:
        raise SystemExit("not an S^2 eigenstate: <S^2> = %r" % x)
    return int(round(s))


def sectors(L):
    found = {}
    for j2 in SCAN_J2:
        states = []
        for k in (0, "pi"):
            for p in (1, -1):
                for z in (1, -1):
                    b = basis(L, k, p, z)
                    if b.Ns == 0:
                        continue
                    h1, h2 = pieces(b, L)
                    e, v = lowest(h1, h2, j2, nev=min(2, b.Ns))
                    for n in range(len(e)):
                        states.append((float(e[n]), spin_of(b, L, v[:, n]), [k, p, z, n]))
        states.sort(key=lambda s: s[0])
        gs = states[0]
        singlet = next(s for s in states[1:] if s[1] == 0)
        triplet = next(s for s in states if s[1] == 1)
        if gs[1] != 0:
            raise SystemExit("ground state is not a singlet at J2=%g" % j2)
        for e, s, (k, p, z, n) in states:      # spin inversion in Sz = 0 is (-1)^(S + L/2)
            if z != (-1) ** (s + L // 2):
                raise SystemExit("z = %d for S = %d at L = %d" % (z, s, L))
        here = {"gs": gs[2], "singlet": singlet[2], "triplet": triplet[2]}
        if found and here != found:
            raise SystemExit("the blocks change with J2: %s vs %s" % (found, here))
        found = here
        found_e = {"E0": gs[0], "E_singlet": singlet[0], "E_triplet": triplet[0]}
    return dict(L=L, **found, at_j2=SCAN_J2[-1], **found_e)


class Checkpoint:
    def __init__(self, L):
        d = os.environ.get("FLOWER_STATE_DIR") or "."
        self.path = os.path.join(d, "ed-L%d.json" % L)
        self.data = json.load(open(self.path)) if os.path.exists(self.path) else {}
        self.resumed = bool(self.data)

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value
        tmp = self.path + ".tmp"
        json.dump(self.data, open(tmp, "w"))
        os.replace(tmp, self.path)


def run(L, rule):
    t0 = time.time()
    r = rule[str(L % 4)]
    ck = Checkpoint(L)
    out = {"L": L, "rule": r, "dims": {}, "corr": [], "levels": []}

    k, p, z, n = r["gs"]
    b = basis(L, k, p, z)
    out["dims"]["gs"] = int(b.Ns)
    h1, h2 = pieces(b, L)
    corr_op = hamiltonian([["zz", [[1.0 / L, i, (i + L // 2) % L] for i in range(L)]]], [], basis=b, **OPTS)
    v0 = None
    for j2 in CORR_J2:
        key = "gs %r" % j2
        if ck.get(key) is None:
            e, v = lowest(h1, h2, j2, nev=n + 1, v0=v0)
            v0 = v[:, 0]
            ck.put(key, {"J2": j2, "E0": float(e[n]), "SzSz": float(corr_op.expt_value(v[:, n]).real)})
        out["corr"].append(ck.get(key))
    mg = ck.get("gs 0.5")["E0"]
    if abs(mg + 3 * L / 8) > 1e-8:
        raise SystemExit("Majumdar-Ghosh check failed: E0(J2=1/2) = %r, exact %r" % (mg, -3 * L / 8))
    del h1, h2, corr_op, v0
    gc.collect()

    levels = {}
    for which in ("singlet", "triplet"):
        k, p, z, n = r[which]
        b = basis(L, k, p, z)
        out["dims"][which] = int(b.Ns)
        h1 = h2 = v0 = None
        for j2 in LEVEL_J2:
            key = "%s %r" % (which, j2)
            if ck.get(key) is None:
                if h1 is None:
                    h1, h2 = pieces(b, L)
                e, v = lowest(h1, h2, j2, nev=n + 1, v0=v0)
                v0 = v[:, 0]
                ck.put(key, float(e[n]))
            levels.setdefault(j2, {})[which] = ck.get(key)
        del h1, h2, v0
        gc.collect()
    out["levels"] = [{"J2": j2, "E_singlet": x["singlet"], "E_triplet": x["triplet"],
                      "delta": x["singlet"] - x["triplet"]} for j2, x in sorted(levels.items())]
    out["seconds"] = round(time.time() - t0, 1)
    json.dump(out, open("result.json", "w"), indent=1)
    e0 = next(c["E0"] for c in out["corr"] if c["J2"] == 0.0)
    return {"L": L, "dim_gs": out["dims"]["gs"], "E0_per_site_J2_0": e0 / L, "resumed": ck.resumed,
            "seconds": out["seconds"]}


if __name__ == "__main__":
    mode, L = sys.argv[1], int(sys.argv[2])
    if L % 2:
        raise SystemExit("L must be even")
    if mode == "sectors":
        res = sectors(L)
    else:
        rule = json.load(open(os.environ["FLOWER_INPUTS"]))["rule"]
        res = run(L, rule)
    print(json.dumps(res))
