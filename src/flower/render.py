"""Human-readable views: plan overview (what you approve), status table, node detail, timeline.

Design goal: a person who has never seen flower can read any of these and answer "what is this
workflow, where is it, why did it do that, and what should I do next?".
"""
from __future__ import annotations

import json
import os
import re
import sys

from .plan import Graph, on_cluster
from .rundir import RunPaths
from .state import NodeState, RunState
from .util import (answer_cmd, first_line, fmt_duration, local_clock, local_stamp, local_zone, parse_iso,
                   seconds_since, short, tail_text, truncate)

ICON = {"succeeded": "✓", "failed": "✗", "running": "●", "waiting": "◆", "pending": "○", "retrying": "↻",
        "skipped": "–", "cancelled": "■"}
COLOR = {"succeeded": "32", "failed": "31", "running": "36", "waiting": "33", "pending": "2", "retrying": "35",
         "skipped": "2", "cancelled": "31", "parked": "33", "awaiting_approval": "33", "rejected": "31"}


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    return stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(text: str, status: str, color: bool) -> str:
    code = COLOR.get(status)
    return f"\x1b[{code}m{text}\x1b[0m" if color and code else text


def bold(text: str, color: bool) -> str:
    return f"\x1b[1m{text}\x1b[0m" if color else text


def kind_label(spec: dict) -> str:
    k = spec.get("kind", "?")
    if on_cluster(spec):  # a shell step sent to a cluster
        return f"{k}@{spec.get('cluster')}"
    return k


_ITEM_REF = re.compile(r"\$\{(item(?:\.[A-Za-z_][\w]*)*|index)\}")


def bound(text, spec: dict):
    """A foreach item's title or description with its own ${item...} / ${index} filled in (display only)."""
    b = spec.get("bind")
    if not isinstance(text, str) or not b or "${" not in text:
        return text

    def sub(m):
        ref = m.group(1)
        if ref == "index":
            return str(b.get("index"))
        v = b.get("item")
        for part in ref.split(".")[1:]:
            if not isinstance(v, dict) or part not in v:
                return m.group(0)
            v = v[part]
        return v if isinstance(v, str) else json.dumps(v)
    return _ITEM_REF.sub(sub, text)


def display_title(spec: dict, nid: str | None = None) -> str:
    return bound(spec.get("title"), spec) or nid or spec.get("id") or ""


def command_text(spec: dict) -> str:
    """What the step runs, in full (the UI's "Does" section): the shell script, or a gate's question."""
    k = spec.get("kind")
    if k == "shell":
        return str(spec.get("run") or "")
    if k == "gate":
        return str(spec.get("message") or "")
    return ""


def what(spec: dict) -> str:
    k = spec.get("kind")
    if spec.get("title") and spec.get("title") != spec.get("id"):
        return first_line(display_title(spec), 90)
    if spec.get("description"):
        return first_line(spec["description"], 90)
    if k == "shell":
        return "$ " + first_line(spec.get("run"), 88)
    if k == "gate":
        return "human decision: " + first_line(spec.get("message"), 70)
    return ""


# ====================================================================== plan overview

def plan_overview(plan: dict, inputs: dict | None = None) -> str:
    g = Graph(plan)
    depth = g.depth()
    out = [f"PLAN  {plan.get('title') or plan['id']}  ({plan['id']}, {len(g.order)} nodes)"]
    if plan.get("description"):
        out += ["", plan["description"].strip()]
    if inputs:
        out += ["", "Inputs:"] + [f"  {k} = {truncate(json.dumps(v, default=str), 100)}" for k, v in inputs.items()]
    out += ["", "Steps (in dependency order):"]
    for i, nid in enumerate(g.topo(), 1):
        spec = g.nodes[nid]
        deps = g.needs[nid]
        ind = "  " * depth.get(nid, 0)
        line = f"  {i:>2}. {ind}{nid:<{max(4, 22 - len(ind))}} {kind_label(spec):<20} {what(spec)}"
        out.append(line.rstrip())
        extras = []
        if deps:
            extras.append("after " + ", ".join(deps))
        if spec.get("when"):
            extras.append(f"only if {spec['when']}")
        if spec.get("foreach") is not None:
            extras.append(f"for each item in {spec['foreach'] if isinstance(spec['foreach'], str) else 'list'}")
        if spec.get("kind") == "gate" and spec.get("on_reject"):
            extras.append(f"on reject: redo {', '.join(spec['on_reject'].get('rerun') or [])}")
        if spec.get("outputs"):
            extras.append("returns " + ", ".join(spec["outputs"]))
        r = spec.get("retry") or {}
        if int(r.get("max_attempts", 1)) > 1 and spec.get("kind") != "gate":
            extras.append(f"up to {r['max_attempts']} attempts")
        if extras:
            out.append("      " + " " * len(ind) + "· " + "; ".join(extras))
    clusters = plan.get("clusters") or {}
    if clusters:
        out += ["", "Compute:"] + [f"  {n}: {c.get('transport', 'local')}"
                                   + (f" via {c['host']}" if c.get("host") else "")
                                   + (" (no scheduler)" if c.get("scheduler") == "none" else " (slurm)")
                                   + (", installs nothing" if c.get("install") == "never" else "")
                                   for n, c in clusters.items()]
    from .plan import warnings as plan_warnings
    missing = [w.split("'")[1] for w in plan_warnings(plan) if "'" in w]
    if missing:
        out += ["", "Steps without a description (what they establish, how to read the result): " + ", ".join(missing)]
    return "\n".join(out)


# ====================================================================== status

def node_activity(st: RunState, paths: RunPaths, ns: NodeState, spec: dict) -> str:
    a = ns.last
    if ns.status == "succeeded" and ns.result:
        r = ns.result
        tag = f"(reused {r.reused_from}) " if r.reused_from else ""
        return tag + (r.summary or "done")
    if ns.status == "skipped":
        return f"skipped: {ns.skipped_reason or ''}"
    if ns.status == "pending" and spec.get("foreach") is not None:   # a collector waits for its items
        kids = [n for n, s in st.graph().nodes.items() if s.get("expanded_from") == spec.get("id")]
        if kids:
            done = sum(1 for k in kids if (st.nodes.get(k) or NodeState(k)).status in ("succeeded", "failed",
                                                                                    "skipped", "cancelled"))
            return f"waiting for its items: {done}/{len(kids)} finished"
    if ns.status == "failed" and a and a.error:
        return f"{a.error.get('error_class')}: {first_line(a.error.get('message'), 120)}"
    if ns.status == "retrying":
        when = ns.retry_not_before
        wait = ""
        if when:
            secs = -seconds_since(when)
            wait = f" in {fmt_duration(max(0, secs))}"
        err = (a.error or {}).get("error_class") if a else ""
        return f"retry #{ns.next_attempt}{wait} (after {err})"
    if ns.status == "waiting":
        g = st.gates.get(ns.gate_id) if ns.gate_id else None
        if g:
            return f"needs your decision ({' / '.join(g.decisions)}) — see below"
        return "waiting"
    if ns.status == "running" and a:
        if on_cluster(spec):
            from .hpc import scheduler_for
            j = a.job or {}
            sched = scheduler_for((st.plan.get("clusters") or {}).get(spec.get("cluster")) or {})
            if not j.get("job_id") and j.get("waiting_for"):
                return f"waiting for {j['waiting_for']}"
            if not j.get("job_id"):
                e = j.get("last_remote_error")
                return ("submitting…" if sched.NAME == "slurm" else "starting…") + \
                    (f" (retrying after error: {first_line(e.get('error'), 60)})" if e else "")
            live = tail_text(paths.attempt_dir(ns.id, a.n) / "live.txt", 300).strip()
            live = f" · {live}" if live else ""
            if sched.NAME == "none":
                return f"{j.get('state') or 'RUNNING'} · pid {j['job_id']} on {spec.get('cluster')}{live}"
            return f"{j.get('state') or 'QUEUED'} · slurm job {j['job_id']}" + (f" ({j.get('sched')})" if j.get("sched") else "") + live
        out = tail_text(paths.attempt_dir(ns.id, a.n) / "proc" / "stdout.log", 400).strip().splitlines()
        return first_line(out[-1], 100) if out else "running…"
    if ns.stale and a:                  # a step that never ran is just pending, not "again"
        return f"queued again ({first_line(ns.stale_reason, 80)})"
    return ""


def node_time(ns: NodeState) -> str:
    a = ns.last
    if not a:
        return ""
    if a.ended_at:
        return fmt_duration(a.duration_s)
    return fmt_duration(seconds_since(a.started_at))


COLLAPSE_ITEMS = 12   # a foreach with more items is summarised; running / failed items stay listed


def status_view(st: RunState, paths: RunPaths, color: bool = False, width: int | None = None,
                all_items: bool = False) -> str:
    g = st.graph()
    depth = g.depth()
    width = width or 140
    elapsed = (parse_iso(st.completed_at) - parse_iso(st.created_at)).total_seconds() if st.completed_at else seconds_since(st.created_at)
    counts = st.counts()
    head = (f"{bold(st.title or st.plan_id, color)}  ·  run {st.run_id}  ·  "
            f"{paint(st.status.upper().replace('_', ' '), st.status, color)}  ·  {fmt_duration(elapsed)}")
    meta = [f"plan generation {st.generation} ({short(st.digest)})",
            f"{counts.get('succeeded', 0)}/{len(g.order)} done"]
    if counts.get("failed"):
        meta.append(f"{counts['failed']} failed")
    if counts.get("running"):
        meta.append(f"{counts['running']} running")
    lines = [head, "  " + "  ·  ".join(meta)]
    if st.status_reason and st.status in ("failed", "parked", "rejected", "cancelled"):
        lines.append("  " + paint(first_line(st.status_reason, 120), st.status, color))
    lines.append("")
    name_w = min(34, max(12, max((len(n) + 2 * depth.get(n, 0) for n in g.order), default=12) + 1))
    lines.append(f"     {'NODE':<{name_w}} {'KIND':<20} {'TIME':>7}  RESULT / ACTIVITY")
    kids: dict[str, list[str]] = {}
    for nid in g.order:
        p = g.nodes[nid].get("expanded_from")
        if p:
            kids.setdefault(p, []).append(nid)
    big = {p for p, ks in kids.items() if len(ks) > COLLAPSE_ITEMS and not all_items}
    # which items of a big foreach stay listed: active work first (up to 8), then failures (up to 5)
    visible: set[str] = set()
    for p in big:
        st_of = lambda k: (st.nodes.get(k) or NodeState(k)).status  # noqa: E731
        visible.update([k for k in kids[p] if st_of(k) in ("running", "retrying", "waiting")][:8])
        visible.update([k for k in kids[p] if st_of(k) == "failed"][:5])
    for nid in g.topo():
        spec = g.nodes[nid]
        ns = st.nodes.get(nid) or NodeState(nid)
        parent = spec.get("expanded_from")
        if parent in big and nid not in visible:
            continue
        name = ("  " * depth.get(nid, 0) + nid)[:name_w]
        att = f"#{ns.last.n}" if ns.last and ns.last.n > 1 else ""
        act = node_activity(st, paths, ns, spec)
        if parent:   # an item says which one it is: its title when the step's title is templated (`${item.case}`)
            t = re.sub(r"\s*\[\d+\]$", "", str(spec.get("title") or ""))
            if t and t not in (parent, str(g.nodes.get(parent, {}).get("title") or "")):
                act = t + (f" · {act}" if act else "")
        room = max(30, width - name_w - 36)
        row = f"  {ICON.get(ns.status, '?')}  {name:<{name_w}} {kind_label(spec)[:20]:<20} {node_time(ns):>7}  {truncate(act, room)}"
        lines.append(paint(row, ns.status, color) if ns.status in ("failed", "running", "waiting", "retrying") else
                     (paint(row, "skipped", color) if ns.status in ("pending", "skipped") else row))
        if att:
            lines[-1] += f"  ({att})"
        if nid in big:
            c: dict[str, int] = {}
            for k in kids[nid]:
                ks = (st.nodes.get(k) or NodeState(k)).status
                c[ks] = c.get(ks, 0) + 1
            order = ("succeeded", "running", "retrying", "waiting", "failed", "cancelled", "skipped", "pending")
            parts = [f"{c[k]} {k}" for k in order if c.get(k)]
            lines.append(f"       {'':<{name_w}} {len(kids[nid])} items: " + ", ".join(parts)
                         + f"   (all: flower status {st.run_id} --items)")
    lines += next_steps(st)
    return "\n".join(lines)


def next_steps(st: RunState) -> list[str]:
    rid = st.run_id
    out: list[str] = []
    gates = st.open_gates()
    if gates:
        out += ["", "Waiting for you:"]
        for g in gates:
            first = first_line(g.message, 100)
            out.append(f"  • gate {g.id}: {first}")
            out.append(f"      flower show {rid} {g.id}            # read it in full")
            out.append(f"      {answer_cmd(rid, g.id, g.decisions)}")
    if st.status == "failed":
        failed = [n for n, s in st.nodes.items() if s.status == "failed"]
        out += ["", "Next:"] + [f"  flower show {rid} {n}        # see why {n} failed" for n in failed[:3]]
        out += [f"  flower rerun {rid} {failed[0]}    # retry it and everything after it"] if failed else []
    elif st.status in ("running", "parked") and not gates:
        out += ["", f"Next:  flower status {rid} --follow   (live view)   ·   flower log {rid}   (what happened)"]
    elif st.status == "succeeded":
        out += ["", f"Done.  flower ui (the whole record)   ·   flower export {rid} STEP   (a protocol to re-run it)"]
    return out


# ====================================================================== node detail

def node_detail(st: RunState, paths: RunPaths, nid: str, color: bool = False) -> str:
    spec = st.node_spec(nid)
    ns = st.nodes.get(nid) or NodeState(nid)
    g = st.graph()
    out = [f"{bold(nid, color)}  [{kind_label(spec)}]  {paint(ns.status.upper(), ns.status, color)}",
           f"  {display_title(spec, nid) if spec.get('title') != nid else ''}".rstrip()]
    if spec.get("description"):
        out.append("  " + spec["description"].strip())
    out.append(f"  needs: {', '.join(g.needs[nid]) or '-'}   ·   used by: {', '.join(g.children.get(nid, [])) or '-'}")
    if spec.get("when"):
        out.append(f"  condition: {spec['when']}")
    if ns.stale_reason and ns.status == "pending":
        out.append(f"  re-queued because: {ns.stale_reason}")
    if ns.status in ("pending", "retrying"):
        blockers = [(d, (st.nodes.get(d) or NodeState(d)).status) for d in g.needs[nid]
                    if (st.nodes.get(d) or NodeState(d)).status not in ("succeeded", "failed", "skipped", "cancelled")]
        if ns.status == "retrying":
            out.append(f"  why not running: retry scheduled for {ns.retry_not_before}")
        elif blockers:
            out.append("  why not running: waiting for " + ", ".join(f"{d} ({s})" for d, s in blockers))
        elif st.status in ("awaiting_approval",):
            out.append("  why not running: the plan has not been approved yet")
        else:
            out.append("  why not running: ready — it starts on the next tick (concurrency or cluster limits permitting)")
    if ns.skipped_reason and ns.status == "skipped":
        out.append(f"  skipped because: {ns.skipped_reason}")
    for a in ns.attempts:
        adir = paths.attempt_dir(nid, a.n)
        label = a.status
        if a.status == "running" and spec.get("kind") == "gate":
            label = "waiting for a decision"
        out += ["", bold(f"attempt {a.n}", color) + f"  {label}  started {local_stamp(a.started_at)}"
                + (f"  took {fmt_duration(a.duration_s)}" if a.duration_s is not None else "")
                + (f"  (reused result of {a.reused_from})" if a.reused_from else "")]
        out.append(f"  dir: {adir}")
        if a.inputs:
            out.append("  inputs: " + truncate(json.dumps(a.inputs, default=str), 300))
        if a.job:
            j = a.job
            what = "process" if j.get("scheduler") == "none" else f"{j.get('scheduler') or 'slurm'} job"
            out.append(f"  {what} {j.get('job_id', '-')} on {j.get('cluster')}  state={j.get('state')}"
                       f"  dir={j.get('job_dir')}" + (f"  remote errors={j['remote_errors']}" if j.get("remote_errors") else ""))
        if a.summary:
            out.append(f"  summary: {a.summary}")
        if a.rationale:
            out.append("  rationale: " + truncate(a.rationale, 600))
        if a.outputs:
            out.append("  outputs: " + truncate(json.dumps(a.outputs, default=str, ensure_ascii=False), 800))
        for k, f in (a.files or {}).items():
            out.append(f"  file {k}: {f.get('path')}" + (f"  ({f['bytes']} B, sha {f['sha256'][:10]})" if f.get("sha256") else ""))
        if a.error:
            out.append(paint(f"  error [{a.error.get('error_class')}]: {a.error.get('message')}", "failed", color))
            det = a.error.get("details") or {}
            for key in ("stderr", "stderr_tail", "text_tail", "stdout_tail"):
                if det.get(key):
                    out.append(f"  {key}:\n" + "\n".join("    " + l for l in str(det[key]).strip().splitlines()[-8:]))
        if a.status == "running":
            for name in ("stdout.log", "stderr.log"):
                t = tail_text(adir / "proc" / name, 800).strip()
                if t:
                    out.append(f"  {name} (tail):\n" + "\n".join("    " + l for l in t.splitlines()[-6:]))
    gid = ns.gate_id
    if gid and gid in st.gates and st.gates[gid].status == "open":
        gt = st.gates[gid]
        out += ["", "QUESTION FOR YOU:", gt.message, "",
                f"answer:  {answer_cmd(st.run_id, gt.id, gt.decisions)}"]
    return "\n".join(out)


def gate_detail(st: RunState, gid: str) -> str:
    g = st.gates[gid]
    out = [f"gate {g.id}  ({g.subject})  {g.status}", "", g.message, ""]
    if g.status == "open":
        out.append(f"answer:  {answer_cmd(st.run_id, g.id, g.decisions)}")
    else:
        out.append(f"answered {g.decision!r} by {g.by} at {g.answered_at}" + (f": {g.text}" if g.text else ""))
    return "\n".join(out)


# ====================================================================== timeline

def describe_event(ev: dict) -> str | None:
    t, p, n = ev["eventType"], ev.get("payload") or {}, ev.get("nodeId")
    att = p.get("attempt")
    a = f"{n}#{att}" if n and att else (n or "")
    if t == "run.created":
        return f"run created from plan {p.get('plan_id')} by {ev.get('actor')} on {p.get('host')}"
    if t == "plan.proposed":
        return f"plan proposed ({len(p['plan']['nodes'])} nodes, digest {short(p.get('digest'))})"
    if t == "plan.approved":
        return f"plan APPROVED by {p.get('by')}" + (f": {p['note']}" if p.get("note") else "")
    if t == "plan.rejected":
        return f"plan REJECTED by {p.get('by')}: {p.get('reason')}"
    if t == "run.started":
        return "run started" if not p.get("reason") else f"run active again ({p['reason']})"
    if t == "run.parked":
        return f"run parked: {p.get('reason')}"
    if t == "run.reopened":
        return f"run reopened: {p.get('reason')}"
    if t == "run.completed":
        return f"RUN {p.get('status', '').upper()}: {p.get('reason')}"
    if t == "run.cancel_requested":
        return f"cancel requested by {p.get('by')}"
    if t == "run.note":
        return f"note by {ev.get('actor')}: {p.get('text')}"
    if t == "node.started":
        if p.get("kind") == "foreach":
            return f"{n}: collecting foreach results"
        return f"{a} started" + (f" ({p.get('executor')})" if p.get("executor") else "")
    if t == "node.progress":
        if p.get("phase") == "launched":
            return None
        return f"{a}: {p.get('phase')}"
    if t == "node.succeeded":
        return f"{a} ✓ {first_line(p.get('summary'), 110)}" + (f" (reused {p['reused_from']})" if p.get("reused_from") else "")
    if t == "node.failed":
        return f"{a} ✗ {p.get('error_class')}: {first_line(p.get('message'), 110)}"
    if t == "node.retry_scheduled":
        return f"{n}: retry scheduled ({p.get('reason')})"
    if t == "node.skipped":
        return f"{n} skipped: {p.get('reason')}"
    if t == "node.cancelled":
        return f"{a} cancelled: {p.get('reason')}"
    if t == "node.stale":
        return f"{n} queued to run again: {p.get('reason')}"
    direct = p.get("scheduler") == "none"  # a process on a machine without a batch system (older events: Slurm)
    if t == "job.submit_intent":
        if direct:
            return f"{a}: starting a process on {p.get('cluster')} (no scheduler)"
        return f"{a}: submitting to {p.get('cluster')} as {p.get('submit_key')}"
    if t == "job.submitted":
        if direct:
            return f"{a}: process {p.get('job_id')} " + ("started" if p.get("via") == "submitted" else "re-attached")
        return f"{a}: slurm job {p.get('job_id')} " + ("submitted" if p.get("via") == "submitted" else f"re-attached ({p.get('via')})")
    if t == "job.observed":
        if direct:
            return f"{a}: process {p.get('state')}"
        return f"{a}: job {p.get('state')}" + (f" ({p.get('raw')})" if p.get("raw") and p.get("raw") != p.get("state") else "")
    if t == "job.exited":
        if direct:
            return f"{a}: process exited (exit code {p.get('ec')})"
        return f"{a}: job exited (exit code {p.get('ec')}" + (f", slurm {p.get('final')})" if p.get("final") else ")")
    if t == "job.remote_error":
        return f"{a}: cluster error during {p.get('op')}: {first_line(p.get('error'), 90)}"
    if t == "job.waiting":   # said once; the run waits quietly after that
        return f"{a}: waiting for {p.get('waiting_for')}" if p.get("limit_waits") == 1 else None
    if t == "job.lost":
        return f"{a}: job LOST — {p.get('why')}"
    if t == "job.retrieved" or t == "job.cancel_requested":
        return None
    if t == "gate.requested":
        subj = p.get("subject")
        return f"decision requested ({subj}{' ' + n if n else ''}): {first_line(p.get('message'), 90)}"
    if t == "gate.answered":
        return f"decision {p.get('gate_id')}: {p.get('decision')!r} by {p.get('by')}" + (f" — {first_line(p.get('text'), 90)}" if p.get("text") else "")
    if t == "plan.amendment.proposed":
        return f"plan change proposed by {p.get('proposed_by')}: {first_line(p.get('rationale'), 100)}"
    if t == "plan.amendment.approved":
        eff = p.get("effects") or {}
        return (f"plan change APPLIED → generation {p.get('generation')} by {p.get('by')}"
                + (f"; adds {', '.join(eff.get('added', [])[:6])}" if eff.get("added") else ""))
    if t == "plan.amendment.rejected":
        return f"plan change rejected by {p.get('by')}: {first_line(p.get('reason'), 100)}"
    if t in ("driver.reloaded", "driver.error"):
        return f"driver {t.split('.')[1]}: {p.get('reason') or p.get('error') or ''}".rstrip(": ")
    if t in ("driver.started", "driver.stopped"):
        return f"background driver {t.split('.')[1]} (pid {p.get('pid')})"
    return f"{t} {a}"


def timeline(events: list[dict], color: bool = False, node: str | None = None) -> str:
    lines = []
    t0 = parse_iso(events[0]["occurredAtIso"]) if events else None
    for ev in events:
        if node and ev.get("nodeId") != node:
            continue
        d = describe_event(ev)
        if not d:
            continue
        ts = local_clock(ev["occurredAtIso"])
        rel = fmt_duration((parse_iso(ev["occurredAtIso"]) - t0).total_seconds()) if t0 else ""
        actor = ev.get("actor", "system")
        who = "" if actor == "system" else f"  [{actor}]"
        status = ("failed" if "✗" in d or "LOST" in d or "FAILED" in d else "succeeded" if "✓" in d or "SUCCEEDED" in d
                  else "waiting" if "decision" in d else "")
        lines.append(f"{ts} +{rel:>6}  " + paint(d, status, color) + who)
    if lines:
        lines.insert(0, f"(times in {local_zone()})")
    return "\n".join(lines)
