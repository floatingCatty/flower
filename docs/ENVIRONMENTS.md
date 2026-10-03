# Environments on execution targets

**Problem.** A node may run on this machine, on a remote host over ssh (`scheduler: none`), on a Slurm
cluster, or later on Kubernetes. Wherever it runs, its software must be there and work, and the result
must be repeatable.

**The choice.** flower does **not** encode how software gets installed: no conda, container or module
logic in flower. That is the agent's job, and it improves as agents improve. flower provides a few
command-line primitives and **one guarantee: the environment recipe that worked is frozen (content-hashed
and versioned) and replayed the same way**, and the steps that need it run with it activated.

## How it works

```
agent explores ──► writes envs/<name>/ ──► flower env freeze ──► flower env replay --fresh ──► plans use it
(flower remote     setup.sh                 (hash, history,        (proof: works from          (environment: <name>)
 exec, logged)     activate.sh              draft setup.sh from     scratch, recorded)
                   check.sh  (+ any files)   the logged commands)
```

### The recipe: `envs/<name>/`, next to the plan (or in a parent directory)

| File | Written by | Contract |
|---|---|---|
| `setup.sh` | agent (or drafted by `freeze` from the logged exploration) | installs into `$FLOWER_ENV_PREFIX`, which does not exist yet; runs with `bash -e` in a copy of the recipe (`$FLOWER_ENV_DIR`), non-interactive |
| `activate.sh` | agent | sourced before every step that uses the environment; makes the tools available |
| `check.sh` | agent | exits 0 if and only if the environment works; prints the versions relied on (recorded) |
| any other file | agent | lock files, patches, data the setup needs; **all hashed into the recipe** |
| `env.yaml` | agent (optional) | `description`, `setup_timeout` |
| `FROZEN.json` | flower | recipe hash, per-file sha256, who/when, previous version, replays (bookkeeping, not hashed) |
| `sessions/*.jsonl` | flower | every `flower remote exec --env <name>` call: command, exit code, output tail. This records how the recipe was found |
| `history/<hash>/` | flower | the files of every frozen version |

On a target, version `<hash>` installs into `$HOME/.flower/envs/<name>-<hash12>`, with the recipe copied
beside it in `…/<name>-<hash12>.recipe`. **A changed recipe gets a new prefix**, so an environment that
results were produced with is never modified.

### The commands (all of flower's environment logic)

| Command | What it does |
|---|---|
| `flower env new <name>` | creates `envs/<name>/` with commented template scripts |
| `flower remote exec --plan P --cluster C --env <name> [--probe] -- <cmd>` | runs a command on the cluster defined by plan P, with the cluster's prelude, `FLOWER_ENV_PREFIX` set to an exploration prefix, and `activate.sh` sourced once it exists. Logged to `sessions/`. `--probe` marks look-only commands, which are not drafted into `setup.sh` |
| `flower env freeze <name>` | pins the recipe: hashes every file, snapshots it in `history/`, and drafts `setup.sh` from the successful non-probe commands if the agent didn't write one. Refuses template scripts |
| `flower env replay <name> --plan P --cluster C [--fresh]` | uploads the frozen recipe and runs `check.sh`; if that fails, `setup.sh` then `check.sh`. `--fresh` installs into a new empty prefix: **proof that the recipe works from scratch**. Recorded in `FROZEN.json` |
| `flower env check <name> --plan P --cluster C` | `check.sh` only |
| `flower env show [name]` | state (draft / frozen / changed), hash, logged commands, recent replays |

### In a plan

```yaml
clusters:
  box: {transport: ssh, host: myworkstation, scheduler: none}   # `install: never` = check only, never setup
nodes:
  - {id: scf, kind: shell, cluster: box, environment: abacus, run: "mpirun -np 8 abacus > out"}
```

A `job`, or a `shell` / `function` step on a cluster, may name an `environment`. When a plan is
normalised, flower adds **one generated step per (environment, cluster)**, for example
`env-abacus-box`. This step:
- appears in `flower plan show`, so it is part of what you approve;
- runs `check.sh`, and if that fails `setup.sh` then `check.sh`;
- outputs `how` (`present` or `installed`), `prefix` and `recipe`, and keeps `check.log` as a file;
- is never served from cache, so a deleted environment is noticed.

Steps that use the environment depend on it, and get its activation (`FLOWER_ENV_PREFIX`,
`FLOWER_ENV_DIR`, `source activate.sh`) prepended to their prelude. That prelude is part of their
definition, so **a new recipe version changes their cache key**: results are never reused across
environment versions.

Validation refuses a plan whose recipe is missing, never frozen, or edited since it was frozen. The hint
names the command to run.

### The agent's loop: no restrictions until it freezes

The agent explores however it likes:
- reads the machine (`--probe`);
- tries installs;
- writes and rewrites the scripts;
- runs `check.sh` through `remote exec`, which sources `activate.sh`.

None of that is constrained. The only fixed points are:
1. **freeze**, when it decides the recipe is ready;
2. **replay `--fresh`**, recommended, which shows the recipe works without the exploration's leftovers.

Whether the result is bit-identical depends on what the agent pins: an explicit conda list with
checksums, an image digest, exact versions. A better agent writes a more reproducible recipe. flower's
part is that the same frozen recipe is always run the same way, into its own prefix, and checked.

## Tested on a real remote machine (2026-10-03)

This was done for the ABACUS benchmark, on a 32-core workstation reached over ssh with no batch system
(`benchmark/envs/abacus/`):
1. **Exploration:** 5 logged commands found conda on the host and an earlier hand-made env, and
   captured its exact package list (`conda list --explicit --md5`, 38 package files).
2. **The recipe:** `setup.sh` finds conda, mamba or micromamba, then runs
   `create -p $FLOWER_ENV_PREFIX --file conda-explicit.txt`. `activate.sh` puts the prefix's `bin`
   first on PATH. `check.sh` checks `abacus --version` (v3.9.0), that `mpirun` comes from the prefix,
   `mpirun -np 2`, and `python3`.
3. **Freeze, then `flower env replay --fresh`:** a new empty prefix was installed and checked in 4 s,
   thanks to the warm package cache.
4. **The benchmark** (`plan-remote.yaml`, steps with `environment: abacus`) ran without any
   hand-written prelude. The generated `env-abacus-remote` step installed the recipe's own prefix in
   10 s. The whole run succeeded in 47 s, with the same results as before.

## Why so little

The design proposal before this one (a typed recipe model with conda/container/modules realisations, a
provisioning engine, fingerprints and install policies) was dropped on purpose. Each of those pieces
would encode today's best practice in flower and age badly. What stays fixed is small:
- a recipe format of three scripts plus any files;
- a hash and a version history;
- a replay procedure;
- one generated step per environment and cluster.

Everything that needs judgement belongs to the agent: which package manager, which build, how to pin,
how to debug a broken MPI.

Possible later additions, none of which changes the format:
- **Kubernetes:** a recipe can choose an image as its "setup" (`setup.sh` pulls a digest, `activate.sh`
  wraps the command). A k8s transport would honour that.
- **Automatic repair:** when the generated step fails, flower could start an agent step with
  `flower remote exec` and ask for a re-frozen recipe. Today the coordinating agent does this itself
  (explore, freeze, `flower rerun RUN env-<name>-<cluster>`).


## Appendix: what the reference projects do

Surveyed from `context/repos/` and Eleforge (`apps/backend/compute/`).

| Project | How the needed software is described | Who installs it | Check before real work | What is recorded |
|---|---|---|---|---|
| aiida-core | `Code`: `InstalledCode` (path), `PortableCode` (uploaded folder), `ContainerizedCode` (`engine_command` with `{image_name}`). `prepend_text` on computer and code | the user, by hand | `verdi code test` (file exists and is executable); `verdi computer test` (connection only) | link to the Code node (path, image name); PortableCode files hashed. No binary hash, image digest or version |
| jobflow-remote | worker `pre_run`, named `exec_config` (modules, export, pre_run) | the user | `jf project check` compares remote `pip list` with local versions. The only environment parity check found | nothing |
| dpdispatcher | `resources`: `module_list`, `source_list`, `envs`, `prepend_script` | the user | none | nothing |
| dflow | container `image` per step; `DispatcherExecutor` passes dpdispatcher settings through; `python_packages` uploads local source | runtime pulls images (`singularity pull`, docker `IfNotPresent`) | none | image tag in the Argo spec, no digest |
| psij | `JobSpec.environment`, `pre_launch` / `post_launch` scripts | the user | none | nothing |
| snakemake | per rule `conda:` / `container:` / `envmodules:`; `--sdm conda apptainer env-modules`; `--conda-create-envs-only` | **the engine**, conda envs named by a hash of the env file, built once on a shared FS | none | env-file hash (identity of the definition, not measured versions) |
| catgo | job-script templates `{{module_loads}}` / `{{env_setup}}` per engine | the user | **`vasp_preflight`** over ssh: `command -v <binary>` *after the real module loads and conda activation*, plus data-file checks. Exposed as a UI button and as an MCP tool the agent must call before submitting | nothing |
| Eleforge | capability probe (`remote/probe.py`): solvers, Python libraries, container runtimes, GPU, Slurm. Runtime-profile registry with image refs | **the engine**: `node_provision.py` installs Miniforge, an env, the runtime wheel, `conda install <engine>`, assets; or `apptainer pull` | provisioning canary: `import` plus `<binary> --version`, once | declared runtime ref / image ref. No measured version. Placeholder digests (`sha256:1111…`) |
| gh-aw, orc, yak, smithers | runtimes detected from the workflow (gh-aw); setup stamps (orc); version-pinned agent images (yak); a subject fingerprint (smithers `.subject.json`) | CI setup steps / image builds | gh-aw `docker run hello-world`; `doctor` commands | SHA-pinned actions and images; fingerprints |

**Patterns and gaps.**
1. **The environment is almost always text pasted in front of the command:** `prepend_text`, `pre_run`,
   `source_list`, module loads. Only snakemake and Eleforge build anything themselves.
2. **Nobody separates *what a step needs* from *how a particular machine provides it*.** aiida Codes
   are tied to one Computer. Eleforge lists the engines it supports in 4–6 places, according to its
   own design doc. A "same environment on another target" concept does not exist.
3. **Checking on the real target, under the real prelude, is rare.** catgo is the only example, and
   Eleforge learned it the hard way: commit 649ca300 found the conda bin directory missing from the
   non-interactive ssh PATH, so the probe reported "0 solvers". Late, opaque job failures are the
   common outcome (Eleforge c878bc2f).
4. **What gets recorded is what was declared, not what was measured:** names, tags, paths. Nobody
   attaches the actual binary version, binary hash or image digest to a result.
5. **Agents diagnose but never repair.** catgo's agent must run `catgo_validate_config` before
   submitting, and routes "command not found" to the user. smithers' health probes only observe. No
   project turns an agent's successful fix into something repeatable.
6. **Containers are where HPC tools struggle.**
   - aiida runs host MPI around the container.
   - Eleforge uses `--ntasks=1`, runs MPI inside the process, binds the host's GPU build into the
     container, and runs `apptainer exec` without `--cleanenv`, so the host environment leaks in.
   - Pulls are unpinned everywhere except gh-aw.

---

