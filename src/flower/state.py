"""Pure fold: events.jsonl -> RunState. No I/O besides what the caller passes in.

Every command (status, report, tick, …) starts by folding the log, so what you see is always
derivable from the journal alone — the property that makes runs replayable and auditable.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .plan import Graph
from .util import parse_iso

TERMINAL_NODE = ("succeeded", "failed", "skipped", "cancelled")
TERMINAL_RUN = ("succeeded", "failed", "cancelled", "rejected")


@dataclass
class Attempt:
    n: int
    started_at: str
    status: str = "running"          # running | succeeded | failed | cancelled
    ended_at: str | None = None
    decl_hash: str | None = None
    input_hash: str | None = None
    inputs: dict = field(default_factory=dict)
    handle: dict = field(default_factory=dict)    # executor launch info (pid, job id, …)
    workdir: str | None = None
    progress: dict = field(default_factory=dict)
    job: dict = field(default_factory=dict)       # hpc sub-state
    session: str | None = None
    repairs: int = 0
    outputs: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    summary: str | None = None
    rationale: str | None = None
    error: dict | None = None
    reused_from: str | None = None
    actor: str | None = None
    amendment: dict | None = None

    @property
    def duration_s(self) -> float | None:
        if not self.started_at:
            return None
        end = self.ended_at
        if not end:
            return None
        return (parse_iso(end) - parse_iso(self.started_at)).total_seconds()


@dataclass
class NodeState:
    id: str
    status: str = "pending"   # pending | running | waiting | retrying | succeeded | failed | skipped | cancelled
    attempts: list[Attempt] = field(default_factory=list)
    stale: bool = False
    stale_reason: str | None = None
    skipped_reason: str | None = None
    skip_cause: str | None = None
    retry_not_before: str | None = None
    next_attempt: int = 1
    gate_id: str | None = None
    rerun_count: int = 0     # how many times a gate's on_reject re-ran it
    retry_base: int = 0      # attempts made before the last reset (retry budget counts from here)
    state_series: int = 0    # which FLOWER_STATE_DIR the attempts use: new on a rerun, kept with --keep-state
    force_next: bool = False # next start must not reuse a cached result
    feedback: str | None = None  # last gate rejection text that sent this node back (${feedback})

    @property
    def attempts_since_reset(self) -> int:
        return len(self.attempts) - self.retry_base

    @property
    def last(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def result(self) -> Attempt | None:
        """Latest successful attempt (what downstream references resolve to)."""
        for a in reversed(self.attempts):
            if a.status == "succeeded":
                return a
        return None


@dataclass
class Gate:
    id: str
    subject: str            # node | plan | amendment | foreach
    node: str | None
    message: str
    decisions: list[str]
    requested_at: str
    status: str = "open"    # open | answered | withdrawn
    decision: str | None = None
    text: str | None = None
    by: str | None = None
    answered_at: str | None = None
    amendment_id: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Amendment:
    id: str
    proposed_at: str
    proposed_by: str
    ops: list
    rationale: str
    parent_generation: int
    status: str = "proposed"   # proposed | approved | rejected
    generation: int | None = None
    decided_by: str | None = None
    effects: dict = field(default_factory=dict)
    source_node: str | None = None
    diff: dict = field(default_factory=dict)


@dataclass
class RunState:
    run_id: str = ""
    created_at: str = ""
    plan_id: str = ""
    title: str = ""
    inputs: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    status: str = "awaiting_approval"
    status_reason: str | None = None
    generations: list[dict] = field(default_factory=list)   # [{generation, digest, plan, by, at, amendment}]
    approved: bool = False
    approved_by: str | None = None
    nodes: dict[str, NodeState] = field(default_factory=dict)
    gates: dict[str, Gate] = field(default_factory=dict)
    amendments: dict[str, Amendment] = field(default_factory=dict)
    signals: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    cancel_requested: bool = False
    completed_at: str | None = None
    last_seq: int = 0
    last_event_at: str | None = None
    drivers: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------ derived
    @property
    def plan(self) -> dict:
        return self.generations[-1]["plan"] if self.generations else {"nodes": []}

    @property
    def generation(self) -> int:
        return self.generations[-1]["generation"] if self.generations else 0

    @property
    def digest(self) -> str | None:
        return self.generations[-1]["digest"] if self.generations else None

    @property
    def base_digest(self) -> str | None:
        return self.generations[0]["digest"] if self.generations else None

    def graph(self) -> Graph:
        return Graph(self.plan)

    def node_spec(self, nid: str) -> dict:
        for n in self.plan["nodes"]:
            if n["id"] == nid:
                return n
        raise KeyError(nid)

    def open_gates(self) -> list[Gate]:
        return [g for g in self.gates.values() if g.status == "open"]

    def cost(self) -> dict:
        usd, tin, tout = 0.0, 0, 0
        priced = True
        for ns in self.nodes.values():
            for a in ns.attempts:
                u = a.usage or {}
                if u.get("cost_usd") is not None:
                    usd += float(u["cost_usd"])
                elif u.get("input_tokens"):
                    priced = False
                tin += int(u.get("input_tokens") or 0)
                tout += int(u.get("output_tokens") or 0)
        return {"usd": round(usd, 4), "input_tokens": tin, "output_tokens": tout, "complete": priced}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for nid in self.graph().order:
            st = self.nodes.get(nid, NodeState(nid)).status
            out[st] = out.get(st, 0) + 1
        return out

    def template_context(self) -> dict:
        nodes = {}
        for nid, ns in self.nodes.items():
            r = ns.result
            if r is not None:
                nodes[nid] = {"outputs": r.outputs, "files": {k: v.get("path") for k, v in r.files.items()},
                              "dir": r.workdir, "summary": r.summary or "", "attempt": r.n}
        # ${step.partial}: for a foreach step, its items' outputs so far (null where an item has not succeeded yet)
        kids: dict[str, list] = {}
        for n in self.plan.get("nodes") or []:
            if isinstance(n, dict) and n.get("expanded_from"):
                kids.setdefault(n["expanded_from"], []).append(((n.get("bind") or {}).get("index", 0), n["id"]))
        for parent, ks in kids.items():
            part = []
            for _, k in sorted(ks):
                r = self.nodes[k].result if k in self.nodes and self.nodes[k].status == "succeeded" else None
                part.append(r.outputs if r is not None else None)
            nodes.setdefault(parent, {})["partial"] = part
        # gate feedback is referenceable even while the gate itself is pending again (on_reject loops)
        for g in sorted(self.gates.values(), key=lambda g: g.answered_at or ""):
            if g.node and g.status == "answered":
                entry = nodes.setdefault(g.node, {})
                entry["feedback"] = g.text or ""
                entry["decision"] = g.decision
        for nid, ns in self.nodes.items():
            if ns.status == "pending" and nid in nodes:
                nodes[nid].setdefault("feedback", "")
        # inputs added to a running plan (amendment add_inputs) carry their value as the declaration's default
        ins = {k: d["default"] for k, d in (self.plan.get("inputs") or {}).items()
               if isinstance(d, dict) and "default" in d and k not in self.inputs}
        ins.update(self.inputs)
        return {"inputs": ins, "run": {"id": self.run_id, "dir": self.meta.get("run_dir")}, "nodes": nodes}


def _node(state: RunState, nid: str) -> NodeState:
    if nid not in state.nodes:
        state.nodes[nid] = NodeState(nid)
    return state.nodes[nid]


def _attempt(ns: NodeState, n: int | None) -> Attempt | None:
    if n is None:
        return ns.last
    for a in ns.attempts:
        if a.n == n:
            return a
    return None


def fold(events: list[dict]) -> RunState:
    st = RunState()
    for ev in events:
        apply(st, ev)
    return st


def apply(st: RunState, ev: dict) -> None:  # noqa: C901 - one switch, kept flat for readability
    t = ev["eventType"]
    p = ev.get("payload") or {}
    at = ev.get("occurredAtIso")
    nid = ev.get("nodeId")
    st.last_seq = ev.get("seq", st.last_seq)
    st.last_event_at = at

    if t == "run.created":
        st.run_id = ev["runId"]
        st.created_at = at
        st.plan_id = p.get("plan_id", "")
        st.title = p.get("title") or st.plan_id
        st.inputs = p.get("inputs") or {}
        st.meta = {k: v for k, v in p.items() if k not in ("inputs",)}
    elif t == "plan.proposed":
        st.generations = [{"generation": 0, "digest": p["digest"], "plan": p["plan"], "at": at,
                           "by": ev.get("actor"), "amendment": None}]
        for n in p["plan"]["nodes"]:
            _node(st, n["id"])
        st.status = "awaiting_approval"
    elif t == "plan.approved":
        st.approved = True
        st.approved_by = p.get("by") or ev.get("actor")
        if st.generations:
            st.generations[0]["approved_by"] = st.approved_by
            st.generations[0]["approved_at"] = at
    elif t == "plan.rejected":
        st.status = "rejected"
        st.status_reason = p.get("reason")
        st.completed_at = at
    elif t == "run.started":
        st.status = "running"
    elif t == "run.reopened":
        st.status = "running"
        st.cancel_requested = False
        st.status_reason = p.get("reason")
        st.completed_at = None
    elif t == "run.parked":
        st.status = "parked"
        st.status_reason = p.get("reason")
    elif t == "run.completed":
        st.status = p.get("status", "succeeded")
        st.status_reason = p.get("reason")
        st.completed_at = at
    elif t == "run.cancel_requested":
        st.cancel_requested = True
    elif t == "run.note":
        st.notes.append({"at": at, "by": ev.get("actor"), "text": p.get("text"), "node": nid})
    elif t in ("driver.started", "driver.stopped", "driver.reloaded", "driver.error"):
        st.drivers.append({"at": at, "event": t, **p})

    elif t == "node.started":
        ns = _node(st, nid)
        a = Attempt(n=p["attempt"], started_at=at, decl_hash=p.get("decl_hash"), input_hash=p.get("input_hash"),
                    inputs=p.get("inputs") or {}, handle=p.get("handle") or {}, workdir=p.get("workdir"),
                    actor=ev.get("actor"))
        ns.attempts = [x for x in ns.attempts if x.n != a.n] + [a]
        ns.next_attempt = a.n + 1
        ns.status = "running"
        ns.stale = False
        ns.force_next = False
        ns.retry_not_before = None
    elif t == "node.progress":
        ns = _node(st, nid)
        a = _attempt(ns, p.get("attempt"))
        if a:
            a.progress.update({k: v for k, v in p.items() if k != "attempt"})
            if p.get("handle"):
                a.handle.update(p["handle"])
    elif t == "agent.session":
        a = _attempt(_node(st, nid), p.get("attempt"))
        if a:
            a.session = p.get("session_id")
    elif t == "agent.repair":
        a = _attempt(_node(st, nid), p.get("attempt"))
        if a:
            a.repairs = p.get("n", a.repairs + 1)
            a.progress["repair_reason"] = p.get("reason")
    elif t == "agent.decision":
        a = _attempt(_node(st, nid), p.get("attempt"))
        if a:
            a.progress.setdefault("decisions", []).append(p)
    elif t.startswith("job."):
        ns = _node(st, nid)
        a = _attempt(ns, p.get("attempt"))
        if a:
            key = t.split(".", 1)[1]
            body = {k: v for k, v in p.items() if k != "attempt"}
            if key == "remote_error":
                op = body.get("op")
                if op in ("submit", "stage", "connect"):
                    a.job["remote_errors"] = a.job.get("remote_errors", 0) + 1
                    a.job["last_remote_error"] = body
                elif op == "retrieve":
                    a.job["retrieve_errors"] = a.job.get("retrieve_errors", 0) + 1
                else:
                    a.job["poll_errors"] = a.job.get("poll_errors", 0) + 1
                a.job["last_error"] = body
            elif key == "observed":
                a.job["state"] = body.get("state")
                a.job["sched"] = body.get("raw")
                a.job["observed_at"] = at
            else:
                a.job.update(body)
                a.job["phase"] = key
                if key == "submitted":
                    a.job["state"] = "QUEUED"
                if key == "lost":
                    a.job["state"] = "LOST"
                if key == "exited":
                    a.job["state"] = "EXITED"
    elif t == "node.succeeded":
        ns = _node(st, nid)
        a = _attempt(ns, p.get("attempt"))
        if a is None:  # reuse without a started attempt
            a = Attempt(n=p.get("attempt") or ns.next_attempt, started_at=at)
            ns.attempts.append(a)
            ns.next_attempt = a.n + 1
        a.status = "succeeded"
        a.ended_at = at
        a.outputs = p.get("outputs") or {}
        a.files = p.get("files") or {}
        a.usage = p.get("usage") or a.usage
        a.summary = p.get("summary")
        a.rationale = p.get("rationale")
        a.reused_from = p.get("reused_from")
        a.amendment = p.get("amendment")
        if p.get("decl_hash"):
            a.decl_hash = p["decl_hash"]
        if p.get("input_hash"):
            a.input_hash = p["input_hash"]
        ns.status = "succeeded"
        ns.stale = False
    elif t == "node.failed":
        ns = _node(st, nid)
        a = _attempt(ns, p.get("attempt"))
        if a:
            a.status = "failed"
            a.ended_at = at
            a.error = {k: v for k, v in p.items() if k not in ("attempt", "usage")}
            if p.get("usage"):
                a.usage = p["usage"]
        ns.status = "failed"
    elif t == "node.retry_scheduled":
        ns = _node(st, nid)
        ns.status = "retrying"
        ns.retry_not_before = p.get("not_before")
        ns.next_attempt = p.get("next_attempt", ns.next_attempt)
    elif t == "node.skipped":
        ns = _node(st, nid)
        ns.status = "skipped"
        ns.skipped_reason = p.get("reason")
        ns.skip_cause = p.get("cause") or ("failure" if str(p.get("reason", "")).startswith("upstream") else None)
    elif t == "node.cancelled":
        ns = _node(st, nid)
        a = _attempt(ns, p.get("attempt"))
        if a and a.status == "running":
            a.status = "cancelled"
            a.ended_at = at
            a.error = {"error_class": "cancelled", "message": p.get("reason")}
        ns.status = "cancelled"
    elif t == "node.stale":
        ns = _node(st, nid)
        ns.stale = True
        ns.stale_reason = p.get("reason")
        ns.status = "pending"
        ns.skipped_reason = None
        ns.retry_not_before = None
        ns.gate_id = None
        ns.retry_base = len(ns.attempts)
        if not p.get("keep_state"):   # a deliberate rerun starts a fresh state dir; --keep-state continues it
            ns.state_series = len(ns.attempts)
        ns.force_next = bool(p.get("force"))
        if p.get("feedback") is not None:
            ns.feedback = p["feedback"]
        if p.get("rerun_count") is not None:
            ns.rerun_count = p["rerun_count"]
    elif t == "gate.requested":
        g = Gate(id=p["gate_id"], subject=p.get("subject", "node"), node=nid, message=p.get("message", ""),
                 decisions=p.get("decisions") or ["approve", "reject"], requested_at=at,
                 amendment_id=p.get("amendment_id"), extra={k: v for k, v in p.items() if k in ("summary", "diff", "request_path")})
        st.gates[g.id] = g
        if nid and g.subject == "node":
            ns = _node(st, nid)
            ns.status = "waiting"
            ns.gate_id = g.id
    elif t == "gate.answered":
        g = st.gates.get(p["gate_id"])
        if g:
            g.status = "answered"
            g.decision = p.get("decision")
            g.text = p.get("text")
            g.by = p.get("by") or ev.get("actor")
            g.answered_at = at
    elif t == "wait.armed":
        ns = _node(st, nid)
        ns.status = "waiting"
        a = _attempt(ns, p.get("attempt"))
        if a:
            a.progress.update({"wait": {k: v for k, v in p.items() if k != "attempt"}})
    elif t == "signal.received":
        st.signals.append({"at": at, "seq": ev.get("seq"), **p})
    elif t == "wait.expired":
        pass
    elif t == "plan.amendment.proposed":
        st.amendments[p["amendment_id"]] = Amendment(
            id=p["amendment_id"], proposed_at=at, proposed_by=p.get("proposed_by") or ev.get("actor"),
            ops=p.get("ops") or [], rationale=p.get("rationale") or "", parent_generation=p.get("parent_generation", 0),
            source_node=p.get("source_node"), diff=p.get("diff") or {})
    elif t == "plan.amendment.approved":
        am = st.amendments.get(p["amendment_id"])
        if am:
            am.status = "approved"
            am.generation = p["generation"]
            am.decided_by = p.get("by") or ev.get("actor")
            am.effects = p.get("effects") or {}
        st.generations.append({"generation": p["generation"], "digest": p["digest"], "plan": p["plan"], "at": at,
                               "by": p.get("by") or ev.get("actor"), "amendment": p["amendment_id"]})
        for n in p["plan"]["nodes"]:
            _node(st, n["id"])
        for gone in (p.get("effects") or {}).get("removed", []):
            st.nodes.pop(gone, None)
    elif t == "plan.amendment.rejected":
        am = st.amendments.get(p["amendment_id"])
        if am:
            am.status = "rejected"
            am.decided_by = p.get("by") or ev.get("actor")
            am.effects = {"reason": p.get("reason")}


def snapshot(state: RunState) -> RunState:
    return copy.deepcopy(state)
