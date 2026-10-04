"""The engine: fold the journal, apply answers, poll work, schedule ready nodes.

Everything happens in :meth:`Engine.tick`, a short, re-entrant pass guarded by a file lock. There
is no resident daemon: ``flower run`` and ``flower status --follow`` loop over ticks in the foreground, a
background driver loops in its own process, and any ``flower status`` call makes one pass.
Because all state is in the journal and all work runs under detached supervisors (or Slurm), any
tick from any process can pick up where the last one stopped.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from . import plan as planmod
from . import template as tpl
from .executors.base import RETRYABLE_DEFAULT, NodeCtx, Outcome
from .journal import Journal
from .rundir import RunPaths, find_root, followers, fs_name, list_runs
from .state import TERMINAL_NODE, TERMINAL_RUN, Attempt, NodeState, RunState, fold
from .util import (FlowerError, answer_cmd, atomic_write_json, atomic_write_text, default_actor, digest, hostname,
                   files_fingerprint, new_id, new_run_id, now, now_iso, parse_duration, parse_iso, read_json,
                   username)

LOCAL_KINDS = ("shell",)


def _executor_kind(spec: dict) -> str:
    """Which executor runs a node: ``job`` for anything on a cluster, else the node's own kind."""
    return "job" if planmod.on_cluster(spec) else str(spec.get("kind"))


def _executors() -> dict:
    from .executors.job import JobExecutor
    from .executors.local import ShellExecutor
    return {"shell": ShellExecutor(), "job": JobExecutor()}


@dataclass
class TickReport:
    run_id: str
    status: str
    busy: bool = False
    started: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    waiting_on: list[str] = field(default_factory=list)
    running: list[str] = field(default_factory=list)
    next_poll_s: float = 2.0
    changed: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ====================================================================== run creation

NEVER_RETRY = {"auth", "contract", "template"}
FILE_EDIT = "plan file edited"  # the rationale prefix of proposals made from the plan file (`flower sync`/`rerun`)


def host_key(c: dict) -> str:
    """One machine, whatever the cluster entry is called in which plan: transport + host."""
    t = c.get("transport", "local")
    return fs_name(f"{t}-{c.get('host') or ('localhost' if t == 'local' else 'unknown')}")


def node_cpus(spec: dict) -> int:
    res = spec.get("resources") or {}
    try:
        return max(1, int(res.get("cpus_per_task") or res.get("cpus") or 1))
    except (TypeError, ValueError):
        return 1


def coerce_inputs(spec: dict, given: dict, base_dir: Path) -> dict:
    out: dict[str, Any] = {}
    problems = []
    for name, s in (spec or {}).items():
        s = s or {}
        if name in given:
            v = given[name]
        elif "default" in s:
            v = s["default"]
        elif s.get("required", True):
            problems.append(planmod.Issue("input_missing", f"inputs.{name}", f"required input {name!r} not given",
                                          f"pass --input {name}=VALUE" + (f" ({s['description']})" if s.get("description") else "")))
            continue
        else:
            continue
        t = s.get("type", "any")
        try:
            if t == "integer" and not isinstance(v, int):
                v = int(v)
            elif t == "number" and not isinstance(v, (int, float)):
                v = float(v)
            elif t == "boolean" and isinstance(v, str):
                v = v.lower() in ("1", "true", "yes", "y", "on")
            elif t in ("object", "array") and isinstance(v, str):
                v = json.loads(v)
            elif t == "path":
                p = Path(str(v)).expanduser()
                p = p if p.is_absolute() else (base_dir / p)
                if s.get("must_exist", True) and not p.exists():
                    raise ValueError(f"path {p} does not exist")
                v = str(p.resolve())
        except (ValueError, TypeError) as exc:
            problems.append(planmod.Issue("input_type", f"inputs.{name}", f"{name}: expected {t}: {exc}"))
            continue
        out[name] = v
    unknown = [k for k in given if k not in (spec or {})]
    for k in unknown:
        problems.append(planmod.Issue("input_unknown", f"inputs.{k}", f"plan has no input named {k!r}",
                                      f"declared inputs: {', '.join(spec or {}) or '(none)'}"))
    if problems:
        raise planmod.PlanInvalid(problems)
    return out


def create_run(plan_raw: dict, inputs: dict | None = None, *, root: Path | None = None,
               actor: str | None = None, approve: bool = False, note: str | None = None,
               reuse_from: list[str] | None = None, rerun_from: list[str] | None = None) -> "Engine":
    plan = planmod.check(plan_raw)
    root = root or find_root(create=True)
    src_dir = Path((plan.get("_source") or {}).get("dir") or os.getcwd())
    values = coerce_inputs(plan.get("inputs") or {}, inputs or {}, Path.cwd())
    # run-level configuration may use ${inputs.*}/${env.*}/${plan.dir}; resolve it now so the contract the
    # user approves shows the concrete clusters/defaults this run will use
    res = tpl.make_resolver({"inputs": values, "env": dict(os.environ),
                             "plan": {"dir": str(src_dir), "id": plan["id"]}})
    try:
        for section in ("clusters", "defaults"):
            if plan.get(section):
                plan[section] = tpl.render(plan[section], res)
    except tpl.TemplateError as exc:
        raise planmod.PlanInvalid([planmod.Issue("template", "clusters/defaults", str(exc),
                                                 "only ${inputs.*}, ${env.*} and ${plan.dir} are available here")]) from None
    run_id = new_run_id(plan["id"])
    paths = RunPaths(root, run_id)
    paths.dir.mkdir(parents=True, exist_ok=False)
    for d in (paths.pending, paths.nodes):
        d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.plan_file, planmod.dump_yaml(plan))
    eng = Engine(paths)
    pdig = planmod.plan_digest(plan)
    actor = actor or default_actor()
    eng.journal.append([
        {"eventType": "run.created", "runId": run_id, "actor": actor, "payload": {
            "plan_id": plan["id"], "title": plan.get("title"), "inputs": values, "run_dir": str(paths.dir),
            "project_dir": str(root), "plan_source": (plan.get("_source") or {}).get("file"),
            "plan_dir": str(src_dir), "flower_version": __version__, "host": hostname(), "user": username(),
            "note": note, "reuse_from": list(reuse_from or []), "rerun_from": list(rerun_from or [])}},
        {"eventType": "plan.proposed", "actor": actor, "planGeneration": 0,
         "payload": {"generation": 0, "digest": pdig, "plan": plan}},
    ])
    from .render import plan_overview
    eng._request_gate("plan", subject="plan", node=None, message=plan_overview(plan, values),
                      decisions=["approve", "reject"], extra={"digest": pdig})
    if approve:
        eng.answer("plan", "approve", text=note or "approved at launch", by=actor)
    return eng


# ====================================================================== engine

class Engine:
    def __init__(self, paths: RunPaths):
        self.paths = paths
        self.journal = Journal(paths.events)
        self._executors: dict | None = None

    @classmethod
    def open(cls, root: Path, run_id: str) -> "Engine":
        return cls(RunPaths(root, run_id))

    @property
    def executors(self) -> dict:
        if self._executors is None:
            self._executors = _executors()
        return self._executors

    def state(self) -> RunState:
        st = fold(self.journal.read())
        if not st.run_id:
            raise FlowerError("run_empty", f"run {self.paths.run_id} has no events")
        return st

    # ------------------------------------------------------------ locking
    @contextmanager
    def lock(self, blocking: bool = True, timeout: float = 120.0):
        self.paths.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.paths.tick_lock, "a+")
        deadline = time.time() + timeout
        got = False
        try:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except BlockingIOError:
                    if not blocking or time.time() > deadline:
                        break
                    time.sleep(0.1)
            if got:
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({"pid": os.getpid(), "host": hostname(), "at": now_iso()}))
                fh.flush()
            yield got
        finally:
            if got:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    # ------------------------------------------------------------ emit helpers
    def emit(self, etype: str, payload: dict | None = None, *, node: str | None = None, attempt: int | None = None,
             actor: str = "system", key: str | None = None, gen: int | None = None) -> dict:
        d: dict[str, Any] = {"eventType": etype, "payload": dict(payload or {}), "actor": actor}
        if node:
            d["nodeId"] = node
        if attempt is not None:
            d["attemptId"] = f"{node}#a{attempt}"
            d["payload"].setdefault("attempt", attempt)
        if key:
            d["idempotencyKey"] = key
        if gen is not None:
            d["planGeneration"] = gen
        return self.journal.append([d])[0]

    def _request_gate(self, gate_id: str, *, subject: str, node: str | None, message: str, decisions: list[str],
                      amendment_id: str | None = None, extra: dict | None = None) -> None:
        req = {"gate_id": gate_id, "run_id": self.paths.run_id, "subject": subject, "node": node,
               "message": message, "decisions": decisions, "amendment_id": amendment_id,
               "answer": answer_cmd(self.paths.run_id, gate_id, decisions),
               "answer_file": str(self.paths.pending / f"{fs_name(gate_id)}.answer.json"),
               "answer_file_format": {"decision": decisions[0], "text": "optional free text", "by": "who"}}
        req.update(extra or {})
        atomic_write_json(self.paths.pending / f"{fs_name(gate_id)}.request.json", req)
        self.emit("gate.requested", {"gate_id": gate_id, "subject": subject, "message": message, "decisions": decisions,
                                     "amendment_id": amendment_id, "request_path": str(self.paths.pending),
                                     **(extra or {})}, node=node, key=f"gate.requested:{gate_id}")

    # ------------------------------------------------------------ public mutations
    def answer(self, gate_id: str, decision: str, text: str | None = None, by: str | None = None) -> dict:
        st = self.state()
        g = st.gates.get(gate_id)
        if g is None:
            # allow node id as shorthand for its open gate
            cand = [x for x in st.open_gates() if x.node == gate_id or x.id.startswith(gate_id)]
            if len(cand) == 1:
                g = cand[0]
        if g is None:
            raise FlowerError("gate_not_found", f"no gate {gate_id!r} in run {st.run_id}",
                                 "List open gates with `flower pending`.")
        if g.status != "open":
            raise FlowerError("gate_closed", f"gate {g.id} was already answered ({g.decision} by {g.by})")
        if decision not in g.decisions:
            raise FlowerError("bad_decision", f"{decision!r} is not a valid answer for gate {g.id}",
                                 f"choose one of: {', '.join(g.decisions)}")
        by = by or default_actor()
        ev = self.emit("gate.answered", {"gate_id": g.id, "decision": decision, "text": text, "by": by},
                       node=g.node, actor=by, key=f"gate.answered:{g.id}")
        atomic_write_json(self.paths.pending / f"{fs_name(g.id)}.answered.json", ev["payload"])
        for suffix in (".request.json", ".answer.json"):
            try:
                (self.paths.pending / f"{fs_name(g.id)}{suffix}").unlink()
            except FileNotFoundError:
                pass
        return ev

    def cancel(self, node: str | None = None, reason: str | None = None, by: str | None = None) -> None:
        by = by or default_actor()
        st = self.state()
        if node is None:
            if st.status in TERMINAL_RUN:
                raise FlowerError("run_finished", f"run is already {st.status}")
            self.emit("run.cancel_requested", {"by": by, "reason": reason}, actor=by, key=f"run.cancel_requested:{st.last_seq}")
            return
        if node not in st.nodes:
            raise FlowerError("node_not_found", f"no node {node!r}")
        ns = st.nodes[node]
        if ns.status in TERMINAL_NODE:
            raise FlowerError("node_finished", f"node {node} is already {ns.status}")
        with self.lock():
            st = self.state()
            ns = st.nodes[node]
            if ns.status in ("running", "waiting") and ns.last:
                spec = self._attempt_spec(node, ns.last)
                ex = self.executors.get(_executor_kind(spec))
                if ex and ns.last.status == "running" and spec.get("kind") != "gate":
                    ex.cancel(self._ctx(st, spec, ns.last))
                    self.emit("node.progress", {"phase": "cancelling", "by": by}, node=node, attempt=ns.last.n)
                    return
            if ns.gate_id and ns.gate_id in st.gates and st.gates[ns.gate_id].status == "open":
                self.emit("gate.answered", {"gate_id": ns.gate_id, "decision": "withdrawn",
                                            "text": f"node cancelled by {by}", "by": by},
                          node=node, actor=by, key=f"gate.answered:{ns.gate_id}")
                try:
                    (self.paths.pending / f"{fs_name(ns.gate_id)}.request.json").unlink()
                except FileNotFoundError:
                    pass
            self.emit("node.cancelled", {"reason": reason or f"cancelled by {by}", "by": by},
                      node=node, attempt=ns.last.n if ns.last else None, actor=by)

    def rerun(self, node: str, downstream: bool = True, force: bool = True, by: str | None = None,
              reason: str | None = None, keep_state: bool = False) -> list[str]:
        """``keep_state``: the node's (and its foreach items') next attempt continues in the FLOWER_STATE_DIR of
        their last attempt, e.g. a checkpointed job that reached its time limit."""
        by = by or default_actor()
        with self.lock():
            st = self.state()
            if node not in st.nodes:
                raise FlowerError("node_not_found", f"no node {node!r}")
            if st.nodes[node].status in ("running", "waiting"):
                raise FlowerError("node_running", f"node {node} is {st.nodes[node].status}",
                                     f"cancel it first: flower cancel {st.run_id} --node {node}")
            g = st.graph()
            # a foreach collector *is* its children: they are upstream of it (it needs them), so they are
            # not among its descendants and must be added explicitly, or only the old results are re-collected
            own = [node] + [c for c in g.order if g.nodes[c].get("expanded_from") == node]
            targets = own + ([d for d in g.descendants(node) if d not in own] if downstream else [])
            busy = [t for t in targets if st.nodes.get(t) and st.nodes[t].status in ("running", "waiting")]
            if busy:
                raise FlowerError("node_running", f"downstream nodes still active: {', '.join(busy)}",
                                     "cancel them or wait for them to finish")
            for t in targets:
                self.emit("node.stale", {"reason": reason or f"rerun of {node} requested by {by}", "by": by,
                                         "force": force and t in own,
                                         **({"keep_state": True} if keep_state and t in own else {})},
                          node=t, actor=by)
            if st.status in TERMINAL_RUN or st.status == "parked":
                self.emit("run.reopened", {"reason": f"rerun {node}", "by": by}, actor=by)
            return targets

    def plan_edits(self, node: str | None, new_inputs: dict | None = None) -> dict:
        """Edits to the plan file that a rerun of ``node`` should pick up, as amendment ops.

        Considered: ``node`` (or its foreach collector), everything downstream of it, the generated
        environment steps those depend on, and nodes that are new in the file. Returns
        {"ops": [...], "changed": [...], "added": [...], "touches_finished": bool, "file": path}.

        New *inputs* (with a `default:`, or a value in ``new_inputs``) and new *clusters* in the file are picked
        up too (ops add_inputs / add_clusters), so a run started as a draft can reach a remote machine later.
        Existing inputs and clusters are fixed for the life of a run, except a cluster's pacing settings
        (``plan.CLUSTER_TUNABLE``: op tune_clusters); other changes to them are reported in ``res["ignored"]``."""
        st = self.state()
        src = st.meta.get("plan_source")
        res = {"ops": [], "changed": [], "added": [], "touches_finished": False, "file": src, "ignored": [],
               "new_inputs": [], "new_clusters": [], "tuned_clusters": []}
        if not src or not Path(src).is_file():
            return res
        raw = planmod.load_plan_file(src)
        # inputs: new declarations become add_inputs, with their value as the default
        cur_in = st.plan.get("inputs") or {}
        add_in = {}
        for name, decl in (raw.get("inputs") or {}).items():
            if name in cur_in:
                continue
            decl = dict(decl) if isinstance(decl, dict) else {"default": decl}
            if new_inputs and name in new_inputs:
                decl["default"] = coerce_inputs({name: {k: v for k, v in decl.items() if k != "default"}},
                                                        {name: new_inputs[name]}, Path.cwd())[name]
            if "default" not in decl:
                raise FlowerError("input_value", f"the plan file declares a new input {name!r} without a value",
                                  f"give it `default:` in {src}, or pass `-i {name}=VALUE`")
            decl.pop("required", None)
            add_in[name] = decl
        # clusters: render new ones exactly as at run creation (inputs, env, plan.dir); existing ones are fixed
        values = {k: d.get("default") for k, d in add_in.items()}
        values.update(st.template_context()["inputs"])
        resolver = tpl.make_resolver({"inputs": values, "env": dict(os.environ),
                                      "plan": {"dir": str(Path(src).parent), "id": st.plan.get("id")}})
        cur_cl = st.plan.get("clusters") or {}
        add_cl, tune_cl = {}, {}
        for name, spec in (raw.get("clusters") or {}).items():
            try:
                rendered = tpl.render(spec, resolver)
            except tpl.TemplateError as exc:
                raise FlowerError("template", f"cluster {name!r} in {src}: {exc}",
                                  "only ${inputs.*}, ${env.*} and ${plan.dir} are available in clusters") from None
            if name not in cur_cl:
                add_cl[name] = rendered
            elif rendered != cur_cl[name]:
                keys = {k for k in set(rendered) | set(cur_cl[name]) if rendered.get(k) != cur_cl[name].get(k)}
                fixed = sorted(keys - set(planmod.CLUSTER_TUNABLE))
                if fixed:
                    res["ignored"].append(f"cluster {name!r} changed in the plan file ({', '.join(fixed)}); only "
                                          f"{', '.join(planmod.CLUSTER_TUNABLE)} may change in a running plan "
                                          "(use a new cluster name, or a new run: `flower run PLAN --reuse RUN`)")
                else:
                    tune_cl[name] = {k: rendered.get(k) for k in sorted(keys)}
        if add_in:
            res["ops"].append({"op": "add_inputs", "inputs": add_in})
            res["new_inputs"] = sorted(add_in)
        if add_cl:
            res["ops"].append({"op": "add_clusters", "clusters": add_cl})
            res["new_clusters"] = sorted(add_cl)
        if tune_cl:
            res["ops"].append({"op": "tune_clusters", "clusters": tune_cl})
            res["tuned_clusters"] = [f"{n} ({', '.join(f'{k}={v}' for k, v in c.items())})" for n, c in tune_cl.items()]
        # compare like with like: the run's plan has its defaults/clusters rendered at creation
        tuned = {n: {**{k: v for k, v in c.items() if k not in tune_cl.get(n, {})},
                     **{k: v for k, v in tune_cl.get(n, {}).items() if v is not None}} for n, c in cur_cl.items()}
        raw["defaults"], raw["clusters"] = st.plan.get("defaults") or {}, {**tuned, **add_cl}
        raw["inputs"] = {**cur_in, **add_in}
        new = {n["id"]: n for n in planmod.normalize(raw).get("nodes") or [] if isinstance(n, dict) and "id" in n}
        g = st.graph()
        cur = g.nodes
        top = (cur.get(node, {}).get("expanded_from") or node) if node is not None else None
        if node is not None and top not in cur and top not in new:
            if res["ops"]:   # only new inputs/clusters, or tuned clusters
                res["added"] = [nid for nid in new if nid not in cur]
                if res["added"]:
                    res["ops"].append({"op": "add", "nodes": [new[nid] for nid in res["added"]]})
            return res
        if node is None:   # the whole plan file (`flower sync`)
            cone = [n for n in cur if not cur[n].get("expanded_from")]
        else:
            cone = [top] + [d for d in g.descendants(top) if not cur[d].get("expanded_from")] if top in cur else []

        def canon(n: dict) -> dict:   # empty values are absent: the run's copy may carry `stage_in: []` and the like
            if str(n.get("generated", "")).startswith("env:"):
                # a generated environment step is its recipe (hash) on its cluster; its script is flower's own and
                # changes with flower's version, which is not an edit of the plan (BUGS #47)
                return {"generated": n.get("generated"), "cluster": n.get("cluster")}
            c = {k: v for k, v in n.items() if k not in ("needs", "bind", "expanded_from")
                 and v not in ({}, [], None)}
            kids = {x for x, s in cur.items() if s.get("expanded_from") == n.get("id")}
            c["needs"] = sorted(d for d in (n.get("needs") or []) if d not in kids)
            return c

        changed = [nid for nid in cone if nid in new and canon(new[nid]) != canon(cur[nid])]
        if node is not None:   # edits the rerun does not reach must not go unnoticed (BUGS #43)
            elsewhere = [n for n in cur if n not in cone and not cur[n].get("expanded_from")
                         and not str(cur[n].get("generated", "")).startswith("env:")
                         and n in new and canon(new[n]) != canon(cur[n])]
            if elsewhere:
                res["ignored"].append(f"the plan file also changes {', '.join(elsewhere)}, outside what this rerun "
                                      f"touches: not applied (`flower sync {st.run_id}` applies them)")
        for nid in list(changed):  # a node's new environment version needs its env step updated too
            for d in new[nid].get("needs") or []:
                if str(cur.get(d, {}).get("generated", "")).startswith("env:") and d in new \
                        and canon(new[d]) != canon(cur[d]) and d not in changed:
                    changed.insert(0, d)
        added = [nid for nid in new if nid not in cur]
        status = {k: v.status for k, v in st.nodes.items()}
        for nid in changed:
            spec = {k: v for k, v in new[nid].items() if k != "id"}
            def ident(n: dict) -> str:   # cache identity, compared like the edit itself (empty values absent)
                return planmod.decl_hash({**{k: v for k, v in n.items() if v not in ({}, [], None)},
                                          "needs": canon(n).get("needs", n.get("needs") or [])})
            if ident(new[nid]) == ident(cur[nid]):
                # description / title / timeout / retry / resources ...: applied without re-running anything (#49);
                # a foreach step's items take the new settings too
                res["ops"].append({"op": "replace", "node": nid, "with": spec, "settings_only": True})
                keys = [k for k in planmod.DECL_EXCLUDE if new[nid].get(k) != cur[nid].get(k)]
                for c in [x for x, sp in cur.items() if sp.get("expanded_from") == nid]:
                    i = (cur[c].get("bind") or {}).get("index", 0)
                    upd = {k: v for k, v in cur[c].items() if k != "id"}
                    for k in keys:
                        if k == "title":
                            upd[k] = f"{new[nid].get('title') or nid} [{i}]"
                        elif new[nid].get(k) is None:
                            upd.pop(k, None)
                        else:
                            upd[k] = new[nid][k]
                    res["ops"].append({"op": "replace", "node": c, "with": upd, "settings_only": True})
                continue
            pending = status.get(nid, "pending") == "pending"
            res["ops"].append({"op": "replace", "node": nid, "with": spec, **({} if pending else {"supersede": True})})
            if status.get(nid) == "succeeded":
                res["touches_finished"] = True
        if added:
            res["ops"].append({"op": "add", "nodes": [new[nid] for nid in added]})
        res["changed"], res["added"] = changed, added
        return res

    def note(self, text: str, node: str | None = None, by: str | None = None) -> dict:
        by = by or default_actor()
        return self.emit("run.note", {"text": text}, node=node, actor=by)

    def propose_amendment(self, ops: list, rationale: str, by: str | None = None, source_node: str | None = None,
                          auto_approve: bool = False) -> str:
        """Record an amendment proposal; open a gate unless auto-approved. Returns the amendment id."""
        by = by or default_actor()
        with self.lock():
            st = self.state()
            if st.status in ("awaiting_approval", "rejected"):
                raise FlowerError("run_not_started", "amend the plan file itself before approval",
                                     "edit the plan and start a new run")
            return self._propose_locked(ops, rationale, by=by, source_node=source_node, auto=auto_approve)

    # ------------------------------------------------------------ tick
    def tick(self) -> TickReport:
        with self.lock(blocking=False) as got:
            if not got:
                st = self.state()
                return TickReport(st.run_id, st.status, busy=True)
            return self._tick()

    def _tick(self) -> TickReport:  # noqa: C901
        rep = TickReport(self.paths.run_id, "")
        self._ingest_files()
        st = self.state()

        # --- cancelled before it was approved
        if st.status == "awaiting_approval" and st.cancel_requested:
            self._cancel_everything(st)
            rep.status = self.state().status
            rep.changed = True
            return rep

        # --- contract approval
        if st.status == "awaiting_approval":
            g = st.gates.get("plan")
            if g and g.status == "answered":
                if g.decision == "approve":
                    self.emit("plan.approved", {"generation": 0, "digest": st.digest, "by": g.by, "note": g.text},
                              actor=g.by or "system", key="plan.approved")
                    self.emit("run.started", {"by": g.by}, key="run.started")
                else:
                    self.emit("plan.rejected", {"by": g.by, "reason": g.text}, actor=g.by or "system",
                              key="plan.rejected")
                st = self.state()
                rep.changed = True
            else:
                rep.status = st.status
                rep.waiting_on = ["plan approval (gate 'plan')"]
                return rep
        # --- amendment gates (also on a finished run: an approved change reopens it)
        if st.status not in ("rejected", "cancelled"):
            for g in st.gates.values():
                if g.subject == "amendment" and g.status == "answered" and g.amendment_id:
                    am = st.amendments.get(g.amendment_id)
                    if am and am.status == "proposed":
                        if g.decision == "approve":
                            try:
                                self._apply_amendment(am.id, by=g.by)
                            except planmod.PlanInvalid as exc:
                                self.emit("plan.amendment.rejected", {"amendment_id": am.id, "by": "flower",
                                                                      "reason": f"no longer applies: {exc.message}"})
                        else:
                            self.emit("plan.amendment.rejected", {"amendment_id": am.id, "by": g.by, "reason": g.text})
                        rep.changed = True
        st = self.state()

        if st.status in TERMINAL_RUN:
            rep.status = st.status
            return rep

        # --- run cancellation
        if st.cancel_requested:
            self._cancel_everything(st)
            rep.status = self.state().status
            rep.changed = True
            return rep

        # --- node gates answered
        for g in list(st.gates.values()):
            if g.subject != "node" or g.status != "answered" or not g.node:
                continue
            ns = st.nodes.get(g.node)
            if not ns or ns.status != "waiting" or ns.gate_id != g.id:
                continue
            self._resolve_node_gate(st, ns, g)
            rep.changed = True
            rep.finished.append(g.node)
        st = self.state()

        # --- poll running work
        running = [(nid, ns) for nid, ns in st.nodes.items()
                   if ns.status in ("running", "waiting") and ns.last and ns.last.status == "running"]
        by_kind: dict[str, list] = {}
        for nid, ns in running:
            try:
                spec = self._attempt_spec(nid, ns.last)
            except KeyError:
                continue
            kind = spec.get("kind")
            if kind == "gate":
                continue
            by_kind.setdefault(_executor_kind(spec), []).append((nid, ns, spec))
        for kind, items in by_kind.items():
            ex = self.executors[kind]
            ctxs = [self._ctx(st, spec, ns.last) for nid, ns, spec in items]
            if hasattr(ex, "poll_many"):
                try:
                    outcomes = ex.poll_many(ctxs)
                except Exception as exc:  # noqa: BLE001 - an executor bug must not wedge every tick
                    self.emit("run.note", {"text": f"{kind} poll failed and will be retried: {type(exc).__name__}: {exc}"})
                    outcomes = [None] * len(ctxs)
            else:
                outcomes = []
                for c in ctxs:
                    try:
                        outcomes.append(ex.poll(c))
                    except Exception as exc:  # executor bug or I/O issue: never crash the tick
                        outcomes.append(Outcome.fail("internal", f"poll failed: {type(exc).__name__}: {exc}"))
            for (nid, ns, spec), oc in zip(items, outcomes):
                if oc is None:
                    rep.running.append(nid)
                    continue
                self._record_outcome(nid, ns, spec, oc)
                rep.finished.append(nid)
                rep.changed = True
        st = self.state()

        # --- schedule
        started = self._schedule(st, rep)
        rep.started.extend(started)
        if started:
            rep.changed = True
        st = self.state()
        # --- run status
        self._update_run_status(st, rep)
        self._write_usage()
        self._write_current_plan(st)
        return rep

    # ------------------------------------------------------------ ingest files (gate answers)
    def _ingest_files(self) -> None:
        st = None
        for f in sorted(self.paths.pending.glob("*.answer.json")) if self.paths.pending.exists() else []:
            data = read_json(f) or {}
            gid = f.name[: -len(".answer.json")]
            st = st or self.state()
            gate = next((g for g in st.gates.values() if fs_name(g.id) == gid), None)
            if gate is None or gate.status != "open":
                f.rename(f.with_suffix(".ignored"))
                continue
            if str(data.get("by") or "").startswith("agent:"):
                atomic_write_json(f.with_suffix(".error.json"), {"code": "inside_run",
                                  "message": "gate answers from agents are not accepted"})
                f.rename(f.with_suffix(".rejected"))
                continue
            try:
                self.answer(gate.id, str(data.get("decision")), text=data.get("text"),
                            by=data.get("by") or "file:" + f.name)
            except FlowerError as exc:
                atomic_write_json(f.with_suffix(".error.json"), exc.to_dict())
                f.rename(f.with_suffix(".rejected"))
    # ------------------------------------------------------------ context & rendering
    def _attempt_spec(self, nid: str, attempt: Attempt) -> dict:
        p = self.paths.attempt_dir(nid, attempt.n) / "node.json"
        spec = read_json(p)
        if spec is None:
            raise KeyError(nid)
        return spec

    def _ctx(self, st: RunState, spec: dict, attempt: Attempt) -> NodeCtx:
        nid = spec["id"]
        adir = self.paths.attempt_dir(nid, attempt.n)

        def emit(etype: str, payload: dict | None = None, **kw):
            return self.emit(etype, payload, node=nid, attempt=attempt.n, **kw)

        return NodeCtx(run_id=st.run_id, run_dir=self.paths.dir, node=spec, attempt=attempt.n, attempt_dir=adir,
                       workdir=Path(attempt.workdir or adir / "work"), inputs=attempt.inputs, plan=st.plan,
                       emit=emit, handle=attempt.handle, progress=attempt.progress, job=attempt.job,
                       series=(st.nodes[nid].state_series if nid in st.nodes else 0) or 0)

    def _resolver(self, st: RunState, spec: dict):
        ctx = st.template_context()
        bind = spec.get("bind") or {}
        ns = st.nodes.get(spec.get("id"))
        res_ctx = {"inputs": ctx["inputs"], "run": ctx["run"], "nodes": ctx["nodes"], "env": dict(os.environ),
                   "feedback": (ns.feedback if ns else "") or "",
                   "plan": {"dir": st.meta.get("plan_dir"), "id": st.plan_id}}
        if "item" in bind:
            res_ctx["item"] = bind["item"]
            res_ctx["index"] = bind.get("index")
        return tpl.make_resolver(res_ctx)

    RENDER_FIELDS = ("inputs", "run", "message", "env", "stage_in", "retrieve", "files", "resources", "prelude")

    def _render(self, st: RunState, spec: dict) -> dict:
        res = self._resolver(st, spec)
        out = dict(spec)
        for f in self.RENDER_FIELDS:
            if f in out and out[f] is not None:
                out[f] = tpl.render(out[f], res)
        base = Path(st.meta.get("plan_dir") or ".")
        if out.get("stage_in"):
            fixed = []
            for item in out["stage_in"]:
                item = {"from": item} if isinstance(item, str) else dict(item)
                src = str(item.get("from", ""))
                if src and not src.startswith("remote:") and not Path(src).expanduser().is_absolute():
                    item["from"] = str((base / src).resolve())
                fixed.append(item)
            out["stage_in"] = fixed
        return out

    # ------------------------------------------------------------ scheduling
    def _dep_state(self, st: RunState, nid: str) -> tuple[str, str | None, str | None]:
        """('wait'|'go'|'skip', reason, cause). cause is 'failure' only when the skip traces back to a
        failed/cancelled node; branches not taken ('condition'), stopped or empty fan-outs are benign."""
        g = st.graph()
        spec = g.nodes[nid]
        deps = [d for d in g.needs[nid] if d in g.nodes]
        if spec.get("foreach") is not None:
            # a collector judges its own children (all_done semantics, in _handle_foreach). It must not wait for
            # them here: after an upstream rerun it has to re-check its item list *before* stale children run
            deps = [d for d in deps if g.nodes[d].get("expanded_from") != nid]
        if not deps:
            return "go", None, None
        stats = {d: st.nodes[d].status if d in st.nodes else "pending" for d in deps}
        if any(s not in TERMINAL_NODE for s in stats.values()):
            return "wait", None, None
        if spec.get("expanded_from") and self._item_outdated(st, spec):
            return "wait", None, None  # the collector re-expands first and hands this child its new item

        def cause_of(d: str) -> str:
            s = stats[d]
            if s in ("failed", "cancelled"):
                return "failure"
            return (st.nodes[d].skip_cause if d in st.nodes else None) or "failure"

        def ok(d: str) -> bool:
            s = stats[d]
            if s == "succeeded":
                return True
            dspec = g.nodes[d]
            return s == "failed" and dspec.get("on_failure") == "continue"

        trig = spec.get("trigger", "all_success")
        if trig == "all_success":
            bad = [d for d in deps if not ok(d)]
            if bad:
                bad.sort(key=lambda d: cause_of(d) != "failure")  # report a failure first if there is one
                d0 = bad[0]
                return "skip", f"upstream {d0} {stats[d0]}" + (
                    f" ({st.nodes[d0].skipped_reason})" if stats[d0] == "skipped" and st.nodes[d0].skipped_reason else ""), \
                    cause_of(d0)
            return "go", None, None
        if trig == "any_success":
            if any(ok(d) for d in deps):
                return "go", None, None
            causes = {cause_of(d) for d in deps}
            return "skip", "no upstream succeeded", "failure" if "failure" in causes else sorted(causes)[0]
        return "go", None, None  # all_done

    # ------------------------------------------------------------ cpu budget per machine, across runs
    def _usage_root(self) -> Path:
        return self.paths.root / ".flower" / "usage"

    def _own_usage(self, st: RunState, g=None) -> dict:
        """{host_key: {node: cpus}} for this run's running cluster jobs on clusters that declare `cpus`."""
        g = g or st.graph()
        out: dict = {}
        for nid, ns in st.nodes.items():
            if ns.status != "running" or nid not in g.nodes or _executor_kind(g.nodes[nid]) != "job":
                continue
            c = (st.plan.get("clusters") or {}).get(g.nodes[nid].get("cluster")) or {}
            if c.get("cpus"):
                out.setdefault(host_key(c), {})[nid] = node_cpus(g.nodes[nid])
        return out

    def _other_usage(self, key: str) -> int:
        total = 0
        for f in (self._usage_root() / key).glob("*.json"):
            if f.stem == self.paths.run_id:
                continue
            d = read_json(f, {}) or {}
            total += sum(int(v) for v in (d.get("jobs") or {}).values())
        return total

    def _write_usage(self) -> None:
        st = self.state()
        mine = {} if st.status in TERMINAL_RUN else self._own_usage(st)
        root = self._usage_root()
        for d in (root.glob("*") if root.is_dir() else []):
            f = d / f"{self.paths.run_id}.json"
            if d.name not in mine and f.exists():
                f.unlink(missing_ok=True)
        for key, jobs in mine.items():
            atomic_write_json(root / key / f"{self.paths.run_id}.json",
                              {"run": self.paths.run_id, "updated": now_iso(), "jobs": jobs})

    def _schedule(self, st: RunState, rep: TickReport) -> list[str]:  # noqa: C901
        g = st.graph()
        started: list[str] = []
        defaults = st.plan.get("defaults") or {}
        limit = int(defaults.get("concurrency") or 4)
        active_local = sum(1 for nid, ns in st.nodes.items() if ns.status == "running" and nid in g.nodes
                           and _executor_kind(g.nodes[nid]) in LOCAL_KINDS)
        changed = True
        while changed:  # skipping a node can unblock others in the same tick
            changed = False
            st = self.state()
            g = st.graph()
            topo = g.topo()
            # retries first: they resume work already under way (with $FLOWER_STATE_DIR, partial progress)
            order = [n for n in topo if (st.nodes.get(n) or NodeState(n)).status == "retrying"] + \
                    [n for n in topo if (st.nodes.get(n) or NodeState(n)).status != "retrying"]
            for nid in order:
                ns = st.nodes.get(nid) or NodeState(nid)
                spec = g.nodes[nid]
                if ns.status not in ("pending", "retrying"):
                    continue
                if ns.status == "retrying" and ns.retry_not_before and parse_iso(ns.retry_not_before) > now():
                    rep.waiting_on.append(f"{nid}: retry at {ns.retry_not_before}")
                    continue
                verdict, why, cause = self._dep_state(st, nid)
                if verdict == "wait":
                    continue
                if verdict == "skip":
                    self.emit("node.skipped", {"reason": why, "cause": cause}, node=nid)
                    changed = True
                    break
                if spec.get("when"):
                    try:
                        cond = tpl.evaluate(str(spec["when"]), self._resolver(st, spec))
                    except tpl.TemplateError as exc:
                        self._fail_without_attempt(nid, ns, "template", f"`when` could not be evaluated: {exc}")
                        changed = True
                        continue
                    if not cond:
                        self.emit("node.skipped", {"reason": f"condition false: {spec['when']}", "cause": "condition"},
                                  node=nid)
                        changed = True
                        break
                if spec.get("foreach") is not None:
                    if self._handle_foreach(st, nid, ns, spec):
                        changed = True
                        break  # the plan changed: re-fold before scheduling anything else
                    continue
                kind = _executor_kind(spec)  # a shell/function node with a `cluster:` runs like a job
                if kind in LOCAL_KINDS and active_local >= limit:
                    rep.waiting_on.append(f"{nid}: concurrency limit {limit}")
                    continue
                if kind == "job":
                    cl = spec.get("cluster")
                    cap = int(((st.plan.get("clusters") or {}).get(cl) or {}).get("max_jobs") or 50)
                    # a retry waiting out its backoff keeps its slot: fresh work must not take it meanwhile
                    busy = sum(1 for x, xs in st.nodes.items()
                               if (xs.status == "running" or (xs.status == "retrying" and x != nid)) and x in g.nodes
                               and _executor_kind(g.nodes[x]) == "job" and g.nodes[x].get("cluster") == cl)
                    if busy >= cap:
                        rep.waiting_on.append(f"{nid}: cluster {cl} at max_jobs={cap}")
                        continue
                    cdef = (st.plan.get("clusters") or {}).get(cl) or {}
                    if cdef.get("cpus"):   # a budget for the machine, shared by all runs of this project
                        key, need = host_key(cdef), node_cpus(spec)
                        used = self._other_usage(key) + sum(
                            n for n in self._own_usage(st, g).get(key, {}).values())
                        if used and used + need > int(cdef["cpus"]):
                            rep.waiting_on.append(f"{nid}: host {key} has {used} of {cdef['cpus']} cpus in use "
                                                  f"(all runs), needs {need}")
                            continue
                if self._start(st, nid, ns, spec):
                    started.append(nid)
                    if kind in LOCAL_KINDS:
                        active_local += 1
                    changed = True
                    break  # re-fold so template context sees fresh state
        return started

    def _fail_without_attempt(self, nid: str, ns: NodeState, cls: str, msg: str) -> None:
        n = ns.next_attempt
        self.emit("node.started", {"attempt": n, "inputs": {}, "handle": {}, "note": "failed before launch"},
                  node=nid, attempt=n)
        self.emit("node.failed", {"error_class": cls, "message": msg, "retryable": False}, node=nid, attempt=n)

    def _start(self, st: RunState, nid: str, ns: NodeState, spec: dict) -> bool:
        n = ns.next_attempt
        try:
            rendered = self._render(st, spec)
        except tpl.TemplateError as exc:
            self._fail_without_attempt(nid, ns, "template", f"could not resolve references: {exc}")
            return True
        inputs = rendered.get("inputs") or {}
        upstream = {}
        for d in st.graph().needs[nid]:
            r = st.nodes[d].result if d in st.nodes else None
            if r is not None:
                upstream[d] = {"outputs": digest(r.outputs), "files": {k: v.get("sha256") for k, v in r.files.items()}}
        dh = planmod.decl_hash(spec)
        if spec.get("kind") == "shell":  # what the step does includes the plan-directory files it runs
            used = files_fingerprint({k: rendered.get(k) for k in ("run", "script", "stage_in", "env", "args",
                                                                   "inputs", "pythonpath")},
                                     (st.plan.get("_source") or {}).get("dir") or st.meta.get("plan_dir"))
            if used:
                dh = digest({"decl": dh, "files": used})
        # the key set is kept from when steps could also be agents or jobs, so recorded results stay reusable
        ih = digest({"inputs": inputs, "upstream": upstream, "bind": spec.get("bind"),
                     "prompt": rendered.get("prompt"), "script": rendered.get("script"), "run": rendered.get("run"),
                     "args": rendered.get("args")})
        # early cut-off / cache: a stale node whose definition and inputs are unchanged keeps its result
        prev = ns.result
        if prev is not None and spec.get("cache", True) and spec.get("kind") != "gate" \
                and not ns.force_next and prev.decl_hash == dh \
                and prev.input_hash == ih:
            self.emit("node.succeeded", {"attempt": n, "outputs": prev.outputs, "files": prev.files,
                                         "summary": prev.summary, "rationale": prev.rationale,
                                         "reused_from": f"a{prev.n}", "decl_hash": dh, "input_hash": ih,
                                         "usage": {}}, node=nid, attempt=n)
            return True
        # results from other runs (--reuse): a step marked cache: false (e.g. an environment check, which must
        # notice an environment deleted since) always runs
        reusable = spec.get("cache", True) and not ns.force_next
        hit = self._reuse_lookup(st, nid, dh, ih) if reusable else None
        if hit is not None:
            src_run, a = hit
            self.emit("node.succeeded", {"attempt": n, "outputs": a.outputs, "files": a.files, "summary": a.summary,
                                         "rationale": a.rationale, "reused_from": f"{src_run}:{nid}#a{a.n}",
                                         "decl_hash": dh, "input_hash": ih, "usage": {}},
                      node=nid, attempt=n)
            return True
        adir = self.paths.attempt_dir(nid, n)
        adir.mkdir(parents=True, exist_ok=True)
        wd = adir / "work"
        wd.mkdir(parents=True, exist_ok=True)
        atomic_write_json(adir / "node.json", rendered)
        up_info = {}
        for d in st.graph().needs[nid]:
            r = st.nodes[d].result if d in st.nodes else None
            if r is not None:
                up_info[d] = {"summary": r.summary, "files": {k: v.get("path") for k, v in r.files.items()},
                              "outputs_keys": sorted(r.outputs)[:20], "dir": r.workdir}
        atomic_write_json(adir / "upstream.json", up_info)
        kind = spec["kind"]
        # write-ahead: the attempt exists in the journal before any side effect happens
        self.emit("node.started", {"attempt": n, "decl_hash": dh, "input_hash": ih, "inputs": inputs,
                                   "workdir": str(wd), "kind": kind, "handle": {},
                                   "executor": kind},
                  node=nid, attempt=n, key=f"node.started:{nid}#a{n}")
        if kind == "gate":
            msg = rendered.get("message") or f"Approve {nid}?"
            self._request_gate(f"{nid}#a{n}", subject="node", node=nid, message=str(msg),
                               decisions=list(rendered.get("decisions") or ["approve", "reject"]))
            return True
        st2 = self.state()
        attempt = st2.nodes[nid].last
        ctx = self._ctx(st2, rendered, attempt)
        try:
            handle = self.executors[_executor_kind(rendered)].start(ctx)
        except Exception as exc:  # noqa: BLE001 - recorded as an attempt failure, retried per policy
            if isinstance(exc, FlowerError):
                oc = Outcome.fail(exc.code, exc.message, retryable=exc.code in RETRYABLE_DEFAULT,
                                  details={"suggestion": exc.suggestion} if exc.suggestion else {})
            else:
                oc = Outcome.fail("spawn", f"{type(exc).__name__}: {exc}", retryable=False)
            st3 = self.state()
            self._record_outcome(nid, st3.nodes[nid], spec, oc)
            return True
        self.emit("node.progress", {"phase": "launched", "handle": handle or {}}, node=nid, attempt=n)
        return True

    # ------------------------------------------------------------ cross-run reuse (fork / replay)
    def _reuse_lookup(self, st: RunState, nid: str, dh: str, ih: str):
        """Recorded result of an earlier run for the same node definition + inputs (yak/DBOS two-key reuse)."""
        sources = st.meta.get("reuse_from") or []
        if not sources or st.graph().nodes[nid].get("kind") == "gate":
            return None  # human decisions are never carried over to another run; they are asked again
        blocked = set(st.meta.get("rerun_from") or [])
        if blocked:
            g = st.graph()
            for b in list(blocked):
                if b in g.nodes:
                    blocked |= set(g.descendants(b))
            if nid in blocked or (st.graph().nodes[nid].get("expanded_from") in blocked):
                return None
        if not hasattr(self, "_reuse_cache"):
            self._reuse_cache = {}
        for rid in sources:
            if rid not in self._reuse_cache:
                try:
                    self._reuse_cache[rid] = Engine(RunPaths(self.paths.root, rid)).state()
                except FlowerError:
                    self._reuse_cache[rid] = None
            other = self._reuse_cache[rid]
            if other is None or nid not in other.nodes:
                continue
            for a in reversed(other.nodes[nid].attempts):
                if a.status == "succeeded" and a.decl_hash == dh and a.input_hash == ih:
                    return rid, a
        return None

    # ------------------------------------------------------------ foreach
    def _item_outdated(self, st: RunState, spec: dict) -> bool:
        """A foreach child whose collector is pending (first expansion or a rerun upstream) and whose bound item is
        no longer what the collector's list evaluates to now. It must not run: re-expansion replaces it."""
        g = st.graph()
        parent = spec.get("expanded_from")
        pspec = g.nodes.get(parent)
        pns = st.nodes.get(parent)
        if pspec is None or (pns is not None and pns.status != "pending"):
            return False
        try:
            items = pspec["foreach"]
            if isinstance(items, str):
                items = tpl.render(items, self._resolver(st, pspec))
            if isinstance(items, dict):
                items = [{"key": k, "value": v} for k, v in items.items()]
        except tpl.TemplateError:
            return False  # the collector keeps the existing expansion in that case
        if not isinstance(items, list):
            return False
        bind = spec.get("bind") or {}
        i = bind.get("index", 0)
        if i >= len(items) or items[i] != bind.get("item"):
            return True
        # the step itself was edited (env, tmpdir, run, ...): wait for the re-expansion instead of running the old one
        def canon(n: dict) -> dict:
            return {k: v for k, v in n.items() if k not in ("id", "title", "description", "needs", "bind",
                                                            "expanded_from", "foreach")}
        return canon(pspec) != canon(spec)

    def _handle_foreach(self, st: RunState, nid: str, ns: NodeState, spec: dict) -> bool:
        g = st.graph()
        children = [c for c in g.order if g.nodes[c].get("expanded_from") == nid]
        children.sort(key=lambda c: (g.nodes[c].get("bind") or {}).get("index", 0))
        try:
            items = spec["foreach"]
            if isinstance(items, str):
                items = tpl.render(items, self._resolver(st, spec))
            if isinstance(items, dict):
                items = [{"key": k, "value": v} for k, v in items.items()]
            if not isinstance(items, list):
                raise tpl.TemplateError(f"foreach must evaluate to a list, got {type(items).__name__}")
        except tpl.TemplateError as exc:
            if children:
                items = None  # keep the existing expansion
            else:
                self._fail_without_attempt(nid, ns, "template", f"foreach: {exc}")
                return True
        old_items = [(g.nodes[c].get("bind") or {}).get("item") for c in children]
        if not children and not items:
            self.emit("node.skipped", {"reason": "foreach over an empty list", "cause": "empty"}, node=nid)
            return True
        def child(i: int, item):
            c = {k: v for k, v in spec.items() if k not in ("foreach", "title")}
            c["id"] = f"{nid}[{i}]"
            c["title"] = f"{spec.get('title') or nid} [{i}]"
            c["needs"] = [d for d in planmod.effective_needs(spec) if d not in children]
            c["bind"] = {"item": item, "index": i}
            c["expanded_from"] = nid
            return c

        def canon(n: dict) -> dict:
            return {k: v for k, v in n.items() if k not in ("id", "title", "description", "needs", "bind", "expanded_from")}

        # children whose template changed (the step was edited, its items were not): replace them too
        reshaped = set()
        if items is not None:
            for i, c in enumerate(children[:len(items)]):
                if canon(child(i, items[i])) != canon(g.nodes[c]):
                    reshaped.add(i)
        if items is not None and (items != old_items or reshaped):
            def in_flight(c: str) -> bool:
                return st.nodes.get(c, NodeState(c)).status in ("running", "waiting", "retrying")

            def settings_only(i: int) -> bool:
                return items[i] == old_items[i] and \
                    planmod.decl_hash(child(i, items[i])) == planmod.decl_hash(g.nodes[children[i]])
            # wait only if an in-flight item itself would be superseded or dropped; appended items (#51), items
            # changed in their settings only, and pending items are updated at once
            touched = [i for i in range(min(len(items), len(children)))
                       if (items[i] != old_items[i] or i in reshaped) and not settings_only(i)]
            dropped = list(range(len(items), len(children)))
            if any(in_flight(children[i]) for i in touched + dropped):
                return False  # let in-flight children finish first
            base_needs = [d for d in (spec.get("needs") or []) if d not in children]
            ops: list[dict] = []
            add = [child(i, it) for i, it in enumerate(items) if i >= len(children)]
            if add:
                ops.append({"op": "add", "nodes": add})
            for i, it in enumerate(items[:len(children)]):
                if it != old_items[i] or i in reshaped:
                    cid = children[i]
                    new_child = child(i, it)
                    if it == old_items[i] and planmod.decl_hash(new_child) == planmod.decl_hash(g.nodes[cid]):
                        # only timeout / retry / resources / ... changed: finished items keep their results (#49)
                        ops.append({"op": "replace", "node": cid, "with": new_child, "settings_only": True})
                        continue
                    done = st.nodes.get(cid, NodeState(cid)).status not in ("pending",)
                    ops.append({"op": "replace", "node": cid, "with": new_child, **({"supersede": True} if done else {})})
            drop = [c for c in children[len(items):]]
            if drop:
                ops.append({"op": "drop", "nodes": drop})
            ids = [f"{nid}[{i}]" for i in range(len(items))]
            ops.append({"op": "set_needs", "node": nid, "needs": base_needs + ids})
            status = {k: v.status for k, v in st.nodes.items()}
            aid = new_id("fx")
            why = (f"foreach expansion of {nid} over {len(items)} item(s)" if not children else
                   f"foreach {nid}: item list changed ({len(old_items)} → {len(items)}), re-expanded"
                   if items != old_items else f"foreach {nid}: step definition changed, {len(reshaped)} item(s) re-expanded")
            try:
                new_plan, effects = planmod.apply_amendment(st.plan, ops, status)
            except planmod.PlanInvalid as exc:
                self._fail_without_attempt(nid, ns, "foreach", f"could not (re-)expand: {exc.message}")
                return True
            self.emit("plan.amendment.proposed", {"amendment_id": aid, "parent_generation": st.generation,
                                                  "parent_digest": st.digest, "ops": ops, "proposed_by": "flower",
                                                  "rationale": why, "source_node": nid,
                                                  "diff": planmod.diff_plans(st.plan, new_plan)})
            self._commit_amendment(aid, new_plan, effects, by="policy:foreach", parent_digest=st.digest)
            return True
        if not children:
            return False
        # children exist: collect when all are terminal
        stats = {c: st.nodes.get(c, NodeState(c)).status for c in children}
        if any(s not in TERMINAL_NODE for s in stats.values()):
            return False
        results = []
        failed = []
        for c in children:
            r = st.nodes[c].result
            results.append(r.outputs if r else None)
            if stats[c] != "succeeded":
                failed.append(c)
        n = ns.next_attempt
        self.emit("node.started", {"attempt": n, "inputs": {}, "handle": {}, "kind": "foreach"}, node=nid, attempt=n)
        outs = {"items": results, "count": len(children), "failed": failed,
                "succeeded": len(children) - len(failed)}
        if failed and spec.get("on_failure") != "continue":
            self.emit("node.failed", {"error_class": "foreach", "retryable": False, "outputs": outs,
                                      "message": f"{len(failed)}/{len(children)} item(s) failed: {', '.join(failed[:5])}"},
                      node=nid, attempt=n)
        else:
            self.emit("node.succeeded", {"outputs": outs, "summary": f"{outs['succeeded']}/{len(children)} item(s) succeeded"},
                      node=nid, attempt=n)
        return True

    # ------------------------------------------------------------ outcomes
    def _record_outcome(self, nid: str, ns: NodeState, spec: dict, oc: Outcome) -> None:
        n = ns.last.n
        if oc.status == "succeeded":
            self.emit("node.succeeded", {"outputs": oc.outputs, "files": oc.files, "usage": oc.usage,
                                         "summary": oc.summary, "rationale": oc.rationale,
                                         "decl_hash": ns.last.decl_hash, "input_hash": ns.last.input_hash},
                      node=nid, attempt=n, key=f"node.done:{nid}#a{n}")
            return
        if oc.status == "cancelled":
            self.emit("node.cancelled", {"reason": oc.message or "cancelled", "usage": oc.usage},
                      node=nid, attempt=n, key=f"node.done:{nid}#a{n}")
            return
        retry = spec.get("retry") or {}
        classes = set(retry.get("on") or RETRYABLE_DEFAULT)
        retryable = oc.retryable if oc.retryable is not None else oc.error_class in classes
        if oc.retryable is None and oc.error_class in classes:
            retryable = True
        # a class the step lists itself is retried (its author decided), except failures that repeating can
        # never fix: bad credentials, a broken output contract, an unresolvable template
        if oc.error_class in set(retry.get("on") or []) and oc.error_class not in NEVER_RETRY:
            retryable = True
        budget = int(retry.get("max_attempts", 1))
        self.emit("node.failed", {"error_class": oc.error_class, "message": oc.message, "retryable": retryable,
                                  "usage": oc.usage, "outputs": oc.outputs or None, "details": oc.details or None},
                  node=nid, attempt=n, key=f"node.done:{nid}#a{n}")
        used = ns.attempts_since_reset
        if retryable and used < budget:
            back = parse_duration(retry.get("backoff") or "10s") or 0
            delay = back * (2 ** max(0, used - 1))
            ra = (oc.details or {}).get("retry_after_s")
            if ra:
                delay = max(delay, float(ra))
            import datetime as dt
            nb = (now() + dt.timedelta(seconds=delay)).isoformat()
            why = f"{oc.error_class}: attempt {used}/{budget} failed"
            self.emit("node.retry_scheduled", {"next_attempt": n + 1, "not_before": nb, "reason": why},
                      node=nid, attempt=n)

    def _propose_locked(self, ops: list, rationale: str, by: str, source_node: str | None, auto: bool) -> str:
        st = self.state()
        status = {k: v.status for k, v in st.nodes.items()}
        aid = new_id("am")
        try:
            new_plan, effects = planmod.apply_amendment(st.plan, ops, status)
            diff = planmod.diff_plans(st.plan, new_plan)
        except planmod.PlanInvalid as exc:
            self.emit("plan.amendment.proposed", {"amendment_id": aid, "parent_generation": st.generation,
                                                  "parent_digest": st.digest, "ops": ops, "rationale": rationale,
                                                  "proposed_by": by, "source_node": source_node})
            self.emit("plan.amendment.rejected", {"amendment_id": aid, "by": "flower",
                                                  "reason": exc.message, "issues": exc.issues})
            raise
        self.emit("plan.amendment.proposed", {"amendment_id": aid, "parent_generation": st.generation,
                                              "parent_digest": st.digest, "ops": ops, "rationale": rationale,
                                              "proposed_by": by, "source_node": source_node, "diff": diff})
        if auto:
            self._commit_amendment(aid, new_plan, effects, by=f"policy:{source_node or 'auto'}", parent_digest=st.digest)
        else:
            from .render import amendment_overview
            self._request_gate(f"amend-{aid}", subject="amendment", node=None,
                               message=amendment_overview(rationale, ops, diff, by),
                               decisions=["approve", "reject"], amendment_id=aid, extra={"diff": diff})
        return aid

    def _apply_amendment(self, aid: str, by: str | None) -> None:
        """Apply an approved amendment. If the plan moved since the reviewer saw the diff, apply only when the
        recomputed diff is identical; otherwise reject it as stale and re-open a fresh gate (compare-and-swap)."""
        st = self.state()
        am = st.amendments[aid]
        status = {k: v.status for k, v in st.nodes.items()}
        new_plan, effects = planmod.apply_amendment(st.plan, am.ops, status)
        if am.parent_generation != st.generation:
            def key(d):
                return (sorted(d.get("added") or []), sorted(d.get("removed") or []),
                        sorted(c["id"] + ":" + ",".join(c["fields"]) for c in d.get("changed") or []))
            if key(planmod.diff_plans(st.plan, new_plan)) != key(am.diff or {}):
                self.emit("plan.amendment.rejected", {"amendment_id": aid, "by": "flower",
                                                      "reason": f"stale: reviewed against generation {am.parent_generation},"
                                                                f" plan is now at {st.generation} and the change would "
                                                                "differ; re-proposed for a fresh review"})
                self._propose_locked(am.ops, am.rationale, by=am.proposed_by, source_node=am.source_node, auto=False)
                return
        self._commit_amendment(aid, new_plan, effects, by=by or "system")

    def _commit_amendment(self, aid: str, new_plan: dict, effects: dict, by: str,
                          parent_digest: str | None = None) -> None:
        st = self.state()
        if parent_digest is not None and st.digest != parent_digest:  # compare-and-swap on the plan generation
            raise FlowerError("plan_moved", f"amendment {aid} was computed on a stale plan generation",
                                 "re-propose it against the current plan")
        src = st.plan.get("_source")
        if src:
            new_plan["_source"] = src
        gen = st.generation + 1
        self.emit("plan.amendment.approved", {"amendment_id": aid, "generation": gen,
                                              "digest": planmod.plan_digest(new_plan), "plan": new_plan,
                                              "effects": effects, "by": by}, actor=by, gen=gen,
                  key=f"plan.amendment.approved:{aid}")
        for nid in effects.get("stale", []):
            if nid in st.nodes and st.nodes[nid].status not in ("pending",):
                self.emit("node.stale", {"reason": f"superseded by amendment {aid}", "by": by}, node=nid)
        for nid in effects.get("stop", []):
            self.emit("node.skipped", {"reason": f"stopped by amendment {aid}", "cause": "stopped"}, node=nid)
        st = self.state()
        st = self.state()
        if st.status in TERMINAL_RUN or st.status == "parked":
            self.emit("run.reopened", {"reason": f"amendment {aid} approved", "by": by})

    def withdraw_file_proposals(self, keep_ops: list) -> list[str]:
        """A proposal made from the plan file is a snapshot of it; a newer one supersedes it. Withdraw the open
        ones that differ from ``keep_ops`` so an outdated snapshot cannot park the run (#58)."""
        out = []
        with self.lock():
            st = self.state()
            for g in st.open_gates():
                am = st.amendments.get(g.amendment_id) if g.subject == "amendment" else None
                if am and am.status == "proposed" and am.rationale.startswith(FILE_EDIT) and am.ops != keep_ops:
                    self.emit("gate.answered", {"gate_id": g.id, "decision": "withdrawn", "by": "system",
                                                "text": "superseded by a newer version of the plan file"},
                              key=f"gate.answered:{g.id}")
                    self.emit("plan.amendment.rejected", {"amendment_id": am.id, "by": "flower",
                                                          "reason": "superseded by a newer version of the plan file"})
                    try:
                        (self.paths.pending / f"{fs_name(g.id)}.request.json").unlink()
                    except FileNotFoundError:
                        pass
                    out.append(am.id)
        return out

    # ------------------------------------------------------------ gates
    def _resolve_node_gate(self, st: RunState, ns: NodeState, g) -> None:
        spec = st.node_spec(ns.id)
        n = ns.last.n
        approve_value = (spec.get("decisions") or ["approve"])[0]
        outs = {"decision": g.decision, "text": g.text or "", "by": g.by}
        if g.decision == approve_value or not spec.get("on_reject") and g.decision not in ("reject", "abort"):
            self.emit("node.succeeded", {"outputs": outs, "summary": f"{g.decision} by {g.by}"
                                         + (f": {g.text}" if g.text else "")}, node=ns.id, attempt=n,
                      actor=g.by or "system")
            return
        orj = spec.get("on_reject") or {}
        targets = orj.get("rerun") or []
        maxa = int(orj.get("max_attempts", 3))
        if targets and ns.rerun_count < maxa and g.decision != "abort":
            graph = st.graph()
            cone: list[str] = []
            for t in targets:
                for x in [t] + graph.descendants(t):
                    if x not in cone:
                        cone.append(x)
            if ns.id not in cone:
                cone.append(ns.id)
            self.emit("node.succeeded", {"outputs": outs, "summary": f"{g.decision} by {g.by}: {g.text or ''}"},
                      node=ns.id, attempt=n, actor=g.by or "system")
            for x in cone:
                self.emit("node.stale", {"reason": f"{ns.id} answered {g.decision!r}: {g.text or 'no reason given'}",
                                         "by": g.by, "force": x in targets or x == ns.id, "feedback": g.text or "",
                                         "rerun_count": ns.rerun_count + 1 if x == ns.id else None}, node=x)
            return
        self.emit("node.failed", {"error_class": "rejected", "retryable": False, "outputs": outs,
                                  "message": f"{g.decision} by {g.by}" + (f": {g.text}" if g.text else "")
                                  + (f" (rework limit {maxa} reached)" if targets else "")},
                  node=ns.id, attempt=n, actor=g.by or "system")

    # ------------------------------------------------------------ cancellation & status
    def _cancel_everything(self, st: RunState) -> None:
        for nid, ns in st.nodes.items():
            if ns.status in ("running", "waiting") and ns.last and ns.last.status == "running":
                try:
                    spec = self._attempt_spec(nid, ns.last)
                    if spec.get("kind") != "gate":
                        self.executors[_executor_kind(spec)].cancel(self._ctx(st, spec, ns.last))
                except Exception:  # noqa: BLE001 - best effort, still record cancellation
                    pass
                self.emit("node.cancelled", {"reason": "run cancelled"}, node=nid, attempt=ns.last.n)
            elif ns.status in ("pending", "retrying"):
                self.emit("node.skipped", {"reason": "run cancelled", "cause": "cancelled"}, node=nid)
        for g in st.open_gates():
            self.emit("gate.answered", {"gate_id": g.id, "decision": "withdrawn", "text": "run cancelled",
                                        "by": "system"}, node=g.node, key=f"gate.answered:{g.id}")
        for f in self.paths.pending.glob("*.request.json") if self.paths.pending.exists() else []:
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        self.emit("run.completed", {"status": "cancelled", "reason": "cancelled on request"},
                  key=f"run.completed:cancel:{st.last_seq}")

    def _update_run_status(self, st: RunState, rep: TickReport) -> None:
        g = st.graph()
        nodes = [st.nodes.get(n, NodeState(n)) for n in g.order]
        rep.running = sorted({ns.id for ns in nodes if ns.status == "running"} | set(rep.running))
        waiting = [ns for ns in nodes if ns.status == "waiting"]
        pending = [ns for ns in nodes if ns.status in ("pending", "retrying")]
        open_gates = st.open_gates()
        for gt in open_gates:
            rep.waiting_on.append(f"gate {gt.id} ({gt.subject})")
        if rep.running:
            rep.status = "running"
            has_job = any(_executor_kind(g.nodes[n]) == "job" for n in rep.running if n in g.nodes)
            has_local = any(_executor_kind(g.nodes[n]) in LOCAL_KINDS for n in rep.running if n in g.nodes)
            rep.next_poll_s = 1.0 if has_local else (15.0 if has_job else 2.0)
            if followers(self.paths):  # someone is watching a node: answer within half a second
                rep.next_poll_s = 0.5
            if st.status == "parked":
                self.emit("run.started", {"reason": "work resumed"})
            return
        if any(ns.status == "retrying" for ns in pending):
            rep.status = "running"
            nb = min(parse_iso(ns.retry_not_before) for ns in pending if ns.retry_not_before) if any(
                ns.retry_not_before for ns in pending) else now()
            rep.next_poll_s = max(0.5, min(60.0, (nb - now()).total_seconds()))
            return
        if pending and not rep.started and not waiting and not open_gates:
            # nothing can make progress: a dependency is stuck (should not happen) -> report and park
            pass
        if waiting or open_gates or pending:
            rep.status = "parked"
            rep.next_poll_s = 0
            reason = "; ".join(rep.waiting_on[:4]) or "waiting"
            if st.status != "parked" or st.status_reason != reason:
                self.emit("run.parked", {"reason": reason, "timed": False})
            return
        if not nodes:   # a draft run (`flower start`) waits for its first step
            rep.status = "parked"
            rep.next_poll_s = 0
            reason = "no steps yet: add one with `flower add RUN ID -- <command>`"
            if st.status != "parked" or st.status_reason != reason:
                self.emit("run.parked", {"reason": reason, "timed": False})
            return
        failed = [ns.id for ns in nodes if ns.status in ("failed", "cancelled")
                  and not (ns.status == "failed" and g.nodes[ns.id].get("on_failure") == "continue")]
        upstream_skips = [ns.id for ns in nodes if ns.status == "skipped" and ns.skip_cause in ("failure", "cancelled")]
        if failed or upstream_skips:
            reason = f"{len(failed)} node(s) failed: {', '.join(failed[:5])}" if failed else \
                f"{len(upstream_skips)} node(s) skipped after upstream failure"
            self.emit("run.completed", {"status": "failed", "reason": reason}, key=f"run.completed:{st.last_seq}")
            rep.status = "failed"
        else:
            self.emit("run.completed", {"status": "succeeded", "reason": "all nodes finished"},
                      key=f"run.completed:{st.last_seq}")
            rep.status = "succeeded"
        rep.changed = True

    def _write_current_plan(self, st: RunState) -> None:
        try:
            if st.generation > 0:
                text = planmod.dump_yaml(self.state().plan)
                if not self.paths.current_plan_file.exists() or self.paths.current_plan_file.read_text() != text:
                    atomic_write_text(self.paths.current_plan_file, text)
        except OSError:
            pass

    # ------------------------------------------------------------ drive loop
    def drive(self, *, until: str = "settled", timeout: float | None = None, on_tick=None,
              max_sleep: float = 30.0) -> TickReport:
        """Tick until the run is terminal or parked (``until='settled'``) or only terminal (``'terminal'``)."""
        start = time.time()
        idle_sleep = 0.5
        while True:
            rep = self.tick()
            if on_tick:
                on_tick(rep)
            if rep.status in TERMINAL_RUN:
                return rep
            if rep.status in ("parked", "awaiting_approval") and until == "settled" and not rep.busy and not rep.changed:
                if not (rep.status == "parked" and rep.next_poll_s):  # timers keep us alive
                    return rep
            if timeout is not None and time.time() - start > timeout:
                return rep
            if rep.busy:
                time.sleep(1.0)
                continue
            if rep.changed:
                idle_sleep = 0.2
            else:
                idle_sleep = min(max(idle_sleep * 1.5, 0.5), rep.next_poll_s or max_sleep, max_sleep)
            time.sleep(idle_sleep)


def driver_alive(paths: RunPaths) -> dict | None:
    info = read_json(paths.driver_file)
    if not info or info.get("host") != hostname():
        return info if info else None
    try:
        os.kill(int(info["pid"]), 0)
        return info
    except (ProcessLookupError, ValueError, KeyError):
        return None
    except PermissionError:
        return info
