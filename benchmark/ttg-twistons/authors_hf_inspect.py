"""Inspect the intermediate data of the authors' Hartree-Fock run (in its job directory): their non-interacting
active-band energies, their final HF Hamiltonian, and how big the self-energy is. Used to locate why our own
implementation of the same scheme gives a much larger splitting.

python3 authors_hf_inspect.py JOB_DIR [INDEX]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def main(d: str, idx: int = 96) -> dict:
    d = Path(d)
    e0 = np.load(d / f"HtotalEigValsrepo_{idx}.npz")["arr_0"]          # (npts, 16) kinetic (active bands)
    H = np.load(d / f"eps10noPHtest2HtotalLowerBandsrepo_{idx}.npy")    # (npts, 16, 16), mu subtracted
    V = np.load(d / f"HtotalEigVecsrepo_{idx}.npz")["arr_0"]            # (npts, basis, 16)
    out = {"shapes": {"e0": list(e0.shape), "H": list(H.shape), "vecs": list(V.shape)}}
    e0 = np.real(e0)
    out["sp_columns_min_max_meV"] = [[float(e0[:, c].min()), float(e0[:, c].max())] for c in range(e0.shape[1])]
    eh = np.array([np.sort(np.real(np.linalg.eigvals(h))) for h in H])
    out["hf_sorted_min_max_meV"] = [[float(eh[:, c].min()), float(eh[:, c].max())] for c in range(eh.shape[1])]
    kin = np.array([np.diag(x) for x in e0])
    S = H - kin
    mu = float(np.mean([np.trace(s) for s in S]) / H.shape[1])
    out["self_energy_trace_mean_meV"] = mu
    out["self_energy_offdiag_rms_meV"] = float(np.sqrt(np.mean(np.abs(S - np.einsum("kii->ki", S)[..., None]
                                                                       * np.eye(S.shape[1])) ** 2)))
    out["self_energy_diag_by_column_meV"] = [float(np.mean(np.real(S[:, c, c]))) for c in range(S.shape[1])]
    out["hermitian_err"] = float(np.max(np.abs(H - np.conj(np.transpose(H, (0, 2, 1))))))
    out["column_norms_of_vectors"] = [float(x) for x in np.linalg.norm(V[0], axis=0)[:16]]
    return out


if __name__ == "__main__":
    res = main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 96)
    json.dump(res, open("inspect.json", "w"), indent=1)
    print(json.dumps({"n_k": res["shapes"]["e0"][0], "self_energy_trace_mean_meV": res["self_energy_trace_mean_meV"]}))
