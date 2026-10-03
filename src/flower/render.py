"""Human-readable views: plan overview (what you approve), status table, node detail, timeline.

Design goal: a person who has never seen flower can read any of these and answer "what is this
workflow, where is it, why did it do that, and what should I do next?".
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .plan import Graph
from .rundir import RunPaths
from .state import TERMINAL_RUN, NodeState, RunState
from .util import first_line, fmt_duration, parse_iso, read_json, seconds_since, short, tail_text, truncate

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
    if k == "agent":
        h = spec.get("harness") or {}
        return f"agent:{h.get('name', '?')}" + (f"/{h['model']}" if h.get("model") else "")
    if k == "job":
        return f"job@{spec.get('cluster')}"
    if k == "function":
        return "function"
    return k


def what(spec: dict) -> str:
    k = spec.get("kind")
    if spec.get("title") and spec.get("title") != spec.get("id"):
        return first_line(spec["title"], 90)
    if spec.get("description"):
        return first_line(spec["description"], 90)
    if k == "agent":
        return first_line(spec.get("prompt"), 90)
    if k == "shell":
        return "$ " + first_line(spec.get("run"), 88)
    if k == "function":
        return f"{spec.get('call')}()"
    if k == "job":
        r = spec.get("resources") or {}
        res = ", ".join(f"{a}={r[a]}" for a in ("nodes", "ntasks", "time", "partition") if r.get(a))
        return first_line(spec.get("script"), 60) + (f"  [{res}]" if res else "")
    if k == "gate":
        return "human decision: " + first_line(spec.get("message"), 70)
    if k == "wait":
        return f"wait for signal {spec.get('signal')!r}" if spec.get("signal") else f"wait {spec.get('timer')}"
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
        if (spec.get("effects") or {}).get("amend"):
            a = spec["effects"]["amend"]
            extras.append("may propose plan changes" + (" (auto-approved within policy)" if isinstance(a, dict) and a.get("auto_approve") else " (needs approval)"))
        if spec.get("outputs"):
            extras.append("returns " + ", ".join(spec["outputs"]))
        r = spec.get("retry") or {}
        if int(r.get("max_attempts", 1)) > 1 and spec.get("kind") not in ("gate", "wait"):
            extras.append(f"up to {r['max_attempts']} attempts")
        if extras:
            out.append("      " + " " * len(ind) + "· " + "; ".join(extras))
    clusters = plan.get("clusters") or {}
    if clusters:
        out += ["", "Compute:"] + [f"  {n}: {c.get('transport', 'local')}"
                                   + (f" via {c['host']}" if c.get("host") else "") + " (slurm)" for n, c in clusters.items()]
    agents = {kind_label(s) for s in g.nodes.values() if s.get("kind") == "agent"}
    if agents:
        out += ["", "Agents used: " + ", ".join(sorted(agents))]
    return "\n".join(out)


def amendment_overview(rationale: str, ops: list, diff: dict, by: str) -> str:
    out = [f"PLAN CHANGE proposed by {by}", "", f"Why: {rationale or '(no rationale given)'}", ""]
    if diff.get("added"):
        out.append("Adds:     " + ", ".join(diff["added"]))
    if diff.get("removed"):
        out.append("Removes:  " + ", ".join(diff["removed"]))
    for c in diff.get("changed") or []:
        out.append(f"Changes:  {c['id']} ({', '.join(c['fields'])})")
    for op in ops:
        for nd in op.get("nodes") or ([op.get("with")] if op.get("with") else []):
            if isinstance(nd, dict):
                out.append(f"  + {nd.get('id')}: {nd.get('kind')} — {what(nd)}")
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
        w = (a.progress or {}).get("wait") if a else {}
        return f"waiting for signal {w.get('signal')!r}" if w and w.get("signal") else "waiting for timer"
    if ns.status == "running" and a:
        if spec.get("kind") == "job":
            j = a.job or {}
            if not j.get("job_id"):
                e = j.get("last_remote_error")
                return "submitting…" + (f" (retrying after error: {first_line(e.get('error'), 60)})" if e else "")
            return f"{j.get('state') or 'QUEUED'} · slurm job {j['job_id']}" + (f" ({j.get('sched')})" if j.get("sched") else "")
        if spec.get("kind") == "agent":
            live = read_json(paths.attempt_dir(ns.id, a.n) / "live.json") or {}
            turn = f" [repair {a.repairs}]" if a.repairs else ""
            act = live.get("last_action")
            return (f"{live.get('actions', 0)} actions" + (f" · {first_line(act, 80)}" if act else " · thinking…") + turn)
        out = tail_text(paths.attempt_dir(ns.id, a.n) / "proc" / "stdout.log", 400).strip().splitlines()
        return first_line(out[-1], 100) if out else "running…"
    if ns.stale:
        return f"queued again ({first_line(ns.stale_reason, 80)})"
    return ""


def node_time(ns: NodeState) -> str:
    a = ns.last
    if not a:
        return ""
    if a.ended_at:
        return fmt_duration(a.duration_s)
    return fmt_duration(seconds_since(a.started_at))


def status_view(st: RunState, paths: RunPaths, color: bool = False, width: int | None = None) -> str:
    g = st.graph()
    depth = g.depth()
    width = width or 140
    elapsed = (parse_iso(st.completed_at) - parse_iso(st.created_at)).total_seconds() if st.completed_at else seconds_since(st.created_at)
    cost = st.cost()
    counts = st.counts()
    head = (f"{bold(st.title or st.plan_id, color)}  ·  run {st.run_id}  ·  "
            f"{paint(st.status.upper().replace('_', ' '), st.status, color)}  ·  {fmt_duration(elapsed)}")
    meta = [f"plan generation {st.generation} ({short(st.digest)})",
            f"{counts.get('succeeded', 0)}/{len(g.order)} done"]
    if counts.get("failed"):
        meta.append(f"{counts['failed']} failed")
    if counts.get("running"):
        meta.append(f"{counts['running']} running")
    if cost["usd"] or cost["input_tokens"]:
        tok = f"{(cost['input_tokens'] + cost['output_tokens']) / 1000:.0f}k tokens"
        if cost["usd"]:
            meta.append(f"agent cost ${cost['usd']:.2f}" + ("+ " if not cost["complete"] else " ") + f"({tok})")
        else:
            meta.append(f"agents used {tok}" + ("" if cost["complete"] else " (cost not reported by harness)"))
    lines = [head, "  " + "  ·  ".join(meta)]
    if st.status_reason and st.status in ("failed", "parked", "rejected", "cancelled"):
        lines.append("  " + paint(first_line(st.status_reason, 120), st.status, color))
    lines.append("")
    name_w = min(34, max(12, max((len(n) + 2 * depth.get(n, 0) for n in g.order), default=12) + 1))
    lines.append(f"     {'NODE':<{name_w}} {'KIND':<20} {'TIME':>7}  RESULT / ACTIVITY")
    for nid in g.topo():
        spec = g.nodes[nid]
        ns = st.nodes.get(nid) or NodeState(nid)
        name = ("  " * depth.get(nid, 0) + nid)[:name_w]
        att = f"#{ns.last.n}" if ns.last and ns.last.n > 1 else ""
        act = node_activity(st, paths, ns, spec)
        room = max(30, width - name_w - 36)
        row = f"  {ICON.get(ns.status, '?')}  {name:<{name_w}} {kind_label(spec)[:20]:<20} {node_time(ns):>7}  {truncate(act, room)}"
        lines.append(paint(row, ns.status, color) if ns.status in ("failed", "running", "waiting", "retrying") else
                     (paint(row, "skipped", color) if ns.status in ("pending", "skipped") else row))
        if att:
            lines[-1] += f"  ({att})"
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
            out.append(f"      flower show {rid} --gate {g.id}            # read it in full")
            out.append(f"      flower answer {rid} {g.id} {g.decisions[0]} [--text '…']   # options: {', '.join(g.decisions)}")
    waits = []
    for nid, ns in st.nodes.items():
        w = ((ns.last.progress or {}).get("wait") or {}) if ns.status == "waiting" and ns.last and not ns.gate_id else {}
        if w.get("signal"):
            waits.append((nid, w))
    if waits:
        out += ["", "Waiting for external input:"]
        for nid, w in waits:
            dl = f" (deadline {w['deadline_at'][:16].replace('T', ' ')} UTC)" if w.get("deadline_at") else ""
            out.append(f"  • {nid} waits for signal {w['signal']!r}{dl}")
            out.append(f"      flower signal {rid} {w['signal']} --data '{{\"…\": …}}'")
    if st.status == "failed":
        failed = [n for n, s in st.nodes.items() if s.status == "failed"]
        out += ["", "Next:"] + [f"  flower show {rid} {n}        # see why {n} failed" for n in failed[:3]]
        out += [f"  flower rerun {rid} {failed[0]}    # retry it and everything after it"] if failed else []
    elif st.status in ("running", "parked") and not gates and not waits:
        out += ["", f"Next:  flower watch {rid}   (live view)   ·   flower log {rid}   (what happened)"]
    elif st.status == "succeeded":
        out += ["", f"Done.  flower report {rid}   → readable report with every decision, output and file"]
    return out


# ====================================================================== node detail

def node_detail(st: RunState, paths: RunPaths, nid: str, color: bool = False) -> str:
    spec = st.node_spec(nid)
    ns = st.nodes.get(nid) or NodeState(nid)
    g = st.graph()
    out = [f"{bold(nid, color)}  [{kind_label(spec)}]  {paint(ns.status.upper(), ns.status, color)}",
           f"  {spec.get('title') if spec.get('title') != nid else ''}".rstrip()]
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
        elif a.status == "running" and spec.get("kind") == "wait":
            label = "waiting for a signal/timer"
        out += ["", bold(f"attempt {a.n}", color) + f"  {label}  started {a.started_at[:19].replace('T', ' ')}"
                + (f"  took {fmt_duration(a.duration_s)}" if a.duration_s is not None else "")
                + (f"  (reused result of {a.reused_from})" if a.reused_from else "")]
        out.append(f"  dir: {adir}")
        if a.inputs:
            out.append("  inputs: " + truncate(json.dumps(a.inputs, default=str), 300))
        if a.session:
            out.append(f"  agent session: {a.session}" + (f"  (repair turns: {a.repairs})" if a.repairs else ""))
        if a.job:
            j = a.job
            out.append(f"  slurm: job {j.get('job_id', '-')} on {j.get('cluster')}  state={j.get('state')}"
                       f"  dir={j.get('job_dir')}" + (f"  remote errors={j['remote_errors']}" if j.get("remote_errors") else ""))
        if a.summary:
            out.append(f"  summary: {a.summary}")
        if a.rationale:
            out.append("  rationale: " + truncate(a.rationale, 600))
        if a.outputs:
            out.append("  outputs: " + truncate(json.dumps(a.outputs, default=str, ensure_ascii=False), 800))
        for k, f in (a.files or {}).items():
            out.append(f"  file {k}: {f.get('path')}" + (f"  ({f['bytes']} B, sha {f['sha256'][:10]})" if f.get("sha256") else ""))
        if a.usage and (a.usage.get("cost_usd") is not None or a.usage.get("input_tokens")):
            u = a.usage
            out.append(f"  usage: {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out tokens"
                       + (f", ${u['cost_usd']:.4f}" if u.get("cost_usd") is not None else "")
                       + (f", {u['turns']} turns" if u.get("turns") else ""))
        if a.error:
            out.append(paint(f"  error [{a.error.get('error_class')}]: {a.error.get('message')}", "failed", color))
            det = a.error.get("details") or {}
            for key in ("stderr", "stderr_tail", "text_tail", "stdout_tail"):
                if det.get(key):
                    out.append(f"  {key}:\n" + "\n".join("    " + l for l in str(det[key]).strip().splitlines()[-8:]))
        if a.status == "running":
            for name in ("stdout.log", "stderr.log"):
                t = tail_text(adir / "proc" / name, 800).strip()
                if t and spec.get("kind") != "agent":
                    out.append(f"  {name} (tail):\n" + "\n".join("    " + l for l in t.splitlines()[-6:]))
    gid = ns.gate_id
    if gid and gid in st.gates and st.gates[gid].status == "open":
        gt = st.gates[gid]
        out += ["", "QUESTION FOR YOU:", gt.message, "",
                f"answer:  flower answer {st.run_id} {gt.id} <{'|'.join(gt.decisions)}> [--text '…']"]
    return "\n".join(out)


def gate_detail(st: RunState, gid: str) -> str:
    g = st.gates[gid]
    out = [f"gate {g.id}  ({g.subject})  {g.status}", "", g.message, ""]
    if g.status == "open":
        out.append(f"answer:  flower answer {st.run_id} {g.id} <{'|'.join(g.decisions)}> [--text '…']")
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
    if t == "agent.session":
        return None
    if t == "agent.repair":
        return f"{a}: answer did not match contract, repair turn {p.get('n')} ({first_line(p.get('reason'), 80)})"
    if t == "job.submit_intent":
        return f"{a}: submitting to {p.get('cluster')} as {p.get('submit_key')}"
    if t == "job.submitted":
        return f"{a}: slurm job {p.get('job_id')} " + ("submitted" if p.get("via") == "submitted" else f"re-attached ({p.get('via')})")
    if t == "job.observed":
        return f"{a}: job {p.get('state')} ({p.get('raw')})"
    if t == "job.exited":
        return f"{a}: job exited (exit code {p.get('ec')}, slurm {p.get('final')})"
    if t == "job.remote_error":
        return f"{a}: cluster error during {p.get('op')}: {first_line(p.get('error'), 90)}"
    if t == "job.lost":
        return f"{a}: job LOST — {p.get('why')}"
    if t == "job.retrieved" or t == "job.cancel_requested":
        return None
    if t == "gate.requested":
        subj = p.get("subject")
        return f"decision requested ({subj}{' ' + n if n else ''}): {first_line(p.get('message'), 90)}"
    if t == "gate.answered":
        return f"decision {p.get('gate_id')}: {p.get('decision')!r} by {p.get('by')}" + (f" — {first_line(p.get('text'), 90)}" if p.get("text") else "")
    if t == "wait.armed":
        return f"{n} waiting" + (f" for signal {p['signal']!r}" if p.get("signal") else "")
    if t == "signal.received":
        return f"signal {p.get('name')!r} from {p.get('by')}"
    if t == "wait.expired":
        return f"{n}: deadline passed"
    if t == "plan.amendment.proposed":
        return f"plan change proposed by {p.get('proposed_by')}: {first_line(p.get('rationale'), 100)}"
    if t == "plan.amendment.approved":
        eff = p.get("effects") or {}
        return (f"plan change APPLIED → generation {p.get('generation')} by {p.get('by')}"
                + (f"; adds {', '.join(eff.get('added', [])[:6])}" if eff.get("added") else ""))
    if t == "plan.amendment.rejected":
        return f"plan change rejected by {p.get('by')}: {first_line(p.get('reason'), 100)}"
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
        ts = ev["occurredAtIso"][11:19]
        rel = fmt_duration((parse_iso(ev["occurredAtIso"]) - t0).total_seconds()) if t0 else ""
        actor = ev.get("actor", "system")
        who = "" if actor == "system" else f"  [{actor}]"
        status = ("failed" if "✗" in d or "LOST" in d or "FAILED" in d else "succeeded" if "✓" in d or "SUCCEEDED" in d
                  else "waiting" if "decision" in d else "")
        lines.append(f"{ts} +{rel:>6}  " + paint(d, status, color) + who)
    return "\n".join(lines)
