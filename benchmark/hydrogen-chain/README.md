# Benchmark: hydrogen chains in the minimal basis (Simons Collaboration benchmark)

**Paper:** M. Motta *et al.*, "Towards the Solution of the Many-Electron Problem in Real Materials: Equation of
State of the Hydrogen Chain with State-of-the-Art Many-Body Methods", Phys. Rev. X 7, 031059 (2017),
[arXiv:1705.01608](https://arxiv.org/abs/1705.01608). In the STO-6G basis the paper gives deterministic
reference energies: Table II, the H10 potential energy curve with seven methods (FCI, RHF, UHF, RCCSD, RCCSD(T),
UCCSD, UCCSD(T)) at ten bond lengths, to 6 decimals; the exact equilibrium bond length and energy; the
dissociation limit; and Table V, the infinite chain (UHF and coupled cluster), to 5 decimals.

**Method here:** PySCF (the frozen `envs/pyscf` recipe) on this machine ([`hchain.py`](hchain.py)): open linear
chains, STO-6G, the lowest UHF found from several broken-symmetry guesses, then RCCSD/(T) and UCCSD/(T). The
paper's tables are parsed from its text layer ([`tables.py`](tables.py)). [`analysis.py`](analysis.py) compares
Table II value by value, fits R_e and E_0 with a quartic to FCI on R = 1.70…1.90, and extrapolates
E(N)/N = e + a/N + b/N² to N → ∞ for Table V from N = 10…50, and up to N = 100 for R ≤ 2.0.

## Result (run `hydrogen-chain-20261004-203454-6173`): 5 of 6 claims reproduced

| claim | paper | this run | |
|---|---|---|---|
| Table II: H10, 7 methods × 10 bond lengths | 66 values (RCC at R ≥ 3.2 apart) | **66/66 to the printed digits** (worst 9·10⁻⁷) | ✓ |
| Restricted CC breaks down at R ≥ 3.2 | RCCSD(T) −0.593, −0.668 vs FCI −0.491, −0.482 | errors vs FCI ≤ 3·10⁻⁴ up to R = 2.4, ≥ 7·10⁻³ at R ≥ 3.2 | ✓ |
| exact R_e (bohr) | 1.786 | **1.7859** | ✓ |
| exact E_0 per atom (hartree) | −0.542457 | **−0.542457** | ✓ |
| dissociation limit per atom | −0.471039 | **−0.471039** | ✓ |
| Table V: the infinite chain | 42 values (5 decimals) | 28/42 within 3·10⁻⁵ (worst 4·10⁻⁴) | ✗ |

**Restricted CC at large R.** Where restricted coupled cluster breaks down (R ≥ 3.2) its equations have several
solutions. PySCF lands on a different one from the paper's: the breakdown is reproduced, the particular wrong
energies are not, and the analysis counts the breakdown itself as the claim.

**Table V: the misses are all unrestricted methods at R ≤ 1.8.** RCCSD and RCCSD(T) match at every bond length
where the paper gives them, and UHF, UCCSD and UCCSD(T) match at R ≥ 2.0. The 14 misses are UHF, UCCSD and
UCCSD(T) at R = 1.0–1.8. The reason is visible in the chains: at short bond lengths the UHF solution breaks spin
symmetry only in long chains. ⟨S²⟩ of the lowest UHF solution is zero up to N = 50 at R = 1.0 (broken only at
N = 60, 80, 100) and up to N = 30 at R = 1.2, while at R = 2.0 every length from N = 10 is broken. Mixing the
two branches in one fit was wrong (an earlier version of the analysis did, and missed badly), so the fit uses
only the chains on the broken-symmetry branch; at R = 1.0 that is three chains. The extrapolated
infinite-chain value there therefore depends on few, long chains, and differs from the paper's by up to
2·10⁻⁴ hartree. The paper's own procedure for these points (which chain lengths, which UHF solution) would be
needed to go further.

## How it ran (one flower run, started with `flower start`)

`paper` (PDF, sha256, Tables II and V parsed) → `h10` (10 bond lengths, all methods) → `chains` (58 items: N = 20…50
at all ten bond lengths, and N = 60, 80, 100 for R ≤ 2.0) → `fci-fine` (FCI on R = 1.70…1.90 for R_e
and E_0) → `h-atom` (one H atom: the dissociation limit) → `analysis`.

What the run found in flower and in the work:
- **BUGS #58.** Two `flower sync` calls each proposed the same edit of the `chains` foreach; the first waited for
  approval, the second was applied, and the leftover proposal parked the finished run. Each sync now withdraws
  older proposals made from the plan file.
- **Scratch space.** Six long chains (N = 80 and 100) failed because PySCF's UCCSD scratch files filled the root
  disk's `/tmp`. `tmpdir: job` on the step moved the scratch into the attempt directory on the large NFS disk,
  and `flower rerun RUN chains[i] --only` re-ran just those items.
- **Branches.** The first Table V fits mixed symmetric and broken-symmetry UHF chains; N = 60…100 chains were
  appended to the running foreach (without re-running the others) to fit on one branch.
- A numpy `bool` in the analysis JSON failed `json.dump` (the default handler now converts numpy scalars).

## Files
- [`tables.py`](tables.py): Tables II and V from the paper's text layer.
- [`hchain.py`](hchain.py): one chain (N, R): RHF, lowest UHF, RCCSD/(T), UCCSD/(T), and FCI when asked.
- [`analysis.py`](analysis.py): the claims, `report.md` and `hchain.png`.
- [`plan.yaml`](plan.yaml): written by `flower add`, plus hand edits to the chain list.
