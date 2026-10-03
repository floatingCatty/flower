"""forgeflow command line.

Exit codes (one vocabulary, Smithers 1.0): 0 ok/succeeded · 1 failed/cancelled/rejected ·
2 usage or input error · 3 parked: the run needs a decision, a signal or more time · 130 interrupted.

Every command accepts ``--json``: stdout is then exactly one object
``{"ok": bool, "data": …, "error": {code, message, suggestion}, "next": ["forgeflow …", …]}``.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from . import plan as planmod
from .engine import Engine, create_run, driver_alive
from .rundir import RunPaths, find_root, list_runs, resolve_run
from .state import TERMINAL_RUN
from .util import ForgeflowError, atomic_write_json, default_actor, hostname, now_iso, read_json

EXIT = {"succeeded": 0, "failed": 1, "cancelled": 1, "rejected": 1, "parked": 3, "awaiting_approval": 3,
        "running": 3}


class Out:
    def __init__(self, as_json: bool):
        self.json = as_json
        self.color = (not as_json) and sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def done(self, data: Any = None, text: str | None = None, nxt: list[str] | None = None, code: int = 0) -> int:
        if self.json:
            print(json.dumps({"ok": code in (0, 3), "data": data, "next": nxt or []}, default=str, ensure_ascii=False))
        else:
            if text:
                print(text)
            if nxt:
                print("\nnext: " + "\n      ".join(nxt))
        return code

    def error(self, err: ForgeflowError, code: int = 2) -> int:
        if self.json:
            print(json.dumps({"ok": False, "error": err.to_dict(), "next": []}, default=str, ensure_ascii=False))
        else:
            print(f"error [{err.code}]: {err.message}", file=sys.stderr)
            if err.details and isinstance(err.details, list):
                for d in err.details[:30]:
                    if isinstance(d, dict):
                        print(f"  - {d.get('path')}: {d.get('message')}" + (f"  (hint: {d['suggestion']})" if d.get("suggestion") else ""), file=sys.stderr)
            if err.suggestion:
                print(f"hint: {err.suggestion}", file=sys.stderr)
        return code


# ====================================================================== helpers

def parse_kv(items: list[str] | None) -> dict:
    out: dict[str, Any] = {}
    for it in items or []:
        if "=" not in it:
            raise ForgeflowError("bad_input", f"--input expects NAME=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        try:
            out[k] = json.loads(v) if v[:1] in "[{" or v in ("true", "false", "null") else v
        except ValueError:
            out[k] = v
    return out


def get_engine(args) -> Engine:
    root = find_root()
    paths = resolve_run(root, getattr(args, "run", None))
    return Engine(paths)


def spawn_driver(eng: Engine) -> dict:
    info = driver_alive(eng.paths)
    if info:
        return {"already_running": True, **info}
    log = eng.paths.dir / "driver.log"
    with open(log, "ab") as fh:
        p = subprocess.Popen([sys.executable, "-m", "forgeflow", "_driver", eng.paths.run_id, "--root", str(eng.paths.root)],
                             stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, start_new_session=True, close_fds=True)
    info = {"pid": p.pid, "host": hostname(), "started_at": now_iso(), "log": str(log)}
    atomic_write_json(eng.paths.driver_file, info)
    return info


def run_status_code(status: str) -> int:
    return EXIT.get(status, 3)


def next_for(st, rid: str) -> list[str]:
    nxt = []
    for g in st.open_gates():
        nxt.append(f"forgeflow show {rid} --gate {g.id}")
        nxt.append(f"forgeflow answer {rid} {g.id} <{'|'.join(g.decisions)}> --text '<why>'")
    if st.status == "failed":
        failed = [n for n, s in st.nodes.items() if s.status == "failed"]
        if failed:
            nxt += [f"forgeflow show {rid} {failed[0]}", f"forgeflow rerun {rid} {failed[0]}"]
    for nid, ns in st.nodes.items():
        w = ((ns.last.progress or {}).get("wait") or {}) if ns.status == "waiting" and ns.last and not ns.gate_id else {}
        if w.get("signal"):
            nxt.append(f"forgeflow signal {rid} {w['signal']} --data '<json>'   # {nid} waits for it")
    if st.status in ("running", "parked") and not st.open_gates():
        nxt.append(f"forgeflow wait {rid} --timeout 600")
    if st.status == "succeeded":
        nxt.append(f"forgeflow report {rid}")
    return nxt


def summary_data(eng: Engine, st=None) -> dict:
    st = st or eng.state()
    g = st.graph()
    nodes = []
    for nid in g.topo():
        ns = st.nodes.get(nid)
        a = ns.last if ns else None
        nodes.append({"id": nid, "kind": g.nodes[nid].get("kind"), "status": ns.status if ns else "pending",
                      "attempt": a.n if a else 0, "summary": (ns.result.summary if ns and ns.result else None),
                      "error": (a.error if a and ns.status == "failed" else None),
                      "job": ({k: a.job.get(k) for k in ("job_id", "state", "cluster")} if a and a.job else None)})
    return {"run_id": st.run_id, "title": st.title, "status": st.status, "reason": st.status_reason,
            "generation": st.generation, "digest": st.digest, "dir": str(eng.paths.dir), "cost": st.cost(),
            "nodes": nodes, "open_gates": [{"id": g.id, "subject": g.subject, "node": g.node, "decisions": g.decisions,
                                           "message": g.message} for g in st.open_gates()]}


def live_drive(eng: Engine, out: Out, timeout: float | None = None) -> str:
    """Drive in the foreground; show a live view on a TTY, change-lines otherwise."""
    from .render import describe_event, status_view
    tty = out.color
    seen = {"n": len(eng.journal.read())}

    def show(rep):
        if out.json:
            return
        if tty:
            st = eng.state()
            view = status_view(st, eng.paths, color=out.color)
            sys.stdout.write("\x1b[H\x1b[2J" + view + "\n\n(ctrl-c detaches; nodes keep running — resume with "
                             f"`forgeflow resume {st.run_id}`)\n")
            sys.stdout.flush()
        else:  # piped / logged: a plain narrative of new events
            evs = eng.journal.read()
            for ev in evs[seen["n"]:]:
                d = describe_event(ev)
                if d:
                    print(f"{ev['occurredAtIso'][11:19]}  {d}", flush=True)
            seen["n"] = len(evs)

    rep = eng.drive(until="settled", timeout=timeout, on_tick=show)
    return rep.status


# ====================================================================== commands

def cmd_init(args, out: Out) -> int:
    root = Path(args.dir or os.getcwd()).resolve()
    (root / ".forgeflow" / "runs").mkdir(parents=True, exist_ok=True)
    msg = [f"initialised {root / '.forgeflow'}"]
    if args.skill:
        from .skill import install_skill
        for p in install_skill(root, args.skill):
            msg.append(f"installed skill → {p}")
    return out.done({"root": str(root)}, "\n".join(msg), ["forgeflow plan new my-plan.yaml",
                                                          "forgeflow run my-plan.yaml"])


def cmd_plan(args, out: Out) -> int:
    from .render import plan_overview
    if args.plan_cmd == "validate":
        raw = planmod.load_plan_file(args.file)
        plan = planmod.check(raw)
        return out.done({"valid": True, "id": plan["id"], "nodes": len(plan["nodes"]), "digest": planmod.plan_digest(plan)},
                        f"✓ {args.file} is valid: {len(plan['nodes'])} nodes, digest {planmod.plan_digest(plan)[7:19]}",
                        [f"forgeflow plan show {args.file}", f"forgeflow run {args.file}"])
    if args.plan_cmd == "show":
        raw = planmod.load_plan_file(args.file)
        plan = planmod.check(raw)
        return out.done({"plan": planmod.contract_view(plan), "overview": plan_overview(plan)}, plan_overview(plan))
    if args.plan_cmd == "diff":
        a = planmod.check(planmod.load_plan_file(args.a))
        b = planmod.check(planmod.load_plan_file(args.b))
        d = planmod.diff_plans(a, b)
        text = "\n".join([f"added:   {', '.join(d['added']) or '-'}", f"removed: {', '.join(d['removed']) or '-'}"]
                         + [f"changed: {c['id']} ({', '.join(c['fields'])})" for c in d["changed"]])
        return out.done(d, text)
    if args.plan_cmd == "new":
        from .skill import PLAN_TEMPLATE
        p = Path(args.file)
        if p.exists() and not args.force:
            raise ForgeflowError("exists", f"{p} already exists", "use --force to overwrite")
        p.write_text(PLAN_TEMPLATE.replace("{id}", p.stem))
        return out.done({"file": str(p)}, f"wrote {p} — edit it, then `forgeflow plan validate {p}`")
    if args.plan_cmd == "reference":
        from .skill import PLAN_REFERENCE
        return out.done({"reference": PLAN_REFERENCE}, PLAN_REFERENCE)
    raise ForgeflowError("usage", "unknown plan command")


def cmd_run(args, out: Out) -> int:
    from .render import plan_overview
    raw = planmod.load_plan_file(args.plan)
    inputs = parse_kv(args.input)
    if args.inputs:
        inputs.update(json.loads(Path(args.inputs).read_text()))
    approve = bool(args.yes)
    reuse = []
    if args.reuse:
        root = find_root(create=True)
        reuse = [resolve_run(root, r).run_id for r in args.reuse]
    eng = create_run(raw, inputs, actor=args.actor, approve=False, note=args.note, reuse_from=reuse,
                     rerun_from=args.rerun_from or [])
    st = eng.state()
    rid = st.run_id
    if not approve and out.color and sys.stdin.isatty() and not args.no_prompt:
        print(plan_overview(st.plan, st.inputs))
        print(f"\nrun {rid} created.  Approve this plan and start? [y/N] ", end="", flush=True)
        approve = sys.stdin.readline().strip().lower() in ("y", "yes")
        if not approve:
            print(f"\nNot started. Approve later with:  forgeflow approve {rid}")
            return 3
    if not approve:
        nxt = [f"forgeflow show {rid} --gate plan", f"forgeflow approve {rid} --note '<who approved and why>'",
               f"forgeflow reject {rid} --text '<what to change>'"]
        return out.done({**summary_data(eng), "overview": plan_overview(st.plan, st.inputs)},
                        plan_overview(st.plan, st.inputs) + f"\n\nrun {rid} is waiting for plan approval.", nxt, code=3)
    eng.answer("plan", "approve", text=args.note or "approved at launch", by=args.actor or default_actor())
    return _continue(eng, args, out)


def _continue(eng: Engine, args, out: Out) -> int:
    rid = eng.paths.run_id
    if getattr(args, "detach", False):
        eng.tick()
        info = spawn_driver(eng)
        st = eng.state()
        return out.done({**summary_data(eng, st), "driver": info},
                        f"run {rid} is running in the background (driver pid {info.get('pid')}).",
                        [f"forgeflow watch {rid}", f"forgeflow wait {rid} --timeout 600", f"forgeflow status {rid}"],
                        code=0 if st.status not in TERMINAL_RUN else run_status_code(st.status))
    try:
        status = live_drive(eng, out, timeout=getattr(args, "timeout", None))
    except KeyboardInterrupt:
        st = eng.state()
        return out.done(summary_data(eng, st), f"\ndetached from {rid} (status {st.status}); "
                        f"running nodes continue. Resume: forgeflow resume {rid}", code=130)
    st = eng.state()
    from .render import status_view
    text = None if out.json else ("\n" + status_view(st, eng.paths, color=out.color))
    return out.done(summary_data(eng, st), text, next_for(st, rid) if out.json else None, code=run_status_code(status))


def cmd_fork(args, out: Out) -> int:
    """New run of the same approved plan + inputs, reusing every recorded result that is still valid."""
    src = get_engine(args)
    st = src.state()
    base = dict(st.generations[0]["plan"])
    eng = create_run(base, st.inputs, actor=args.actor, approve=False,
                     note=args.note or f"fork of {st.run_id}" + (f" from {', '.join(args.rerun_from)}" if args.rerun_from else ""),
                     reuse_from=[st.run_id], rerun_from=args.rerun_from or [])
    args.run = eng.paths.run_id
    if not args.yes:
        nst = eng.state()
        return out.done(summary_data(eng, nst), f"forked {st.run_id} → {nst.run_id} (waiting for approval;"
                        f" unchanged steps will be reused, {', '.join(args.rerun_from) or 'nothing'} re-executed)",
                        [f"forgeflow approve {nst.run_id}"], code=3)
    eng.answer("plan", "approve", text=args.note or f"fork of {st.run_id}", by=args.actor or default_actor())
    return _continue(eng, args, out)


def cmd_resume(args, out: Out) -> int:
    eng = get_engine(args)
    st = eng.state()
    if st.status == "awaiting_approval" and not st.gates["plan"].status == "answered":
        raise ForgeflowError("not_approved", "the plan has not been approved yet", f"forgeflow approve {st.run_id}")
    return _continue(eng, args, out)


def cmd_tick(args, out: Out) -> int:
    root = find_root()
    runs = list_runs(root) if args.all else [resolve_run(root, args.run).run_id]
    reps = []
    for rid in runs:
        eng = Engine(RunPaths(root, rid))
        st = eng.state()
        if st.status in TERMINAL_RUN:
            continue
        rep = eng.tick()
        reps.append(rep.to_dict())
    text = "\n".join(f"{r['run_id']}: {r['status']}" + (f" started {r['started']}" if r['started'] else "")
                     + (f" finished {r['finished']}" if r['finished'] else "") for r in reps) or "nothing to do"
    return out.done(reps, text)


def cmd_status(args, out: Out) -> int:
    from .render import status_view
    eng = get_engine(args)
    st = eng.state()
    if not args.no_tick and st.status not in TERMINAL_RUN and st.status != "awaiting_approval" and not driver_alive(eng.paths):
        eng.tick()
        st = eng.state()
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color), next_for(st, st.run_id) if out.json else None,
                    code=0)


def cmd_watch(args, out: Out) -> int:
    from .render import status_view
    eng = get_engine(args)
    try:
        if driver_alive(eng.paths):
            while True:
                st = eng.state()
                if out.color:
                    sys.stdout.write("\x1b[H\x1b[2J" + status_view(st, eng.paths, color=True) + "\n")
                    sys.stdout.flush()
                if st.status in TERMINAL_RUN or (st.status == "parked" and st.open_gates()):
                    break
                time.sleep(args.interval)
        else:
            live_drive(eng, out)
    except KeyboardInterrupt:
        pass
    st = eng.state()
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color), code=run_status_code(st.status))


def cmd_wait(args, out: Out) -> int:
    from .render import status_view
    eng = get_engine(args)
    start = time.time()
    while True:
        st = eng.state()
        if st.status in TERMINAL_RUN or st.open_gates() or st.status == "awaiting_approval":
            break
        if driver_alive(eng.paths) or args.no_tick:
            time.sleep(2)
        else:
            rep = eng.tick()
            if rep.status == "parked" and not rep.next_poll_s:
                break
            time.sleep(max(0.3, min(rep.next_poll_s or 2, 5)))
        if args.timeout and time.time() - start > args.timeout:
            break
    st = eng.state()
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color), next_for(st, st.run_id),
                    code=run_status_code(st.status))


def cmd_ls(args, out: Out) -> int:
    from .util import fmt_duration, seconds_since
    root = find_root()
    rows = []
    for rid in list_runs(root)[-args.limit:]:
        try:
            st = Engine(RunPaths(root, rid)).state()
        except ForgeflowError:
            continue
        c = st.counts()
        rows.append({"run_id": rid, "title": st.title, "status": st.status, "created": st.created_at,
                     "done": c.get("succeeded", 0), "nodes": sum(c.values()), "gates": len(st.open_gates())})
    text = "\n".join(f"{r['run_id']:<46} {r['status']:<18} {r['done']}/{r['nodes']:<4} "
                     f"{fmt_duration(seconds_since(r['created'])):>7} ago  {r['title']}"
                     + (f"  ⚑ {r['gates']} decision(s) pending" if r["gates"] else "") for r in rows) or "no runs yet"
    return out.done(rows, text)


def cmd_show(args, out: Out) -> int:
    from .render import gate_detail, node_detail, status_view
    eng = get_engine(args)
    st = eng.state()
    if args.gate:
        gid = args.gate
        if gid not in st.gates:
            cand = [g for g in st.gates if g.startswith(gid)]
            if len(cand) != 1:
                raise ForgeflowError("gate_not_found", f"no gate {gid!r}", f"gates: {', '.join(st.gates)}")
            gid = cand[0]
        g = st.gates[gid]
        return out.done(g.__dict__, gate_detail(st, gid))
    if args.node:
        if args.node not in st.nodes:
            raise ForgeflowError("node_not_found", f"no node {args.node!r}", f"nodes: {', '.join(st.graph().order)}")
        ns = st.nodes[args.node]
        data = {"id": args.node, "status": ns.status, "spec": st.node_spec(args.node),
                "attempts": [a.__dict__ for a in ns.attempts]}
        return out.done(data, node_detail(st, eng.paths, args.node, color=out.color))
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color))


def cmd_log(args, out: Out) -> int:
    from .render import timeline
    eng = get_engine(args)
    evs = eng.journal.read()
    if args.json:
        if args.node:
            evs = [e for e in evs if e.get("nodeId") == args.node]
        return out.done(evs)
    print(timeline(evs, color=out.color, node=args.node))
    if args.follow:
        from .render import describe_event
        seen = len(evs)
        try:
            while True:
                time.sleep(1)
                evs = eng.journal.read()
                for ev in evs[seen:]:
                    if args.node and ev.get("nodeId") != args.node:
                        continue
                    d = describe_event(ev)
                    if d:
                        print(f"{ev['occurredAtIso'][11:19]}  {d}", flush=True)
                seen = len(evs)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_logs(args, out: Out) -> int:
    from .transcript import render_transcript
    eng = get_engine(args)
    st = eng.state()
    ns = st.nodes.get(args.node)
    if not ns or not ns.attempts:
        raise ForgeflowError("no_attempt", f"node {args.node} has not run yet")
    a = ns.attempts[-1] if not args.attempt else next((x for x in ns.attempts if x.n == args.attempt), None)
    if a is None:
        raise ForgeflowError("no_attempt", f"node {args.node} has no attempt {args.attempt}",
                             f"attempts: {', '.join(str(x.n) for x in ns.attempts)}")
    adir = eng.paths.attempt_dir(args.node, a.n)
    spec = st.node_spec(args.node)
    if spec.get("kind") == "agent" and not args.raw:
        text = render_transcript(adir, (spec.get("harness") or {}).get("name", "claude"))
    elif spec.get("kind") == "job":
        jd = Path(a.job.get("job_dir") or "")
        local = jd if jd.exists() else adir / "job"
        jid = a.job.get("job_id")
        parts = []
        for name in ([f"slurm-{jid}.out", f"slurm-{jid}.err"] if jid else []):
            p = local / name
            if p.exists():
                parts.append(f"==> {p} <==\n" + p.read_text(errors="replace")[-20000:])
        text = "\n".join(parts) or "(no job output yet)"
    else:
        parts = []
        for name in ("stdout.log", "stderr.log"):
            p = adir / "proc" / name
            if p.exists() and p.stat().st_size:
                parts.append(f"==> {name} <==\n" + p.read_text(errors="replace")[-20000:])
        text = "\n".join(parts) or "(no output)"
    return out.done({"text": text, "dir": str(adir)}, text)


def cmd_output(args, out: Out) -> int:
    eng = get_engine(args)
    st = eng.state()
    ns = st.nodes.get(args.node)
    if not ns or not ns.result:
        raise ForgeflowError("no_result", f"node {args.node} has no successful result")
    r = ns.result
    data = {"outputs": r.outputs, "files": r.files, "summary": r.summary, "rationale": r.rationale, "attempt": r.n}
    if args.key:
        if args.key in r.files:
            v = r.files[args.key]["path"]
        else:
            v = r.outputs
            try:
                for part in args.key.split("."):
                    v = v[part] if isinstance(v, dict) else v[int(part)]
            except (KeyError, IndexError, ValueError, TypeError):
                raise ForgeflowError("no_key", f"node {args.node} has no output {args.key!r}",
                                     f"outputs: {', '.join(r.outputs) or '-'}; files: {', '.join(r.files) or '-'}") from None
        return out.done(v, v if isinstance(v, str) else json.dumps(v, indent=2, ensure_ascii=False))
    return out.done(data, json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _record_and_continue(eng: Engine, args, out: Out, text: str) -> int:
    rep = eng.tick()
    st = eng.state()
    if not getattr(args, "no_continue", False) and st.status not in TERMINAL_RUN and (rep.running or st.status == "running"):
        if getattr(args, "wait", False):
            return _continue(eng, args, out)
        info = spawn_driver(eng)
        text += f"\nthe run continues in the background (driver pid {info.get('pid')})."
    st = eng.state()
    return out.done(summary_data(eng, st), text, next_for(st, st.run_id), code=0)


def cmd_approve(args, out: Out) -> int:
    eng = get_engine(args)
    st = eng.state()
    gid = args.gate or ("plan" if st.gates.get("plan") and st.gates["plan"].status == "open" else None)
    if gid is None:
        opens = st.open_gates()
        if len(opens) != 1:
            raise ForgeflowError("which_gate", f"{len(opens)} open gates; say which",
                                 "open gates: " + ", ".join(g.id for g in opens) if opens else "nothing to approve")
        gid = opens[0].id
    g = st.gates.get(gid) or next((x for x in st.open_gates() if x.node == gid), None)
    if g is None:
        raise ForgeflowError("gate_not_found", f"no gate {gid!r} in run {st.run_id}",
                             "open gates: " + (", ".join(x.id for x in st.open_gates()) or "none"))
    decision = (g.decisions[0] if g else "approve") if not args.reject else ("reject" if g and "reject" in g.decisions else g.decisions[-1])
    eng.answer(gid, decision, text=args.note or args.text, by=args.actor or default_actor())
    return _record_and_continue(eng, args, out, f"{decision}: {gid}")


def cmd_answer(args, out: Out) -> int:
    eng = get_engine(args)
    eng.answer(args.gate, args.decision, text=args.text, by=args.actor or default_actor())
    return _record_and_continue(eng, args, out, f"answered {args.gate}: {args.decision}")


def cmd_cancel(args, out: Out) -> int:
    eng = get_engine(args)
    eng.cancel(node=args.node, reason=args.reason, by=args.actor or default_actor())
    for _ in range(20):
        rep = eng.tick()
        st = eng.state()
        if args.node is None and st.status in TERMINAL_RUN:
            break
        if args.node and st.nodes[args.node].status not in ("running", "waiting"):
            break
        time.sleep(0.5)
    st = eng.state()
    return out.done(summary_data(eng, st), f"cancel requested ({args.node or 'whole run'}); status now {st.status}")


def cmd_rerun(args, out: Out) -> int:
    eng = get_engine(args)
    targets = eng.rerun(args.node, downstream=not args.only, force=not args.cached, by=args.actor or default_actor(),
                        reason=args.reason)
    return _record_and_continue(eng, args, out, f"queued again: {', '.join(targets)}")


def cmd_amend(args, out: Out) -> int:
    import yaml
    eng = get_engine(args)
    doc = planmod.yaml_load(Path(args.file).read_text())
    if isinstance(doc, list):
        doc = {"ops": doc}
    rationale = args.rationale or doc.get("rationale") or ""
    aid = eng.propose_amendment(doc.get("ops") or [], rationale, by=args.actor or default_actor(),
                                auto_approve=bool(args.yes))
    st = eng.state()
    if args.yes:
        return _record_and_continue(eng, args, out, f"amendment {aid} applied (generation {st.generation})")
    return out.done({"amendment_id": aid, "gate": f"amend-{aid}"},
                    f"amendment {aid} proposed; it needs approval:\n  forgeflow show {st.run_id} --gate amend-{aid}\n"
                    f"  forgeflow approve {st.run_id} amend-{aid}", code=3)


def cmd_signal(args, out: Out) -> int:
    eng = get_engine(args)
    data = json.loads(args.data) if args.data else None
    eng.signal(args.name, data, by=args.actor or default_actor(), token=args.token)
    return _record_and_continue(eng, args, out, f"signal {args.name!r} sent")


def cmd_note(args, out: Out) -> int:
    eng = get_engine(args)
    eng.note(args.text, node=args.node, by=args.actor or default_actor())
    return out.done({"ok": True}, "noted")


def cmd_report(args, out: Out) -> int:
    from .report import write_report
    eng = get_engine(args)
    paths = write_report(eng)
    text = f"report: {paths['md']}\n        {paths['html']}"
    return out.done(paths, text)


def cmd_audit(args, out: Out) -> int:
    from .report import audit
    eng = get_engine(args)
    data = audit(eng)
    if out.json:
        return out.done(data)
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_export(args, out: Out) -> int:
    from .provenance import export_crate
    eng = get_engine(args)
    path = export_crate(eng)
    return out.done({"ro_crate": str(path)}, f"RO-Crate (Provenance Run Crate) metadata: {path}\n"
                    f"the run directory {eng.paths.dir} is now a self-describing research object")


def cmd_skill(args, out: Out) -> int:
    from .skill import install_skill
    root = find_root(create=True) if args.target == "project" else Path.cwd()
    paths = install_skill(root, args.target)
    return out.done([str(p) for p in paths], "\n".join(f"installed {p}" for p in paths))


def cmd_doctor(args, out: Out) -> int:
    import shutil
    checks = []

    def chk(name, ok, detail):
        checks.append({"check": name, "ok": ok, "detail": detail})

    chk("python", sys.version_info >= (3, 9), sys.version.split()[0])
    for exe, flag in (("claude", "--version"), ("codex", "--version"), ("pi", "--version")):
        p = shutil.which(exe)
        if p:
            try:
                v = subprocess.run([p, flag], capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[-1]
            except Exception as exc:  # noqa: BLE001
                v = f"error: {exc}"
            chk(f"harness {exe}", True, f"{p} ({v})")
        else:
            chk(f"harness {exe}", False, "not on PATH (only needed for agent nodes using it)")
    for exe in ("sbatch", "squeue", "sacct", "ssh", "rsync"):
        p = shutil.which(exe)
        chk(exe, bool(p), p or "not on PATH")
    try:
        root = find_root()
        chk("project", True, str(root))
    except ForgeflowError as exc:
        chk("project", False, exc.message)
    text = "\n".join(f"{'✓' if c['ok'] else '·'} {c['check']:<16} {c['detail']}" for c in checks)
    return out.done(checks, text)


def cmd_driver(args, out: Out) -> int:  # internal: background driver loop
    root = Path(args.root)
    eng = Engine(RunPaths(root, args.run))
    atomic_write_json(eng.paths.driver_file, {"pid": os.getpid(), "host": hostname(), "started_at": now_iso()})
    eng.emit("driver.started", {"pid": os.getpid(), "host": hostname()})
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
    idle_limit = float(os.environ.get("FORGEFLOW_DRIVER_IDLE_EXIT", 6 * 3600))
    idle = 0.0
    try:
        while not stop["flag"]:
            rep = eng.drive(until="settled", timeout=60)
            if rep.status in TERMINAL_RUN:
                break
            if rep.status in ("parked", "awaiting_approval") and not rep.next_poll_s:
                # parked on a human/external event: keep watching cheaply so answers or signals dropped as
                # files are picked up without anyone running a command; give up after a long idle period
                before = eng.state().last_seq
                time.sleep(15)
                pending = list(eng.paths.pending.glob("*.answer.json")) + list(eng.paths.signals.glob("*.json"))
                if eng.state().last_seq != before or pending:
                    idle = 0.0
                    continue
                idle += 15
                if idle >= idle_limit:
                    break
    finally:
        eng.emit("driver.stopped", {"pid": os.getpid(), "host": hostname(), "status": eng.state().status})
        info = read_json(eng.paths.driver_file) or {}
        if info.get("pid") == os.getpid():
            try:
                eng.paths.driver_file.unlink()
            except FileNotFoundError:
                pass
    return 0


def cmd_ui(args, out: Out) -> int:
    from .ui import serve
    root = find_root()

    def ready(srv, ui):
        host, port = srv.server_address[:2]
        url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/?token={ui.token}"
        lines = [f"forgeflow ui for {root}", f"  open: {url}", f"  acting as: {ui.actor}  (decisions you make here are recorded under this name)"]
        if host in ("127.0.0.1", "localhost"):
            lines.append(f"  remote machine? tunnel first:  ssh -N -L {port}:localhost:{port} <this-host>")
        lines.append("  ctrl-c to stop (runs keep going; the UI only reads the journal and sends the same actions as the CLI)")
        if out.json:
            print(json.dumps({"ok": True, "data": {"url": url, "root": str(root), "actor": ui.actor}}), flush=True)
        else:
            print("\n".join(lines), flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)

    try:
        serve(root, host=args.host, port=args.port, actor=args.actor, token=args.token, ready=ready)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        raise ForgeflowError("ui_port", f"cannot listen on {args.host}:{args.port}: {exc}", "pick another --port") from None
    return 0


def cmd_mcp(args, out: Out) -> int:
    from .mcp_server import serve
    return serve(read_only=args.read_only)


def cmd_fake_slurm(args, out: Out) -> int:
    from .testing.fakeslurm import install
    bindir = install(Path(args.dir))
    return out.done({"bin_dir": str(bindir)}, f"fake Slurm installed in {bindir}\n"
                    f"use it in a plan:  clusters: {{local: {{transport: local, bin_dir: {bindir}, min_poll: 1s}}}}")


# ====================================================================== parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forgeflow", description="Durable, file-first workflows for long agent + HPC research runs.")
    p.add_argument("--version", action="version", version=f"forgeflow {__version__}")
    p.add_argument("--json", dest="json_top", action="store_true", help="machine-readable output (any position)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output (one JSON object)")
    common.add_argument("--actor", help="who is acting (default: $FORGEFLOW_ACTOR or human:$USER)")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    s = add("init", cmd_init, "create .forgeflow/ in this directory")
    s.add_argument("dir", nargs="?")
    s.add_argument("--skill", choices=["claude", "codex", "agents", "project", "all"], help="also install the agent skill")

    s = add("plan", cmd_plan, "write, validate and inspect plan files")
    ps = s.add_subparsers(dest="plan_cmd", metavar="SUBCOMMAND")
    for name, h in (("validate", "check a plan and list every problem"), ("show", "human overview of a plan")):
        x = ps.add_parser(name, parents=[common], help=h)
        x.add_argument("file")
    x = ps.add_parser("diff", parents=[common], help="compare two plan files")
    x.add_argument("a")
    x.add_argument("b")
    x = ps.add_parser("new", parents=[common], help="write a commented starter plan")
    x.add_argument("file")
    x.add_argument("--force", action="store_true")
    ps.add_parser("reference", parents=[common], help="print the plan file reference")

    s = add("run", cmd_run, "create a run from a plan (asks for approval), then drive it")
    s.add_argument("plan")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE (repeatable)")
    s.add_argument("--inputs", help="JSON file with inputs")
    s.add_argument("-y", "--yes", action="store_true", help="approve the plan now (you are the approver)")
    s.add_argument("--note", help="approval note recorded in the journal")
    s.add_argument("-d", "--detach", action="store_true", help="continue in a background driver")
    s.add_argument("--no-prompt", action="store_true", help="never ask interactively")
    s.add_argument("--timeout", type=float)
    s.add_argument("--reuse", action="append", metavar="RUN",
                   help="reuse recorded results of RUN where a node's definition and inputs are unchanged")
    s.add_argument("--rerun-from", action="append", metavar="NODE", help="with --reuse: re-execute NODE and its downstream")

    s = add("fork", cmd_fork, "re-run a run's approved plan, reusing every still-valid recorded result (replay)")
    s.add_argument("run")
    s.add_argument("--from", dest="rerun_from", action="append", metavar="NODE", help="re-execute NODE and downstream")
    s.add_argument("-y", "--yes", action="store_true", help="approve the new run now")
    s.add_argument("-d", "--detach", action="store_true")
    s.add_argument("--note")
    s.add_argument("--timeout", type=float)

    s = add("resume", cmd_resume, "drive a run again (foreground, or --detach)")
    s.add_argument("run", nargs="?")
    s.add_argument("-d", "--detach", action="store_true")
    s.add_argument("--timeout", type=float)

    s = add("tick", cmd_tick, "one non-blocking scheduling pass (for cron/scrontab)")
    s.add_argument("run", nargs="?")
    s.add_argument("--all", action="store_true", help="every active run in this project")

    s = add("status", cmd_status, "where is the run, and what needs you")
    s.add_argument("run", nargs="?")
    s.add_argument("--no-tick", action="store_true", help="pure read; do not advance the run")

    s = add("watch", cmd_watch, "live view (drives the run unless a background driver does)")
    s.add_argument("run", nargs="?")
    s.add_argument("--interval", type=float, default=2.0)

    s = add("wait", cmd_wait, "block until the run finishes or needs a decision")
    s.add_argument("run", nargs="?")
    s.add_argument("--no-tick", action="store_true", help="observe only; never advance the run")
    s.add_argument("--timeout", type=float, help="seconds; exit 3 if still running")

    s = add("ls", cmd_ls, "list runs")
    s.add_argument("--limit", type=int, default=30)

    s = add("show", cmd_show, "details of a run, a node (attempts, outputs, errors) or a gate")
    s.add_argument("run", nargs="?")
    s.add_argument("node", nargs="?")
    s.add_argument("--gate")

    s = add("log", cmd_log, "human timeline of everything that happened")
    s.add_argument("run", nargs="?")
    s.add_argument("--node")
    s.add_argument("-f", "--follow", action="store_true")

    s = add("logs", cmd_logs, "raw output of a node (agent transcript, job stdout/stderr, shell logs)")
    s.add_argument("run")
    s.add_argument("node")
    s.add_argument("--attempt", type=int)
    s.add_argument("--raw", action="store_true", help="agent: raw JSON stream instead of the readable transcript")

    s = add("output", cmd_output, "print a node's outputs (or one key / file path)")
    s.add_argument("run")
    s.add_argument("node")
    s.add_argument("key", nargs="?")

    s = add("approve", cmd_approve, "approve the plan (or a gate / amendment)")
    s.add_argument("run", nargs="?")
    s.add_argument("gate", nargs="?")
    s.add_argument("--note", help="recorded with the approval")
    s.add_argument("--text", help=argparse.SUPPRESS)
    s.add_argument("--reject", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--no-continue", action="store_true", help="record only; do not start a background driver")
    s.add_argument("--wait", action="store_true", help="drive in the foreground after approving")
    s.add_argument("--timeout", type=float)

    s = add("reject", cmd_approve, "reject the plan (or a gate / amendment)")
    s.add_argument("run", nargs="?")
    s.add_argument("gate", nargs="?")
    s.add_argument("--text", help="why / what to change")
    s.add_argument("--note", help=argparse.SUPPRESS)
    s.add_argument("--no-continue", action="store_true")
    s.add_argument("--wait", action="store_true", help="drive in the foreground afterwards")
    s.add_argument("--timeout", type=float)
    s.set_defaults(reject=True)

    s = add("answer", cmd_answer, "answer a gate with one of its decisions")
    s.add_argument("run")
    s.add_argument("gate")
    s.add_argument("decision")
    s.add_argument("--text")
    s.add_argument("--no-continue", action="store_true")
    s.add_argument("--wait", action="store_true", help="drive in the foreground afterwards")
    s.add_argument("--timeout", type=float)

    s = add("cancel", cmd_cancel, "cancel a run (or one node)")
    s.add_argument("run", nargs="?")
    s.add_argument("--node")
    s.add_argument("--reason")

    s = add("rerun", cmd_rerun, "run a node again (and, by default, everything downstream)")
    s.add_argument("run")
    s.add_argument("node")
    s.add_argument("--only", action="store_true", help="do not re-run downstream nodes")
    s.add_argument("--cached", action="store_true", help="allow reuse if definition+inputs are unchanged")
    s.add_argument("--reason")
    s.add_argument("--no-continue", action="store_true")
    s.add_argument("--wait", action="store_true", help="drive in the foreground afterwards")
    s.add_argument("--timeout", type=float)

    s = add("amend", cmd_amend, "propose a change to a running plan (YAML: {rationale, ops})")
    s.add_argument("run")
    s.add_argument("file")
    s.add_argument("--rationale")
    s.add_argument("-y", "--yes", action="store_true", help="you are the approver: apply now")
    s.add_argument("--no-continue", action="store_true")
    s.add_argument("--wait", action="store_true", help="drive in the foreground afterwards")
    s.add_argument("--timeout", type=float)

    s = add("signal", cmd_signal, "send a named signal to waiting nodes")
    s.add_argument("run")
    s.add_argument("name")
    s.add_argument("--data", help="JSON payload")
    s.add_argument("--token", help="only wake waits armed with this token")
    s.add_argument("--no-continue", action="store_true")
    s.add_argument("--wait", action="store_true", help="drive in the foreground afterwards")
    s.add_argument("--timeout", type=float)

    s = add("note", cmd_note, "add a human note to the run record")
    s.add_argument("run")
    s.add_argument("text")
    s.add_argument("--node")

    s = add("report", cmd_report, "write report.md + report.html (decisions, outputs, files, provenance)")
    s.add_argument("run", nargs="?")

    s = add("audit", cmd_audit, "full machine-readable record of a run (stable JSON)")
    s.add_argument("run", nargs="?")

    s = add("export", cmd_export, "export the run as a Workflow Run RO-Crate (provenance)")
    s.add_argument("run", nargs="?")

    s = add("skill", cmd_skill, "install the forgeflow skill for coding agents")
    s.add_argument("target", choices=["claude", "codex", "agents", "project", "all"], nargs="?", default="project")

    add("doctor", cmd_doctor, "check harnesses, Slurm tools and the project")

    s = add("ui", cmd_ui, "local web UI: live DAG, node details, decisions, timeline, plan history")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; use an SSH tunnel)")
    s.add_argument("--token", help="fixed access token (default: random per start)")
    s.add_argument("--open", action="store_true", help="open a browser")

    s = add("mcp", cmd_mcp, "serve forgeflow over MCP (stdio)")
    s.add_argument("--read-only", action="store_true")

    s = add("fake-slurm", cmd_fake_slurm, "install a local fake Slurm (sbatch/squeue/sacct/scancel) for trying job nodes")
    s.add_argument("dir")

    s = sub.add_parser("_driver", help=argparse.SUPPRESS)
    s.add_argument("run")
    s.add_argument("--root", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_driver)
    return p


def _is_decision(args) -> bool:
    cmd = getattr(args, "cmd", None)
    if cmd in ("approve", "reject", "answer"):
        return True
    if cmd in ("run", "fork", "amend") and getattr(args, "yes", False):
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 2
    if args.cmd == "plan" and not getattr(args, "plan_cmd", None):
        parser.parse_args(["plan", "--help"])
    if getattr(args, "actor", None):
        os.environ["FORGEFLOW_ACTOR"] = args.actor
    out = Out(getattr(args, "json", False) or getattr(args, "json_top", False))
    if os.environ.get("FORGEFLOW_INSIDE_RUN") and _is_decision(args):
        return out.error(ForgeflowError(
            "inside_run", f"`forgeflow {args.cmd}` makes a decision that belongs to the user, and this process is "
            f"running inside a forgeflow node ({os.environ.get('FF_RUN_ID')}/{os.environ.get('FF_NODE_ID')})",
            "finish the node's task and report in its JSON answer; propose plan changes via an `amendment`"), 2)
    try:
        return args.fn(args, out)
    except ForgeflowError as exc:
        return out.error(exc, 2 if exc.code not in ("journal_corrupt",) else 1)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
