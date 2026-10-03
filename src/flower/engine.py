"""The engine: fold the journal, apply answers/signals, poll work, schedule ready nodes.

Everything happens in :meth:`Engine.tick`, a short, re-entrant pass guarded by a file lock. There
is no resident daemon: ``flower run/watch/wait`` loop over ticks in the foreground, ``--detach``
loops in a background process, and cron/scrontab or the outer agent can call ``flower tick``.
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
from .rundir import RunPaths, find_root, fs_name, list_runs
from .state import TERMINAL_NODE, TERMINAL_RUN, Attempt, NodeState, RunState, fold
from .util import (FlowerError, atomic_write_json, atomic_write_text, default_actor, digest, hostname,
                   new_id, new_run_id, now, now_iso, parse_duration, parse_iso, read_json, source_fingerprint,
                   username)

LOCAL_KINDS = ("shell", "function", "agent")


def _executors() -> dict:
    from .executors.agent import AgentExecutor
    from .executors.job import JobExecutor
    from .executors.local import FunctionExecutor, ShellExecutor
    return {"shell": ShellExecutor(), "function": FunctionExecutor(), "agent": AgentExecutor(), "job": JobExecutor()}


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
    for d in (paths.pending, paths.signals, paths.nodes):
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
               "answer": f"flower answer {self.paths.run_id} {gate_id} <{'|'.join(decisions)}> [--text '…']",
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
                ex = self.executors.get(spec.get("kind"))
                if ex and ns.last.status == "running" and spec.get("kind") not in ("gate", "wait"):
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
              reason: str | None = None) -> list[str]:
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
                                         "force": force and t in own}, node=t, actor=by)
            if st.status in TERMINAL_RUN or st.status == "parked":
                self.emit("run.reopened", {"reason": f"rerun {node}", "by": by}, actor=by)
            return targets

    def signal(self, name: str, data: Any = None, by: str | None = None, token: str | None = None) -> dict:
        by = by or default_actor()
        return self.emit("signal.received", {"name": name, "data": data, "by": by, "token": token}, actor=by)

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
        if st.status in TERMINAL_RUN:
            rep.status = st.status
            return rep

        # --- run cancellation
        if st.cancel_requested:
            self._cancel_everything(st)
            rep.status = self.state().status
            rep.changed = True
            return rep

        # --- amendment gates
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
            if kind == "wait":
                if self._poll_wait(st, nid, ns, spec):
                    rep.finished.append(nid)
                    rep.changed = True
                continue
            if kind == "gate":
                continue
            by_kind.setdefault(kind, []).append((nid, ns, spec))
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
        # waits armed in this tick may already be satisfiable (sticky signals, elapsed timers)
        for nid in started:
            ns = st.nodes.get(nid)
            if ns and ns.status == "waiting" and ns.last and ns.last.status == "running" and not ns.gate_id:
                spec = self._attempt_spec(nid, ns.last)
                if spec.get("kind") == "wait" and self._poll_wait(st, nid, ns, spec):
                    rep.finished.append(nid)
                    st = self.state()

        # --- run status
        self._update_run_status(st, rep)
        self._write_current_plan(st)
        return rep

    # ------------------------------------------------------------ ingest files (gate answers, signals)
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
        if self.paths.signals.exists():
            for f in sorted(self.paths.signals.glob("*.json")):
                data = read_json(f) or {}
                self.emit("signal.received", {"name": data.get("name") or f.stem, "data": data.get("data"),
                                              "by": data.get("by") or "file:" + f.name, "token": data.get("token")},
                          key=f"signal.file:{f.name}:{f.stat().st_mtime_ns}")
                done = self.paths.signals / "consumed"
                done.mkdir(exist_ok=True)
                f.rename(done / f.name)

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
                       session=attempt.session, repairs=attempt.repairs)

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

    RENDER_FIELDS = ("inputs", "run", "prompt", "system", "script", "args", "message", "env", "cwd", "stage_in",
                     "retrieve", "call", "files", "resources", "signal", "prelude", "modules", "python", "pythonpath",
                     "harness", "token")

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
        if not deps:
            return "go", None, None
        stats = {d: st.nodes[d].status if d in st.nodes else "pending" for d in deps}
        if any(s not in TERMINAL_NODE for s in stats.values()):
            return "wait", None, None
        if spec.get("foreach") is not None:  # a collector judges its own children (all_done semantics)
            deps = [d for d in deps if g.nodes[d].get("expanded_from") != nid]
            if not deps:
                return "go", None, None

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

    def _schedule(self, st: RunState, rep: TickReport) -> list[str]:  # noqa: C901
        g = st.graph()
        started: list[str] = []
        defaults = st.plan.get("defaults") or {}
        limit = int(defaults.get("concurrency") or 4)
        active_local = sum(1 for nid, ns in st.nodes.items() if ns.status == "running" and nid in g.nodes
                           and g.nodes[nid].get("kind") in LOCAL_KINDS)
        changed = True
        while changed:  # skipping a node can unblock others in the same tick
            changed = False
            st = self.state()
            g = st.graph()
            for nid in g.topo():
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
                kind = spec.get("kind")
                if kind in LOCAL_KINDS and active_local >= limit:
                    rep.waiting_on.append(f"{nid}: concurrency limit {limit}")
                    continue
                if kind == "job":
                    cl = spec.get("cluster")
                    cap = int(((st.plan.get("clusters") or {}).get(cl) or {}).get("max_jobs") or 50)
                    busy = sum(1 for x, xs in st.nodes.items() if xs.status == "running" and x in g.nodes
                               and g.nodes[x].get("kind") == "job" and g.nodes[x].get("cluster") == cl)
                    if busy >= cap:
                        rep.waiting_on.append(f"{nid}: cluster {cl} at max_jobs={cap}")
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
        if spec.get("kind") == "function":  # what the node does includes the code it calls
            dirs = [str(Path(str(p)).expanduser()) for p in (rendered.get("pythonpath") or [])]
            dirs.append((st.plan.get("_source") or {}).get("dir") or st.meta.get("plan_dir") or ".")
            code = source_fingerprint(str(rendered.get("call") or "").partition(":")[0], dirs)
            if code:
                dh = digest({"decl": dh, "code": code})
        ih = digest({"inputs": inputs, "upstream": upstream, "bind": spec.get("bind"),
                     "prompt": rendered.get("prompt"), "script": rendered.get("script"), "run": rendered.get("run"),
                     "args": rendered.get("args")})
        # early cut-off / cache: a stale node whose definition and inputs are unchanged keeps its result
        prev = ns.result
        if prev is not None and spec.get("cache", True) and spec.get("kind") not in ("gate", "wait") \
                and not ns.force_next and prev.decl_hash == dh \
                and prev.input_hash == ih:
            self.emit("node.succeeded", {"attempt": n, "outputs": prev.outputs, "files": prev.files,
                                         "summary": prev.summary, "rationale": prev.rationale,
                                         "reused_from": f"a{prev.n}", "decl_hash": dh, "input_hash": ih,
                                         "usage": {}}, node=nid, attempt=n)
            return True
        hit = self._reuse_lookup(st, nid, dh, ih)
        if hit is not None:
            src_run, a = hit
            self.emit("node.succeeded", {"attempt": n, "outputs": a.outputs, "files": a.files, "summary": a.summary,
                                         "rationale": a.rationale, "reused_from": f"{src_run}:{nid}#a{a.n}",
                                         "amendment": a.amendment, "decl_hash": dh, "input_hash": ih, "usage": {}},
                      node=nid, attempt=n)
            if a.amendment:  # a recorded agent decision included a plan change: replay it faithfully
                self._agent_amendment(nid, spec, a.amendment)
            return True
        adir = self.paths.attempt_dir(nid, n)
        adir.mkdir(parents=True, exist_ok=True)
        if rendered.get("cwd"):
            wd = Path(str(rendered["cwd"])).expanduser()
            if not wd.is_absolute():
                wd = Path(st.meta.get("plan_dir") or ".") / wd
        else:
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
                                   "executor": rendered.get("harness", {}).get("name") if kind == "agent" else kind},
                  node=nid, attempt=n, key=f"node.started:{nid}#a{n}")
        if kind == "gate":
            msg = rendered.get("message") or f"Approve {nid}?"
            self._request_gate(f"{nid}#a{n}", subject="node", node=nid, message=str(msg),
                               decisions=list(rendered.get("decisions") or ["approve", "reject"]))
            return True
        if kind == "wait":
            tsec = parse_duration(rendered.get("timer"))
            dsec = parse_duration(rendered.get("deadline"))
            base = now()
            import datetime as dt
            self.emit("wait.armed", {"signal": rendered.get("signal"),
                                     "timer_at": (base + dt.timedelta(seconds=tsec)).isoformat() if tsec else None,
                                     "deadline_at": (base + dt.timedelta(seconds=dsec)).isoformat() if dsec else None,
                                     "since_seq": st.last_seq, "token": rendered.get("token")},
                      node=nid, attempt=n)
            return True
        st2 = self.state()
        attempt = st2.nodes[nid].last
        ctx = self._ctx(st2, rendered, attempt)
        try:
            handle = self.executors[kind].start(ctx)
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
        if items is not None and items != old_items:
            if any(st.nodes.get(c, NodeState(c)).status in ("running", "waiting", "retrying") for c in children):
                return False  # let in-flight children finish first
            base_needs = [d for d in (spec.get("needs") or []) if d not in children]

            def child(i: int, item):
                c = {k: v for k, v in spec.items() if k not in ("foreach", "title")}
                c["id"] = f"{nid}[{i}]"
                c["title"] = f"{spec.get('title') or nid} [{i}]"
                c["needs"] = [d for d in planmod.effective_needs(spec) if d not in children]
                c["bind"] = {"item": item, "index": i}
                c["expanded_from"] = nid
                return c

            ops: list[dict] = []
            add = [child(i, it) for i, it in enumerate(items) if i >= len(children)]
            if add:
                ops.append({"op": "add", "nodes": add})
            for i, it in enumerate(items[:len(children)]):
                if it != old_items[i]:
                    cid = children[i]
                    done = st.nodes.get(cid, NodeState(cid)).status not in ("pending",)
                    ops.append({"op": "replace", "node": cid, "with": child(i, it), **({"supersede": True} if done else {})})
            drop = [c for c in children[len(items):]]
            if drop:
                ops.append({"op": "drop", "nodes": drop})
            ids = [f"{nid}[{i}]" for i in range(len(items))]
            ops.append({"op": "set_needs", "node": nid, "needs": base_needs + ids})
            status = {k: v.status for k, v in st.nodes.items()}
            aid = new_id("fx")
            why = (f"foreach expansion of {nid} over {len(items)} item(s)" if not children else
                   f"foreach {nid}: item list changed ({len(old_items)} → {len(items)}), re-expanded")
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
                                         "summary": oc.summary, "rationale": oc.rationale, "amendment": oc.amendment,
                                         "decl_hash": ns.last.decl_hash, "input_hash": ns.last.input_hash},
                      node=nid, attempt=n, key=f"node.done:{nid}#a{n}")
            if oc.amendment:
                self._agent_amendment(nid, spec, oc.amendment)
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
        budget = int(retry.get("max_attempts", 1))
        quota_failures = sum(1 for a in ns.attempts[ns.retry_base:] if (a.error or {}).get("error_class") == "quota_retry")
        used = ns.attempts_since_reset - quota_failures
        if oc.error_class == "quota_retry":
            # a usage limit is not a failure of the work: park until the reset (DEV_PLAN D8), bounded overall
            cap = int((self.state().plan.get("defaults") or {}).get("quota_max_retries") or 48)
            if quota_failures < cap:
                retryable, used, budget = True, 0, 1
        self.emit("node.failed", {"error_class": oc.error_class, "message": oc.message, "retryable": retryable,
                                  "usage": oc.usage, "outputs": oc.outputs or None, "details": oc.details or None},
                  node=nid, attempt=n, key=f"node.done:{nid}#a{n}")
        if retryable and used < budget:
            back = parse_duration(retry.get("backoff") or "10s") or 0
            delay = back * (2 ** max(0, used - 1))
            ra = (oc.details or {}).get("retry_after_s")
            if ra:
                delay = max(delay, float(ra))
            elif oc.error_class == "quota_retry":
                delay = max(delay, 900.0)  # no reset time given: look again in 15 minutes
            import datetime as dt
            nb = (now() + dt.timedelta(seconds=delay)).isoformat()
            why = ("quota_retry: usage limit reached; waiting for the reset" if oc.error_class == "quota_retry"
                   else f"{oc.error_class}: attempt {used}/{budget} failed")
            self.emit("node.retry_scheduled", {"next_attempt": n + 1, "not_before": nb, "reason": why},
                      node=nid, attempt=n)

    def _agent_amendment(self, nid: str, spec: dict, amendment: dict) -> None:
        """An agent's proposed plan change. Untrusted input: validated, policy-checked on its *effect*,
        and never allowed to crash the tick or to widen the grant that allowed it."""
        try:
            eff = (spec.get("effects") or {}).get("amend")
            ops = amendment.get("ops") if isinstance(amendment, dict) else None
            rationale = str(amendment.get("rationale") or "") if isinstance(amendment, dict) else ""
            if not eff:
                self.emit("run.note", {"text": f"{nid} proposed an amendment but has no `effects.amend` permission;"
                                               " ignored", "rationale": rationale}, node=nid)
                return
            if not ops:
                return
            well_formed = isinstance(ops, list) and all(isinstance(o, dict) for o in ops)
            auto = well_formed and self._within_policy(eff if isinstance(eff, dict) else {}, ops)
            if not well_formed:
                ops = ops if isinstance(ops, list) else [ops]
            self._propose_locked(ops, rationale, by=f"agent:{nid}", source_node=nid, auto=auto)
        except planmod.PlanInvalid:
            pass  # recorded as rejected with its issues
        except Exception as exc:  # noqa: BLE001
            self.emit("run.note", {"text": f"amendment from {nid} could not be processed: {type(exc).__name__}: {exc}"},
                      node=nid)

    def _within_policy(self, policy: dict, ops: list) -> bool:
        """Judge the amendment by what it *does* to the plan, not by how the ops are spelled (H2/H3)."""
        if policy.get("auto_approve") is not True:
            return False
        allowed_ops = set(policy.get("ops") or ["add", "detour"])
        if any(op.get("op") not in allowed_ops for op in ops):
            return False
        st = self.state()
        try:
            new_plan, effects = planmod.apply_amendment(st.plan, ops, {k: v.status for k, v in st.nodes.items()})
        except planmod.PlanInvalid:
            return False
        by_id = {n["id"]: n for n in new_plan["nodes"]}
        touched = [by_id[x] for x in list(effects.get("added", [])) + list(effects.get("changed", [])) if x in by_id]
        added = [by_id[x] for x in effects.get("added", []) if x in by_id]
        if policy.get("max_nodes") is not None and len(added) > int(policy["max_nodes"]):
            return False
        if policy.get("kinds") and any(n.get("kind") not in set(policy["kinds"]) for n in added):
            return False
        replaced = [by_id[c] for c in effects.get("changed", []) if c in by_id and c not in effects.get("added", [])]
        if policy.get("kinds") and any(n.get("kind") not in set(policy["kinds"]) for n in replaced
                                       if any(op.get("op") == "replace" and op.get("node") == n["id"] for op in ops)):
            return False
        if any((n.get("effects") or {}).get("amend") for n in touched):
            return False  # a grant can never mint a new (possibly wider) grant without a human
        if effects.get("removed") or effects.get("stale"):
            return False  # dropping or superseding work always needs a human
        return True

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
        if st.status in TERMINAL_RUN or st.status == "parked":
            self.emit("run.reopened", {"reason": f"amendment {aid} approved", "by": by})

    # ------------------------------------------------------------ gates
    def _resolve_node_gate(self, st: RunState, ns: NodeState, g) -> None:
        spec = st.node_spec(ns.id)
        n = ns.last.n
        approve_value = spec.get("approve_value") or (spec.get("decisions") or ["approve"])[0]
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

    # ------------------------------------------------------------ waits
    def _poll_wait(self, st: RunState, nid: str, ns: NodeState, spec: dict) -> bool:
        w = (ns.last.progress or {}).get("wait") or {}
        n = ns.last.n
        name = w.get("signal")
        since = w.get("since_seq", 0)
        if name:
            # a signal is delivered once: the earliest one with this name not yet consumed by another wait
            # (signals sent before the wait was armed are kept, not lost)
            consumed = {a.outputs.get("signal_seq") for x in st.nodes.values() for a in x.attempts
                        if a.status == "succeeded" and isinstance(a.outputs, dict)}
            token = w.get("token")
            for s in st.signals:
                if s.get("name") != name or s.get("seq") in consumed:
                    continue
                if token and s.get("token") != token:
                    continue
                self.emit("node.succeeded", {"outputs": {"signal": name, "data": s.get("data"), "by": s.get("by"),
                                                         "expired": False, "signal_seq": s.get("seq")},
                                             "summary": f"signal {name!r} received from {s.get('by')}"},
                          node=nid, attempt=n)
                return True
        t = w.get("timer_at")
        if t and parse_iso(t) <= now():
            self.emit("node.succeeded", {"outputs": {"signal": None, "data": None, "expired": False, "timer": True},
                                         "summary": "timer elapsed"}, node=nid, attempt=n)
            return True
        d = w.get("deadline_at")
        if d and parse_iso(d) <= now():
            self.emit("wait.expired", {"deadline_at": d}, node=nid, attempt=n)
            self.emit("node.succeeded", {"outputs": {"signal": name, "data": None, "expired": True},
                                         "summary": f"deadline passed without signal {name!r}"}, node=nid, attempt=n)
            return True
        return False

    def _signal_seq(self, s: dict) -> int:
        # signals keep their journal position via the fold order; recover seq lazily
        return s.get("seq", 10**12)

    # ------------------------------------------------------------ cancellation & status
    def _cancel_everything(self, st: RunState) -> None:
        for nid, ns in st.nodes.items():
            if ns.status in ("running", "waiting") and ns.last and ns.last.status == "running":
                try:
                    spec = self._attempt_spec(nid, ns.last)
                    if spec.get("kind") not in ("gate", "wait"):
                        self.executors[spec["kind"]].cancel(self._ctx(st, spec, ns.last))
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
        for ns in waiting:
            if ns.gate_id is None:
                rep.waiting_on.append(f"{ns.id}: waiting for signal/timer")
        if rep.running:
            rep.status = "running"
            has_job = any(g.nodes[n].get("kind") == "job" for n in rep.running if n in g.nodes)
            has_local = any(g.nodes[n].get("kind") in LOCAL_KINDS for n in rep.running if n in g.nodes)
            rep.next_poll_s = 1.0 if has_local else (15.0 if has_job else 2.0)
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
            timed = any((ns.last.progress or {}).get("wait", {}).get("timer_at") or
                        (ns.last.progress or {}).get("wait", {}).get("deadline_at")
                        for ns in waiting if ns.last)
            rep.status = "parked"
            rep.next_poll_s = 30.0 if timed else 0
            reason = "; ".join(rep.waiting_on[:4]) or "waiting"
            if st.status != "parked" or st.status_reason != reason:
                self.emit("run.parked", {"reason": reason, "timed": bool(timed)})
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
