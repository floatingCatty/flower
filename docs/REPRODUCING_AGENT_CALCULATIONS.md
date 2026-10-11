# Reproducing a scientific-agent calculation from a fresh clone

This is a concrete example of what **reproducible agent-run scientific computing** means
in flower. It follows a published, versioned protocol rather than a live agent session.
You can inspect the commands, environments, reported outputs, and failed checks before
you trust the result.

The study reproduces numerical results for the frustrated spin-1/2 J1–J2 Heisenberg
chain from [Eggert, *Physical Review B* **54**, R9612 (1996)](https://arxiv.org/abs/cond-mat/9602026).
The complete source is in [`benchmark/j1j2-chain/`](../benchmark/j1j2-chain/README.md).

## What is actually in the repository?

| Artifact | Purpose |
| --- | --- |
| [`protocol.yaml`](../benchmark/j1j2-chain/protocol.yaml) | Exported dependency graph and commands needed for the final comparison |
| [`expected.json`](../benchmark/j1j2-chain/expected.json) | Expected output values from the recorded study |
| [`PROTOCOL.md`](../benchmark/j1j2-chain/PROTOCOL.md) | Machine inputs, step descriptions, and run requirements |
| [`paper.py`](../benchmark/j1j2-chain/paper.py) | Source-paper acquisition and extraction of Table I |
| [`ed.py`](../benchmark/j1j2-chain/ed.py) | QuSpin exact diagonalization and symmetry-sector calculations |
| [`analysis.py`](../benchmark/j1j2-chain/analysis.py) | Numerical comparisons with the published claims |
| [`benchmark/envs/quspin/`](../benchmark/envs/quspin/) | Frozen environment recipe used by the remote computation |
| [`benchmark/envs/analysis/`](../benchmark/envs/analysis/) | Analysis environment recipe |

No result below is newly computed for this documentation page. Numbers refer to the
[recorded benchmark](../benchmark/j1j2-chain/README.md).

## Before running

You need Python 3.9 or newer on the machine controlling flower, network access to
fetch the original paper, and **an SSH-accessible Linux workstation with enough
CPU and memory for the large symmetry-sector diagonalizations**. This is not a
one-minute laptop example: the recorded L=32 calculation includes a block with
about 4.7 million states and used eight CPU cores. The benchmark ran for roughly
one hour; runtime will depend on hardware and queue contention.

Install flower from GitHub, **not** by running `pip install flower`
(that would install the unrelated Celery Flower package):

```bash
python -m pip install "git+https://github.com/floatingCatty/flower"
git clone https://github.com/floatingCatty/flower.git
cd flower/benchmark
```

Make sure the workstation is accessible via a working SSH alias (for example,
`ssh my-lab-box`). The exported protocol uses the `remote` cluster over SSH,
not Slurm; Slurm support is a separate flower feature.

Create `my-machines.json` **locally** in `benchmark/`, substituting your own SSH
alias. The file is only an example of the input schema; it contains no credentials.

```json
{
  "host": "my-lab-box",
  "ssh_options": []
}
```

If you need a non-default SSH config, use
`"ssh_options": ["-F", "/path/to/ssh_config"]`. Your credentials must
already work with SSH. Don't commit local machine-specific inputs or secrets.

## Re-run the protocol and compare

From `flower/benchmark/`:

```bash
flower run j1j2-chain/protocol.yaml --follow --inputs my-machines.json
flower status RUN_ID
flower compare RUN_ID j1j2-chain/expected.json
```

Replace `RUN_ID` with the run identifier printed by the first command.
The protocol stages the required scripts, creates or checks the frozen
`quspin` and `analysis` environments, runs the dependency graph, and checks
the physical claims in the final analysis.

A successful *recorded* execution produced:

- Critical coupling `J2crit = 0.241167`, compared with the paper's
  `0.241167 ± 0.000005`.
- `n_reproduced = 6` of `n_claims = 6`, as evaluated by the project's
  claim checks.
- A fresh-clone protocol run for which `flower compare` found all 24
  exported expected values matching within a relative tolerance of `1e-6`.

These statements describe the recorded benchmark. **Your reproduction is only
successful if your own run completes and its comparison passes.** Don't edit
`expected.json` to make a failed comparison pass.

## What to look at when it fails

Run `flower status RUN_ID`, then inspect the failed step with
`flower show RUN_ID STEP --logs`. Common questions are whether SSH still works,
the remote environment can be installed, resources are sufficient, the paper can
be downloaded, or the comparison tolerance is justified. If one computation is
re-run after changing code or inputs, inspect the recorded plan change and its
downstream results.

A numerical match alone is not proof of scientific correctness. In this study,
the authors of the original paper did not specify every fitting convention used
for Table II. The benchmark explicitly scans 144 candidate fitting choices,
selecting one that reproduces the original table before applying it to its own
computed correlations. That decision and its sensitivity are documented in the
[benchmark report](../benchmark/j1j2-chain/README.md); it should **not** be
described as a completely blind independent inference.

## Hand this to an AI coding agent

An agent can work with the repository, but it should follow the same acceptance
criteria as a person:

> Read `benchmark/j1j2-chain/PROTOCOL.md` and `protocol.yaml`. Verify SSH
> access and the two frozen environment recipes. Run the protocol from
> `benchmark/` using my local `my-machines.json`. Record the run ID,
> `flower status`, the final `analysis` result, and `flower compare`
> against the repository's `expected.json`. If any step fails or the
> comparison differs, report the exact mismatch and relevant logs. Do not
> rewrite `expected.json`, suppress a failing check, or claim the study
> reproduced successfully without its numerical comparison.

This is the boundary flower tries to preserve: a task executed by an agent
becomes a sequence a second person can inspect and re-run. It does not
automatically validate the scientific interpretation of the results.

For the general tool model and other examples, see
[Using flower](USING.md), the [command reference](REFERENCE.md), and the
[full benchmark collection](../benchmark/README.md). A short
[English/Chinese research note](https://floatingcatty.github.io/research/flower/)
explains the motivation.
