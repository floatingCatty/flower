# flower design input: dynamic graphs, durable replay, provenance, event schema

Scope: semantics only (not harness adapters or Slurm transport). Sources are local clones under
`flower/context/repos/` (paths below are relative to that directory unless they start with `/`),
plus `/homes/nessa/zhanghao/dev/Eleforge/packages/labflow` and the Claude Code Workflow docs
(`/homes/nessa/zhanghao/dev/Eleforge/context/workflow-design` has no journal material, so the replay
quotes come from the bundled `/workflow-authoring` skill and the prior-art survey notes).

---

## 0. TL;DR recommendations

1. **The amendment model is Smithers plan-store "generations" with jobflow's `Response` verbs as the op
   vocabulary.** A plan only grows, and existing nodes are frozen. Every approved amendment is
   `generation += 1` under a compare-and-swap on the parent digest. `baseDigest` (what the human first
   approved) stays separate from `digest` (the current one). jobflow's `addition` / `detour` / `replace`
   / `stop_children` / `stop_jobflow` become amendment ops. They are *proposed* (by an agent node or the
   outer agent), gated (by a human or a pre-authorised policy), and then appended. This differs from
   jobflow, where the job applies them itself.
2. **Address outputs by reference, `node_id.outputs.<key>[.path]`.** A reference resolves to the
   *latest succeeded revision* of that node (jobflow's `which="last"` and `index`). The resolved content
   hash is frozen into the attempt's `node.started` event, so replay never re-resolves.
3. **Replay rule for coarse nodes uses three contexts, not prefix replay** (see §3.4):
   - *Resume* (same run, same plan): every terminal recorded result is authoritative. An in-flight job
     is re-attached by its Slurm id.
   - *Amend* (same run): growth only. Changing a completed node needs an explicit `supersede` op, which
     marks the downstream cone stale.
   - *Fork or new run*: reuse is keyed by `(decl_hash, input_hash)`, with early cut-off. Agent results
     are reused only when both keys match. Cross-run reuse is opt-in.
4. **Provenance follows the AiiDA split.** *Data provenance* (inputs → attempt → outputs, content
   hashes) and *logical provenance* (which plan generation, which amendment, which agent rationale,
   which human approval caused the node) are recorded separately. Export targets are W3C PROV-O and
   Workflow Run RO-Crate 0.6 (Provenance Run Crate).
5. **The event log keeps LabFlow's envelope** (`eventId, runId, eventType, occurredAtIso, nodeRunId,
   attemptId, actorType, actorId, payload`) and adds `seq`, `idempotencyKey`, `planGeneration`,
   `schemaVersion` and `writerEpoch`. New `plan.*` and `gate.*` families are added. LabFlow's
   `external.*` and `cancellation.*` families are kept as they are.

---

## 1. Dynamic graph growth

### 1.1 jobflow `Response` (materialsproject/jobflow, modified BSD/LBNL)

`jobflow/src/jobflow/core/job.py:1241`:
```
output: T = None
detour: jobflow.Flow | Job | list[Job] | list[jobflow.Flow] = None
addition: ...      replace: ... | dict[Any, Job|Flow] = None
stored_data: dict[Hashable, Any] = None
stop_children: bool = False      stop_jobflow: bool = False
```
- **Implicit replace.** In `Response.from_job_returns`, if a job *returns* a Job, Flow, or a
  list/tuple/dict made entirely of them, the return is "interpret[ed] as a replace". The output schema
  is "only appl[ied] if there is no replace" (job.py:1313-1336).
- **`replace`** (`prepare_replace`, job.py:1413-1481) works as follows:
  - Replacing with a single Job: `replace.set_uuid(current_job.uuid); replace.index = current_job.index + 1`,
    and the job inherits metadata and output_schema. **The replacement keeps the uuid and gets index+1**,
    so every existing `OutputReference(uuid)` downstream now resolves to the replacement.
  - Replacing with a Flow (or list/dict): a synthetic `store_inputs(replace.output)` job is appended
    *with the same uuid and index+1* ("add a job with same UUID as the current job to store the outputs
    of the flow"). The flow's output therefore becomes the original job's output, again with no
    rewiring of downstream references.
- **`addition`**: `get_flow(response.addition)`. The new subflow's parents are `[current uuid]`
  (jobflow-remote `_append_flow`: `job_parents = [job_doc["uuid"]]` unless REPLACE). Existing children
  are **not** rewired; they run in parallel with the addition. The tutorial
  (`docs/tutorials/5-dynamic-flows.ipynb`) uses it for "If the output is less than 10, do the addition
  again", i.e. loops by self-appending.
- **`detour`**: the new subflow is also parented on the current job, *and* existing children gain the
  detour's leaves as extra parents:
  ```
  if response_type == DynamicResponseType.DETOUR:
      leaf_uuids = [v for v, d in new_flow.graph.out_degree() if d == 0]
      self.jobs.update_many({"parents": job_doc["uuid"]},
                            {"$push": {"parents": {"$each": leaf_uuids}}})
  ```
  (`jobflow-remote/src/jobflow_remote/jobs/jobcontroller.py`, `_append_flow`, around l.3950). In other
  words, the detour is inserted between X and X's children.
- **`stop_children`**: in the local manager this is transitive. A skipped child adds itself to
  `stopped_parents` (`managers/local.py:87-103`). In jobflow-remote it means "Stop the direct children
  of a Job in the WAITING state" (`stop_children`, l.4306), which sets WAITING/READY → `STOPPED`;
  grandchildren then never become READY. **`stop_jobflow`** stops every remaining job in the flow.
  Generated jobs are created in the `STOPPED` state if the same response also had a stop flag
  (`stopped=stop_generated`).
- **Inheritance**: new jobs get `add_hosts_uuids(self.hosts)` and the parent's metadata, config and
  name updates (`dynamic=True`), plus the manager config (job.py:648-673).
- **Persistence (jobflow-remote, BSD-3, Matgenix)** is handled by `checkin_job` → `_append_flow` (l.3819).
  It runs under a flow lock (`lock_flow`) and does the following:
  - allocates `db_id`s from an `auxiliary.next_id` counter;
  - `$addToSet` new uuids into `flows.jobs`;
  - sets `parents.{uuid}.{index}`;
  - `$push`es `(db_id, uuid, index)` into `flows.ids`;
  - inserts new job docs.

  Parents are "identified only by their uuid". Readiness (`refresh_children`) maps each uuid to *its
  highest-index* doc and requires all parents to be COMPLETED.
- **Rerun constraint after dynamic growth (important)** (`rerun_job` docstring, l.967):
  > "In any case, no Job with children with index > 1 can be rerun, as there is no sensible way of
  > handling it."

  `_full_rerun` also refuses a job that "is not the highest index".

  **Lesson for flower:** rerunning a node whose amendment already grew the graph needs explicit
  semantics. Our answer is supersede + retire the spawned subgraph (§3.4).

### 1.2 AiiDA WorkChain (MIT, EPFL)

- **Fixed outline with imperative submission.** The structure is static code (`spec.outline(...)` with
  `while_`/`if_`). The *dynamic* part is `self.submit(...)` inside a step, which returns
  `ToContext(...)`. `_do_step`: "If the stepper returns a non-finished status and the return value is of
  type ToContext, the contents ... will be turned into awaitables ... the process will enter in the Wait
  state" (`aiida-core/src/aiida/engine/processes/workchains/workchain.py:257`).
- **Durability.** `@auto_persist('_awaitables')`. `save_instance_state` stores `ctx` plus the stepper
  state. `load_instance_state` re-arms awaitables. The docs say: "The state of the work chain is saved
  after each outline step" (`docs/source/topics/workflows/concepts.rst:196`). The runner calls back
  `call_on_process_finish(pk)` rather than polling from a blocked thread. This is the right shape for a
  Slurm wait that lasts hours.
- **Fit for flower: poor as a plan model.** The graph is only visible *after the fact* through CALL
  links, and there is nothing to approve up front. Two parts are worth borrowing: the step-level
  checkpoint and the callback-on-child-finish wake.

### 1.3 dflow (deepmodeling, LGPL-3.0)

Dynamism is Argo-native and *declared statically*:
- **Fan-out**: `with_param` / `with_sequence`. "the parallelism equals to the length of the list",
  where the list can be another step's output.
- **Slicing**: `Slices(input_parameter=[...], output_artifact=[...], group_size, sub_path, create_dir)`
  (`src/dflow/python/python_op_template.py:31`). It "assists user to slice input parameters/artifacts
  ... to feed parallel steps and stack their output parameters/artifacts into lists".
- **Loops**: "recursively use a `steps` as the template of a building block within itself to achieve
  dynamic loop", with `when=expr` as the break condition (README l.256, 288).
- **Partial-failure policy**: `continue_on_failed`, `continue_on_num_success`,
  `continue_on_success_ratio`.
- **Restart and reuse by key**: `Step(..., key="some-key")`, then
  `wf.submit(reuse_step=[step0, step1])`. "Before the new workflow runs a step, it will detect if there
  exists a reused step with a matching key. If a match is found, the workflow will skip the step and use
  the outputs from the reused step." Outputs can be edited before reuse with
  `modify_output_parameter/artifact` (README l.360). Keys are *user-chosen names*, not content hashes.

Fit for flower: the `map`/`slice` and `continue_on_success_ratio` ideas are useful as *plan-level
node templates*, for example a `map` node that expands to N children at dispatch time and is recorded
as an auto-approved amendment. Key-based `reuse_step` is the model for an explicit "import result
from run X".

### 1.4 Smithers 1.0 plan store (MIT): the closest match

From `apps/docs/plan-store/src/content/docs/index.md` and `guides/append-a-generation.md`, and
`packages/smithers/flows/plan/docs/concepts/{plan-value,step-keys}.md`:

- **Plan value**: `planId, flow, generation, baseDigest, digest, nodes[]`. Each node has
  `id` ("durable lookup address. Never hashed"), `kind ∈ {step, agent, merge}`, `key`, `material`,
  `effects`, `dependsOn`.
- **Growth only**: "A plan grows; it is never rewritten. `Plan.append` adds a pre-keyed subgraph at the
  next generation. Recorded nodes keep their id, key, edges, and generation byte for byte, and the SQL
  raises rather than letting a caller update or delete one."
- **Two digests**: "`digest`, which moves as the plan elaborates, and `baseDigest`, which still names
  the shape a reviewer signed off on ... an approval taken at generation 0 still validates against a
  plan that has grown three times since."
- **Compare-and-swap append**: the UPDATE matches "the previous generation, the flow, the approved
  `baseDigest`, and that running prefix digest". The failure modes are "generation 3 was skipped or
  moved under the append" and "recorded plan's nodes diverge from the plan this append was grown from".
  An empty append is refused.
- **Engine journal records**: `plan-recorded`, `subgraph-appended`, `node-settled`, `node-invalidated`
  (`apps/docs/engine-store/.../guides/drive-a-plan.md`).
- **Run pinning**: "The same run cannot silently switch plan IDs or approved base graphs; start a new
  run for changed work."
- **Merge nodes are appended as ordinary elaboration**, and recovery "validates the recorded parent
  digest, generated node identity and intervening approved generations. Missing or inconsistent
  recovery data is an error, not permission to invent an extension."

### 1.5 Which model fits "agent proposes amendment → user approves → appended to plan"

**Smithers generations for the storage and approval invariant, plus jobflow verbs for the op
semantics.** jobflow has the right *verbs* but applies them unilaterally at runtime. Smithers has the
right *ledger* (append-only, CAS on parent digest, base vs current digest). AiiDA and dflow are less
suitable (imperative, or static-at-submit).

Proposed amendment document (written to `runs/<id>/amendments/<n>.yaml`, and referenced by hash in
events):
```yaml
amendment: 3
parent_generation: 2          # CAS: rejected if plan.generation != 2 at approve time
parent_digest: sha256:...
proposed_by: {actor: agent, node: screen-candidates, attempt: a1}   # or outer-agent / human
rationale: "3 of 12 candidates passed; add phonon checks for passing ones"
ops:
  - add:      [ {id: phonon-Li3N, kind: job, needs: [relax-Li3N], ...} ]      # jobflow addition
  - detour:   {after: relax-all, nodes: [...]}    # children of relax-all also wait on detour leaves
  - replace:  {node: md-long, with: [...], outputs_from: md-long-part3}   # rev+1 of node id; refs follow
  - supersede:{node: write-report, decl: {...}, invalidate: downstream}   # change a completed/started node
  - edit:     {node: plot, patch: {...}}          # only legal while node is pending (never started)
  - stop:     {nodes: [...], scope: children|subtree}   # stop_children
  - stop_run: {reason: ...}                        # stop_jobflow
policy_ref: auto-approve/map-expansion   # optional: pre-authorised class, still recorded
```
Invariants:
- Ops may only *add* nodes or edges into **pending** nodes.
- Started or terminal nodes are immutable (AiiDA "attributes of a sealed node are immutable";
  Smithers triggers). They change only via `replace` (before start, or as rev+1) or `supersede`.
- Every approved amendment = `generation+1`, `digest` moves, `baseDigest` stays.
- **Dispatch freeze**: a node's `plan_generation` is recorded at dispatch. A pending amendment does not
  block unrelated ready nodes. Nodes the amendment touches are held (`node.held`) until it is approved
  or rejected.

---

## 2. Output references and data passing

| System | Address | Resolution | Hashing |
|---|---|---|---|
| jobflow | `OutputReference(uuid, attributes=(("a",attr)|("i",idx))...)` (`core/reference.py`) | at run: `store.query_one({"uuid": uuid}, ["index"], sort={"index": -1})`, so **latest index wins**; `get_output(which="last"|"first"|"all"|int)` (`core/store.py:466`). `OnMissing.ERROR / NONE / PASS` | none (Mongo docs keyed by `uuid,index`) |
| jobflow-remote | same, plus `parents.{uuid}.{index}` in flow doc | readiness uses highest index per uuid | none |
| AiiDA | labelled links `INPUT_CALC/INPUT_WORK/CREATE/RETURN/CALL_CALC/CALL_WORK` (`src/aiida/common/links.py`) between immutable Data and Process nodes | by node pk/uuid plus link label | `_aiida_hash` extra (see §4) |
| dflow | `step.outputs.parameters["x"]`, `.artifacts["y"]` (Argo) | Argo templating | Argo memoize ConfigMap for `reuse_step` |
| Smithers | `InputRef = Literal{value} \| Ref{from, path} \| Pending{from}` ("The tag is hashed, so `Pending{from}` and `Ref{from, path: []}` cannot collide") | `StepKey.project`, "own data properties" only | plan key (declarations) vs dispatch key (settled content) |
| yak | named artifacts `artifacts/<name>.json` | by name | `artifact.written{hash,bytes}` |
| LabFlow | `outputs.committed{payload.outputs, envelope{materialization, artifactRef, contentHash, sizeBytes}}`; CAS `templates/cas/{aa}/{hash}.json` (`template_cas.py`), `sha256:` over `sort_keys, separators=(",",":")` | — | sha256 of canonical JSON |

**flower proposal**
- Ref syntax: `${node_id}.outputs.<key>[.<json-path>]` and `${node_id}.files.<name>`, plus
  `needs: [node_id]` for ordering-only dependencies (Smithers `Pending`).
- Each attempt writes `runs/<run>/nodes/<node_id>/<attempt_id>/outputs.json` plus `files/`.
  `node.output` events carry small values inline (LabFlow `valueKind/inlineValue`) and large values by
  `{path, sha256, bytes}` (LabFlow envelope).
- **Resolution** happens at dispatch time against the *latest succeeded revision* of `node_id` (jobflow
  semantics). The resolved `{ref → sha256}` map is written into `node.started.payload.inputs`, and
  `input_hash = sha256(canonical(resolved map))`. Replay reads that map; it never re-resolves.
- Missing ref policy: `on_missing: error|none|pass` (jobflow) for optional upstreams, for example when
  a stopped branch is acceptable.
- Canonical JSON: reuse LabFlow `template_cas.canonical_serialize` (sort_keys, compact separators,
  UTF-8) for every hash. One canonicalisation for plans, inputs and outputs.

---

## 3. Durable execution and replay

### 3.1 DBOS Transact (MIT)

- **Checkpoint table** (`dbos/_schemas/system_database.py:103`):
  `operation_outputs(workflow_uuid, function_id, function_name, output, error, child_workflow_id,
  started_at_epoch_ms, completed_at_epoch_ms, serialization, ...)`, with
  `PrimaryKeyConstraint("workflow_uuid", "function_id")`.
  `workflow_status` carries `status, inputs, application_version, forked_from, was_forked_from,
  owner_xid, recovery_attempts, ...`.
- **Ordering**: `function_id` is a per-workflow counter incremented on each step or child call
  (`dbos/_context.py:220-238`, `self.function_id += 1`).
- **Determinism check** (`_sys_db.py:2883` `_check_operation_execution_txn`): if a row exists at
  `(wf, function_id)`, its output or error is returned *instead of executing*. If the recorded
  `function_name` differs, DBOS raises `DBOSUnexpectedStepError`: "function {recorded} was recorded when
  {expected} was expected. Check that your workflow is deterministic." The same call raises
  `DBOSWorkflowCancelledError` if status is CANCELLED, so **cancel is cooperative at the next step
  boundary**.
- **Version gating**: "An application's version is computed from a hash of the source of its
  workflows ... if the app's workflows are updated (which would break recovery), its version changes"
  (`_dbos.py:327`). Recovery only picks up workflows of the matching version. **Analogue: flower plan
  generation/digest.**
- **cancel** (`_sys_db.py:1188`): sets `CANCELLED` unless already `SUCCESS/ERROR`, clears queue and
  owner, and can cascade to children level by level (`cancel_children`).
- **resume** (`_sys_db.py:1233`): sets `ENQUEUED`, `recovery_attempts=0`. Re-execution replays from the
  top, with recorded steps served from `operation_outputs`.
- **fork_workflow(workflow_id, start_step, replacement_children=...)** (`_sys_db.py:1703`): creates a
  *new* workflow id with `forked_from=orig` and copies checkpoints
  `oo.c.function_id < mapping_subquery.c.start_step`. Steps from `start_step` onward re-run.
  `replacement_children` swaps child-workflow ids in the copied rows.
- **rewind_workflow** (`_sys_db.py:1520`): "Drop a workflow's history from start_step onwards and
  re-enqueue it under the same workflow ID". Terminal workflows only.

### 3.2 Claude Code Workflow journal

From the bundled `/workflow-authoring` skill:
- "relaunch with `Workflow({scriptPath, resumeFromRunId})` — the longest unchanged prefix of agent()
  calls returns cached results instantly; the first edited/new call and everything after it runs live.
  Same script + same args → 100% cache hit."
- "`<transcriptDir>/journal.jsonl` ... records each agent's actual return value."
- `Date.now()/Math.random()/new Date()` "throw (they would break resume)".

The docs add (via `notes/prior-art-survey/agent_harness_orchestration.md:75`): "The first agent whose
prompt differs … runs again, and so does every agent after it". A failed or stopped agent causes "every
agent that started after it" to rerun.

Prefix replay is keyed by *call order*, the same as DBOS `function_id`. That is correct for a
deterministic script and wasteful for a DAG of multi-day jobs. yak's spec makes the same point
(`yak/spec.md` §4.4): "Claude Code's dynamic workflows replay in *agent start order* ...
Content-addressed caching avoids that class of waste".

### 3.3 yak (MIT) and Smithers keys: the two-key model

- **yak** (`spec.md` §4.4):
  `semanticKey = sha256(step id + input artifact hashes + adapter id + model id)` and
  `definitionKey = sha256(prompt file contents + tools + schema + budget + engine version)`.
  - `cache: strict`: a mismatch on either key reruns the step and everything downstream.
  - `cache: loose`: reuses the result if only the definition moved, marked `stale: true`.
  - "There is deliberately **no global 'ignore prompt drift' flag** ... `yak run --from <step-id>`".
  - "**Never interpolate churn** — dates, run ids, hostnames, absolute paths — into a prompt".
- **Smithers** (`plan/docs/concepts/step-keys.md`; `engine-store/.../drive-a-plan.md`,
  `attempts-and-replay.md`):
  - The *plan key* folds upstream plan keys, so "an edit anywhere upstream re-keys everything below it".
  - The *dispatch key* "folds the node's own material and never an upstream key. Each input contributes
    content instead". This gives **early cut-off**: "unchanged consumed content can reuse a result even
    when an upstream declaration changed".
  - Tiers are `sealed | compensable | irreversible`. Only sealed nodes are content-cacheable. Other
    tiers get `StepKey.ordinal` run-local keys: "work that changed the world outside the workspace must
    not be served from a cache".
  - Replay table: a `succeeded` row "Returns the recorded result"; `failed` "Rethrows the persisted
    domain failure. It never re-admits the attempt"; `running` "Recovers under the run fence";
    `suspended` "Continues the same attempt".
  - "Invalidation is re-keying ... no reverse-dependency index and no invalidating node visitor".
- **AiiDA** caching hashes class + attributes + input-node hashes (`ProcessNodeCaching.get_objects_to_hash`)
  and **excludes scheduler resources**: `CalcJobNode._hash_ignored_attributes` adds `'queue_name',
  'account', 'qos', 'priority', 'max_wallclock_seconds', 'max_memory_kb', 'version'`. A node is a valid
  cache source only if `is_finished and is_sealed` (`orm/nodes/process/process.py:59`). A hit records
  `_aiida_cached_from`.

### 3.4 Distilled replay rule for flower (coarse nodes)

Per node declaration, compute:
- `decl_hash` covers the node definition minus churn:
  - kind;
  - for agents: prompt file *contents* (not the path), harness id and pinned harness version, model id,
    permission/tool profile, output schema;
  - for shell: the command;
  - for functions: `module:qualname` plus the source hash or package version;
  - for jobs: the job-script template and the code/environment identifier (container digest, module
    list).

  Scheduler resources are **excluded** (AiiDA rule). Retry and timeout settings are also excluded.
- `input_hash` = hash of the resolved input map (content hashes of referenced outputs and files, plus
  literals), computed at dispatch (Smithers dispatch key / yak semanticKey).
- `tier` is one of:
  - `pure`: function/shell with declared I/O, cacheable;
  - `external`: Slurm job or anything with remote side effects, run-local;
  - `agent`: nondeterministic, run-local by default;
  - `gate`: human.

**Rule R1: Resume (same run, any process restart).** Fold `events.jsonl` and act on each node's state:
- **Terminal states are final.** `succeeded`, `failed`, `cancelled` and `skipped` are never re-executed
  automatically (DBOS and Smithers). A `failed` node is re-admitted only by retry policy or an explicit
  `rerun`.
- `submitted` (job): re-attach via the recorded `scheduler_job_id` and poll `sacct`. Do not resubmit.
  This is the LabFlow `external.submitted` → `external.state_observed` pattern.
- `started` agent or shell with no terminal event and a dead PID: emit `node.failed{reason:"orphaned"}`
  (or LabFlow `cancellation.detached`). Retry follows policy *only if* the tier is not `external`. An
  agent may optionally continue via a recorded harness session id (`claude --resume <sid>`), recorded
  as a new attempt with `resumed_from`.
- `gate.requested` with no answer: still waiting. Re-render the pending request file (yak `pending/`
  protocol).
- No hashes are recomputed on plain resume. The recorded `input_hash` is the truth.

**Rule R2: Amend (same run, approved amendment).**
- Only pending nodes change. A pending node whose inputs or declaration changed is simply dispatched
  with the new keys later.
- A started or terminal node changes only via `supersede`. That creates rev+1 of the same `node_id`,
  marks the old rev `superseded`, and marks the **downstream cone** `stale`.
- Stale nodes are re-dispatched. **Early cut-off:** before running a stale node, compare its new
  `(decl_hash, input_hash)` with the recorded succeeded attempt. If they are equal, emit
  `node.succeeded{reused:true, reused_from:<attempt>}` instead of running it. For `agent` and
  `external` tiers this only fires when the inputs really are byte-identical, which is uncommon after an
  upstream agent rerun; that is the correct behaviour.
- `explicit rerun <node> [--downstream|--only]` is shorthand for a supersede with an unchanged
  declaration. It always runs the node, even if keys match (`--force` semantics, like yak `--from`).
  The downstream cone becomes stale, and is subject to early cut-off unless `--downstream=force`.
- **Graph-growing nodes** (jobflow-remote's "index > 1" rule) apply when the node being
  rerun/superseded had proposed an amendment that was approved. The ops it generated are *retired*
  through a recorded companion amendment (`plan.amendment.approved{retires:[...]}`). The new attempt
  may propose afresh. Never edit history silently.

**Rule R3: Fork or new run (`flower fork <run> [--from <node>] [--plan <file>]`).**
- This is the DBOS `fork_workflow` analogue, but cut by **graph cone rather than step ordinal**. Nodes
  not in the cone of `--from` (and not affected by plan changes) carry over their recorded results:
  `node.succeeded{reused_from:"<run>/<node>/<attempt>"}`, the AiiDA `cached_from` idea.
- Within the cone, nodes are reused by key match (strict: both keys).
- `--loose` (yak) allows reuse on an `input_hash` match when only `decl_hash` moved. The node is marked
  `stale:true` in status. It is per-invocation, never a global mode.
- Cross-run content cache (outside fork lineage) is opt-in and limited to `pure` tier.

**Rule R4: Never key by call order.** Ordinals (DBOS `function_id`, CC Workflow prefix) are used only
*inside* one attempt, for example when a `function` node orchestrates sub-calls. The graph-level
identity is `node_id` + rev + attempt.

**Plan version vs replay.** The run pins `baseDigest`. Each attempt records the `plan_generation` it
was dispatched under. A newer generation never invalidates an attempt by itself; only R2 ops do. This
is the counterpart of DBOS app-version gating, made explicit so that it does not happen implicitly.

---

## 4. Provenance

### 4.1 AiiDA model (reference)

- Node classes are Data and Process (Calculation: CalcJob/CalcFunction; Workflow: WorkChain/WorkFunction).
- Link types (`common/links.py`): `CREATE, RETURN, INPUT_CALC, INPUT_WORK, CALL_CALC, CALL_WORK`.
- Two planes (`docs/source/topics/provenance/concepts.rst:39-46`):
  - "**data provenance** ... only data and calculations ... **input** and **create** links ... a
    directed acyclic graph";
  - "**logical provenance** ... **input**, **return** and **call** links ... gives additional
    information on why a specific calculation was run".
- Workflows "cannot *create* data". Computation done inside a workflow step is "'hidden' or
  'encapsulated' ... by a single workflow node" (`workflows/concepts.rst:159-179`). **This is exactly an
  `agent` node: its internals are opaque, so the transcript pointer is the only window.**
- Immutability: sealed nodes ("attributes of a sealed node are immutable", `orm/utils/mixins.py:218`).
  Process input links cannot be added after store (`ProcessNodeLinks.validate_incoming`).
- Caching: `get_objects_to_hash = {class, attributes − ignored, repository_hash, computer_uuid,
  inputs:{label: input.hash}}` (`orm/nodes/caching.py:59`, `process/process.py:86`). Hits are stored as
  `_aiida_hash` / `_aiida_cached_from`.

### 4.2 Minimal flower provenance record (per attempt, in `node.started` / `node.succeeded` events plus `attempt.json`)

| Field | Why |
|---|---|
| `node_id, rev, attempt_id, plan_generation, plan_digest` | identity, plus which approved plan authorised it |
| `decl_hash`, `input_hash`, resolved `inputs{ref → sha256}` | data provenance and replay keys |
| `outputs{key → {sha256, bytes, path or inline}}` | data provenance |
| `executor`: harness name+version (`claude --version`, `codex --version`), `model_id`, effort/params, permission profile hash; or `job`: cluster, scheduler_job_id, partition, nodes, `sacct` final state, exit code; or `function`: qualname + code hash | reproducibility (AiiDA computer_uuid + code) |
| `env`: flower version, git commit of workdir (+dirty flag), container/module digest | AiiDA `version` attributes |
| `transcript`: path + sha256 of harness JSONL (CC `agent-<id>.jsonl`, Codex `--json` stream), `session_id` | opaque agent window, and `--resume` |
| `usage`: tokens/cost, wallclock, core-hours | budget |
| `rationale`: short structured `{decision, alternatives_considered, evidence_refs}` returned by the agent in its output schema | logical provenance ("why") |
| `caused_by`: amendment id / gate answer id / human actor | logical provenance (call/return analogue) |
| `reused_from` (optional) | AiiDA `cached_from` |

Hard rule: rationale is *data the agent returned*, not something re-derived later. Smithers says the
same: "Nothing reconstructs an outcome from current source, an attempt status, or a model prediction"
(`packages/smithers/ENGINE-JOURNAL-PROJECTION.md`).

### 4.3 Export targets (do not adopt as the native store; generate on demand)

- **W3C PROV** (PROV-DM, https://www.w3.org/TR/prov-dm/; PROV-O, https://www.w3.org/TR/prov-o/):
  - attempt → `prov:Activity` (`startedAtTime`/`endedAtTime`);
  - outputs and files → `prov:Entity` with `wasGeneratedBy`; inputs → `used`;
  - harness, model and human → `prov:SoftwareAgent` / `prov:Person` with `wasAssociatedWith`;
  - plan generation → `prov:Plan` via `prov:qualifiedAssociation/hadPlan`;
  - rationale → `prov:Entity` linked by `wasInfluencedBy`.
- **RO-Crate 1.1+** (https://w3id.org/ro/crate/1.1) with **Workflow Run RO-Crate 0.6** profiles
  (https://www.researchobject.org/workflow-run-crate/):
  - Process Run Crate (`https://w3id.org/ro/wfrun/process/0.6`);
  - Workflow Run Crate (`.../workflow/0.6`);
  - **Provenance Run Crate** (`.../provenance/0.6`) is the right target. It describes "internal details
    of the workflow run, such as step executions and intermediate outputs". The mapping is:
    - each attempt → `CreateAction{instrument: SoftwareApplication (harness/code), object: inputs, result: outputs, agent}`;
    - each plan node → `HowToStep`, with `ControlAction{instrument: HowToStep, object: CreateAction}`;
    - the flower engine run → `OrganizeAction{instrument: flower, object: [ControlActions], result: run CreateAction}`;
    - plan file → `ComputationalWorkflow`; node inputs/outputs → `FormalParameter`.
  - Amendments and rationale have no native term; carry them as additional `File`s (amendment YAMLs)
    linked from the relevant `ControlAction`.

---

## 5. Event-sourcing schema

### 5.1 What exists

- **LabFlow** (`packages/labflow/python/src/labflow/events.py`):
  - Envelope: `eventId, runId, eventType, occurredAtIso, nodeRunId?, attemptId?,
    actorType∈{human,agent,system}?, actorId?, payload`. **There is no `seq`**: ordering is file order,
    and `eventId` is opaque ("e1"...).
  - Types:
    - workflow lifecycle: `workflow.accepted|planned|started|suspended|completed`,
      `workflow.outputs.committed|failed`;
    - node: `node.ready|leased|started|heartbeat|suspended|failed`, `outputs.committed`,
      `node.checkpoint|awaiting_config|awaiting_confirm`;
    - control: `retry.scheduled`, `approval.requested|resolved`,
      `cancellation.requested|acknowledged|completed|detached|failed`;
    - external: `external.submitted|state_observed`.
  - The projection is a pure fold (`apply_workflow_event`). Several events carry **full state
    snapshots** (`payload.nodeRun`, `payload.attempt`) rather than deltas. There is no plan-amendment
    event and no `node.succeeded` (success is implied by `outputs.committed`).
  - Golden fixtures live in `fixtures/golden/*.events.jsonl`.
  - The plan is a deterministic `plan.json` envelope (`kind/apiVersion/planVersion/metadata/spec`,
    sorted keys, atomic tmp+rename, `plan.py:dump_plan`), with structured `PlanLoadError{code, path,
    suggestion}` for LLM consumption.
- **DBOS**: not an event log. It uses mutable `workflow_status` plus an append-only-per-step
  `operation_outputs` keyed by `(wf, function_id)`; the idempotency key is the PK.
- **Smithers journal** (`apps/docs/journal/.../index.md`):
  - Per-run `seq`; "lifecycle" (durable) and "telemetry" (lossy) channels share one order.
  - "Producer identity is `(runId, sourceId, sourceSeq)`, unique in the database, so a producer that
    replays after a crash gets `Duplicate` back".
  - An owner fence: "A process that was replaced fails with `fence_lost`".
  - Credential scrubbing on write; checkpoint + compaction.
  - Engine events: `plan-recorded, subgraph-appended, node-settled{built|clean|failed|skipped|deferred},
    node-invalidated, attempt-finished`. Native seqs "are ordered but are not contiguous".
- **yak** (`spec.md` "Journal events"): `run.started{inputHash, adapter}`,
  `step.started{semanticKey, definitionKey}`, `step.completed{artifactHash, cached, stale, skipped}`,
  `step.failed`, `artifact.written{hash, bytes}`, `budget.consumed`, `loop.iteration`,
  `gate.opened{requestPath}`, `gate.answered`, `run.suspended{reason}`, `run.finished`. Every event has
  `{at, runId}`.

### 5.2 Proposed flower envelope (LabFlow-compatible superset)

```json
{"schemaVersion":1, "seq":42, "eventId":"01J...ULID", "runId":"r-...",
 "eventType":"node.submitted", "occurredAtIso":"...Z",
 "nodeRunId":"relax-Li3N@r1", "attemptId":"relax-Li3N@r1#a2",
 "actorType":"system|agent|human", "actorId":"flower|claude-code:opus|user:zh",
 "planGeneration":3, "writerEpoch":7,
 "idempotencyKey":"node.submitted:relax-Li3N@r1#a2",
 "payload":{...}}
```
- `seq`: contiguous per run, assigned by the single writer holding `flock(events.jsonl)`. Readers detect
  truncation or gaps. Smithers' non-contiguity comes from SQL reservations; we do not need that.
- `idempotencyKey`: deterministic from `(eventType, subject, attempt[, sourceSeq])`. On append, the
  writer drops duplicates by key (Smithers `Duplicate`). This makes CLI retries and agent re-invocations
  safe.
- `writerEpoch`: from `run.lease.acquired`. Appends carrying a stale epoch are refused (Smithers
  `fence_lost`). This is needed because the outer agent, cron/`flower tick`, and the user can all
  invoke the CLI concurrently.
- `nodeRunId = node_id@rev`, `attemptId = nodeRunId#aN`. These map onto LabFlow's
  `nodeRunId/attemptId` and onto jobflow `(uuid, index)` + attempt.
- Payloads are **deltas plus identities**. A `payload.nodeRun` snapshot may be included for LabFlow
  projection compatibility, but it is never required for flower's own fold.
- Secret scrubbing on write (Smithers). Large blobs never go inline; they go by `{path, sha256}`.

### 5.3 Vocabulary (fields beyond the envelope)

| Event | Payload | LabFlow equivalent |
|---|---|---|
| `run.created` | `planPath, planDigest(base), flowerVersion, workdir, gitCommit` | `workflow.accepted` |
| `run.lease.acquired` / `run.lease.released` | `owner{host,pid}, epoch, ttl` | — |
| `plan.proposed` | `generation:0, digest, path, proposedBy` | `workflow.planned` |
| `plan.approved` | `generation, digest, baseDigest, approver, note` | `approval.resolved` (subject=plan) |
| `plan.amendment.proposed` | `amendmentId, parentGeneration, parentDigest, path, sha256, ops summary, proposedBy{actor,nodeRunId?,attemptId?}, rationale` | — (new) |
| `plan.amendment.approved` | `amendmentId, newGeneration, newDigest, approver \| policyRef, retires[]?` | — (new) |
| `plan.amendment.rejected` | `amendmentId, approver, reason` | — (new) |
| `run.started` / `run.suspended{reason: gate\|budget\|amendment\|manual}` / `run.resumed` | | `workflow.started/suspended` |
| `run.completed` | `state: succeeded\|failed\|cancelled\|stopped` | `workflow.completed{state}` |
| `node.ready` | `planGeneration` | `node.ready` |
| `node.held` / `node.released` | `reason: pending-amendment\|gate\|manual` | (`node.suspended`) |
| `node.started` | `decl_hash, input_hash, inputs{ref→sha256}, executor{...}, tier, workdir, pid?, sessionId?` | `node.started` (+`payload.attempt`) |
| `node.submitted` (job) | `scheduler, cluster, schedulerJobId, script sha256, remoteDir` | `external.submitted` |
| `node.observed` (job) | `schedulerState, sacct snapshot` (deduplicated: emit only on change) | `external.state_observed` |
| `node.heartbeat` | lossy; optional; may live in a sidecar file | `node.heartbeat` |
| `node.output` | `key, inline? \| {path, sha256, bytes}` | `outputs.committed` |
| `node.succeeded` | `outputs digest, usage, transcript{path, sha256}, rationale?, reused?, reusedFrom?, stale?` | (implied) |
| `node.failed` | `errorClass: transient\|permanent\|orphaned\|timeout, message, exitCode?` | `node.failed` |
| `node.retry.scheduled` | `nextAttempt, notBeforeIso` | `retry.scheduled` |
| `node.cancel.requested` / `node.cancelled` / `node.cancel.detached` | `by, cascade` | `cancellation.requested/completed/detached` |
| `node.stale` | `cause: supersede\|rerun\|upstream, byAmendment?` | — (Smithers `node-invalidated`) |
| `node.superseded` | `byRev, amendmentId` | — |
| `node.skipped` | `cause: stop\|upstream-failed\|condition` | — (jobflow STOPPED) |
| `gate.requested` | `gateId, subject(plan\|amendment\|node), requestPath, answerSchema` | `approval.requested` / `node.awaiting_confirm` |
| `gate.answered` | `gateId, answer sha256/path, by` | `approval.resolved` |
| `agent.decision` (optional, if an agent emits multiple) | `nodeRunId, decision, rationale, evidenceRefs` | — |

**Ordering rules**
- Per attempt: `started < submitted < observed* < output* < (succeeded | failed | cancelled)`.
- A node's `ready` must come after the `succeeded` of all its needs. The fold validates this and
  flags violations; it does not crash.
- `plan.amendment.approved` must come before any `node.ready` of nodes it adds.
- **Write-ahead for side effects:**
  - Before `sbatch`, write `node.started` with the attempt id.
  - After `sbatch` returns, write `node.submitted`.
  - If a crash happens between the two, run `squeue --name=<attemptId>` on resume (the job name is set
    to the attempt id) to reconcile instead of resubmitting.

  This is the irreversible-tier idempotency key that Smithers requires ("an unresolved irreversible
  crossing requires an idempotency key before retry").

**Compatibility with LabFlow**
- Keep the envelope field names and `actorType` enum.
- Keep the `external.*` and `cancellation.*` semantics. Either alias the names or emit LabFlow names
  under a `--labflow-compat` projection.
- Golden fixtures in `packages/labflow/fixtures/golden` can serve as fold tests after a name mapping.
- Add `seq`, `idempotencyKey`, `planGeneration`, `writerEpoch` and `schemaVersion` as optional fields.
  LabFlow `WorkflowEvent.from_dict` reads only fixed keys, so the extra keys are silently ignored
  (harmless); they are also dropped on `to_dict`.
- The plan file can reuse LabFlow `plan.py`'s deterministic envelope and `PlanLoadError` codes.
- `template_cas.py` canonical hashing can be reused directly.

---

## 6. Licenses (for porting decisions)

| Project | License | Worth porting |
|---|---|---|
| jobflow (`jobflow/LICENSE`, pyproject `modified BSD`, LBNL) | BSD-3-style plus LBNL clause | `Response` op semantics; `OutputReference` + `OnMissing`; replace-keeps-uuid trick (ideas, small code) |
| jobflow-remote (`pyproject: BSD-3-Clause`, Matgenix) | BSD-3 | `_append_flow` detour rewiring; rerun constraints; flow lock |
| aiida-core (`LICENSE.txt`) | MIT | hash-ignored scheduler attributes; data vs logical provenance split; `cached_from` |
| dbos-transact-py (`LICENSE`) | MIT | `operation_outputs` schema, fork/rewind/cancel semantics, source-hash app version |
| dflow (`setup.py: LGPLv3`) | **LGPL-3.0** | ideas only (Slices, key-based reuse, continue_on_success_ratio). Avoid copying code into a permissively licensed package |
| smithers-main (`LICENSE`, `flows/LICENSE`) | MIT | plan-generation CAS + base/current digests; plan vs dispatch keys; tiers; journal producer-idempotency and fencing |
| yak (`LICENSE`) | MIT | two-key cache, `--from`, `cache: loose`, file-based gate protocol |
| LabFlow (Eleforge internal) | in-house | envelope, fold, canonical hashing, plan envelope, golden fixtures |
| W3C PROV, RO-Crate, WRROC | W3C / CC-BY specs | export mappings |
