"""flower command line.

Exit codes (one vocabulary, Smithers 1.0): 0 ok/succeeded · 1 failed/cancelled/rejected ·
2 usage or input error · 3 parked: the run needs a decision, a signal or more time · 130 interrupted.

Every command accepts ``--json``: stdout is then exactly one object
``{"ok": bool, "data": …, "error": {code, message, suggestion}, "next": ["flower …", …]}``.
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
from .util import FlowerError, atomic_write_json, default_actor, hostname, local_clock, now_iso, parse_duration, read_json

EXIT = {"succeeded": 0, "failed": 1, "cancelled": 1, "rejected": 1, "parked": 3, "awaiting_approval": 3,
        "running": 3}


class Out:
    def __init__(self, as_json: bool):
        self.json = as_json
        self.color = (not as_json) and sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def done(self, data: Any = None, text: str | None = None, nxt: list[str] | None = None, code: int = 0) -> int:
        if self.json:
            msg = {"message": text} if text else {}   # notes (e.g. an ignored edit) reach --json callers too
            print(json.dumps({"ok": code in (0, 3), "data": data, **msg, "next": nxt or []}, default=str,
                             ensure_ascii=False))
        else:
            if text:
                print(text)
            if nxt:
                print("\nnext: " + "\n      ".join(nxt))
        return code

    def error(self, err: FlowerError, code: int = 2) -> int:
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
            raise FlowerError("bad_input", f"--input expects NAME=VALUE, got {it!r}")
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
        p = subprocess.Popen([sys.executable, "-m", "flower", "_driver", eng.paths.run_id, "--root", str(eng.paths.root)],
                             stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, start_new_session=True, close_fds=True)
    info = {"pid": p.pid, "host": hostname(), "started_at": now_iso(), "log": str(log)}
    atomic_write_json(eng.paths.driver_file, info)
    # a driver that dies at once (an import error, a bad event, ...) must not look like a running one
    size0 = log.stat().st_size if log.exists() else 0
    for _ in range(20):
        time.sleep(0.1)
        if p.poll() is not None:
            if p.returncode == 0:   # nothing left to drive (e.g. the run had already settled): a normal exit
                break
            tail = log.read_bytes()[size0:].decode(errors="replace").strip()[-600:]
            raise FlowerError("driver_died", f"the background driver of {eng.paths.run_id} exited at once "
                              f"(code {p.returncode}): {tail or 'no output'}", f"see {log}")
    return info


def run_status_code(status: str) -> int:
    return EXIT.get(status, 3)


def next_for(st, rid: str) -> list[str]:
    nxt = []
    for g in st.open_gates():
        nxt.append(f"flower show {rid} --gate {g.id}")
        nxt.append(f"flower answer {rid} {g.id} <{'|'.join(g.decisions)}> --text '<why>'")
    if st.status == "failed":
        failed = [n for n, s in st.nodes.items() if s.status == "failed"]
        if failed:
            nxt += [f"flower show {rid} {failed[0]}", f"flower rerun {rid} {failed[0]}"]
    for nid, ns in st.nodes.items():
        w = ((ns.last.progress or {}).get("wait") or {}) if ns.status == "waiting" and ns.last and not ns.gate_id else {}
        if w.get("signal"):
            nxt.append(f"flower signal {rid} {w['signal']} --data '<json>'   # {nid} waits for it")
    if st.status in ("running", "parked") and not st.open_gates():
        nxt.append(f"flower wait {rid} --timeout 600")
    if st.status == "succeeded":
        nxt.append(f"flower report {rid}")
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
                             f"`flower resume {st.run_id}`)\n")
            sys.stdout.flush()
        else:  # piped / logged: a plain narrative of new events
            evs = eng.journal.read()
            for ev in evs[seen["n"]:]:
                d = describe_event(ev)
                if d:
                    print(f"{local_clock(ev['occurredAtIso'])}  {d}", flush=True)
            seen["n"] = len(evs)

    rep = eng.drive(until="settled", timeout=timeout, on_tick=show)
    return rep.status


# ====================================================================== commands

def cmd_init(args, out: Out) -> int:
    """Make a directory a flower project, and ship the agent instructions *with the project*: the skill under
    .claude/skills and .agents/skills, a managed block in AGENTS.md, and (with --hook) the Claude Code hook
    that notices compute run beside an active run. An agent opening the project gets them whatever its
    personal setup is."""
    from . import devloop
    root = Path(args.dir or os.getcwd()).resolve()
    (root / ".flower" / "runs").mkdir(parents=True, exist_ok=True)
    msg = [f"initialised {root / '.flower'}"]
    files = []
    if not args.no_skill:
        from .skill import install_skill
        for p in install_skill(root, args.skill or "project"):
            files.append(str(p))
            msg.append(f"installed skill → {p}")
        p = devloop.write_agents_md(root)
        files.append(str(p))
        msg.append(f"agent instructions → {p}")
    if args.hook:
        p = devloop.install_hook(root, sys.executable)
        files.append(str(p))
        msg.append(f"Claude Code hook (reminds an agent that runs compute beside an active run) → {p}")
    return out.done({"root": str(root), "files": files}, "\n".join(msg),
                    ['flower start "<what the work is for>"', "flower add RUN ID -- <command>"])


def cmd_start(args, out: Out) -> int:
    """A run from minute one: an empty draft plan, approved by the person starting it, parked until steps are
    added with `flower add` (or edits picked up by `flower rerun`)."""
    from . import devloop
    root = find_root(create=True)
    pid = args.id or devloop.slug(args.goal)
    d = Path(args.dir or pid)
    p = d / "plan.yaml"
    if p.exists():
        raise FlowerError("exists", f"{p} already exists", f"run it: flower run {p}   (or choose --id / --dir)")
    inputs = parse_kv(args.input)
    if args.inputs:
        inputs.update(json.loads(Path(args.inputs).read_text()))
    d.mkdir(parents=True, exist_ok=True)
    p.write_text(devloop.draft_plan_text(pid, args.goal, inputs, args.edits))
    eng = create_run(planmod.load_plan_file(str(p)), inputs, actor=args.actor, approve=False,
                     note="draft started with `flower start`")
    by = args.actor or default_actor()
    eng.answer("plan", "approve", text="draft started with `flower start` (empty plan; steps are added as "
               f"amendments under policies.edits={args.edits})", by=by)
    eng.tick()
    rid = eng.paths.run_id
    ui = ""
    try:
        if args.no_ui or os.environ.get("FLOWER_NO_UI"):
            raise FlowerError("no_ui", "not requested")
        from . import ui as uimod
        info = uimod.ensure_background(root, actor=by)
        ui = "\nwatch it in the project's UI:\n" + "\n".join(uimod.access_text(info))
    except Exception as exc:  # noqa: BLE001  (the run does not depend on the UI)
        ui = "" if isinstance(exc, FlowerError) and exc.code == "no_ui" else f"\n(UI not started: {exc}; `flower ui`)"
    return out.done({"run_id": rid, "plan": str(p)},
                    f"run {rid} started for: {args.goal}\nplan file: {p} (empty draft){ui}",
                    [f"flower add {rid} <id> -- <command>", f"flower status {rid}"])


def cmd_add(args, out: Out) -> int:
    """Write a shell step into the run's plan file and run it (`flower rerun RUN ID --follow` semantics): the
    recorded way costs one command, like running it by hand."""
    from . import devloop
    eng = get_engine(args)
    st = eng.state()
    src = st.meta.get("plan_source")
    if not src or not Path(src).is_file():
        raise FlowerError("no_plan_file", f"run {st.run_id} has no plan file to add to ({src})",
                          "write the step into an amendment: flower amend RUN change.yaml")
    node = devloop.node_from_args(args, args.command)
    cur = planmod.load_plan_file(src)
    if any(isinstance(n, dict) and n.get("id") == args.id for n in cur.get("nodes") or []):
        raise FlowerError("exists", f"step {args.id!r} is already in {src}",
                          f"edit it there, then: flower rerun {st.run_id} {args.id} --follow")
    text = Path(src).read_text()
    new_text = devloop.insert_node(text, node)
    tmp = Path(src).with_name(f".{Path(src).name}.flower-add")
    tmp.write_text(new_text)
    try:
        planmod.check(planmod.load_plan_file(str(tmp)))
    except planmod.PlanInvalid:
        tmp.unlink()
        raise
    os.replace(tmp, src)
    ns = argparse.Namespace(run=args.run, node=args.id, only=False, cached=False, no_continue=False, wait=False,
                            reason=f"added with `flower add`", follow=not args.no_follow, no_edits=False,
                            yes=args.yes, input=args.input, timeout=args.timeout, json=args.json, actor=args.actor)
    if not out.json:
        print(f"added step {args.id} to {src}", flush=True)
    return cmd_rerun(ns, out)


def cmd_hook(args, out: Out) -> int:
    """Harness hook (stdin: the harness's JSON). Never fails the tool call; prints a reminder at most."""
    from . import devloop
    try:
        data = json.loads(sys.stdin.read() or "{}")
        msg = devloop.reminder(str((data.get("tool_input") or {}).get("command") or ""),
                               Path(data.get("cwd") or os.getcwd()), data.get("session_id"))
        if msg:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
    except Exception:  # noqa: BLE001
        pass
    return 0


def cmd_plan(args, out: Out) -> int:
    from .render import plan_overview
    if args.plan_cmd == "validate":
        raw = planmod.load_plan_file(args.file)
        plan = planmod.check(raw)
        return out.done({"valid": True, "id": plan["id"], "nodes": len(plan["nodes"]), "digest": planmod.plan_digest(plan)},
                        f"✓ {args.file} is valid: {len(plan['nodes'])} nodes, digest {planmod.plan_digest(plan)[7:19]}",
                        [f"flower plan show {args.file}", f"flower run {args.file}"])
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
            raise FlowerError("exists", f"{p} already exists", "use --force to overwrite")
        p.write_text(PLAN_TEMPLATE.replace("{id}", p.stem))
        return out.done({"file": str(p)}, f"wrote {p} — edit it, then `flower plan validate {p}`")
    if args.plan_cmd == "reference":
        from .skill import PLAN_REFERENCE
        return out.done({"reference": PLAN_REFERENCE}, PLAN_REFERENCE)
    raise FlowerError("usage", "unknown plan command")


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
            print(f"\nNot started. Approve later with:  flower approve {rid}")
            return 3
    if not approve:
        nxt = [f"flower show {rid} --gate plan", f"flower approve {rid} --note '<who approved and why>'",
               f"flower reject {rid} --text '<what to change>'"]
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
                        [f"flower watch {rid}", f"flower wait {rid} --timeout 600", f"flower status {rid}"],
                        code=0 if st.status not in TERMINAL_RUN else run_status_code(st.status))
    try:
        status = live_drive(eng, out, timeout=getattr(args, "timeout", None))
    except KeyboardInterrupt:
        st = eng.state()
        return out.done(summary_data(eng, st), f"\ndetached from {rid} (status {st.status}); "
                        f"running nodes continue. Resume: flower resume {rid}", code=130)
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
                        [f"flower approve {nst.run_id}"], code=3)
    eng.answer("plan", "approve", text=args.note or f"fork of {st.run_id}", by=args.actor or default_actor())
    return _continue(eng, args, out)


def cmd_resume(args, out: Out) -> int:
    eng = get_engine(args)
    st = eng.state()
    if st.status == "awaiting_approval" and not st.gates["plan"].status == "answered":
        raise FlowerError("not_approved", "the plan has not been approved yet", f"flower approve {st.run_id}")
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
    note = ""
    if not args.no_tick and st.status not in TERMINAL_RUN and st.status != "awaiting_approval" and not driver_alive(eng.paths):
        eng.tick()
        st = eng.state()
        if st.status == "running":   # work under way and nobody driving it: start a driver (and say so)
            try:
                spawn_driver(eng)
                note = "\n(no background driver was running; started one)"
            except FlowerError as exc:
                note = f"\nwarning: no background driver, and starting one failed: {exc.message}"
    elif args.no_tick and st.status == "running" and not driver_alive(eng.paths):
        note = f"\nwarning: running, but no background driver is alive: flower resume {st.run_id} --detach"
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color, all_items=getattr(args, "items", False)) + note,
                    next_for(st, st.run_id) if out.json else None,
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
        except FlowerError:
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
                raise FlowerError("gate_not_found", f"no gate {gid!r}", f"gates: {', '.join(st.gates)}")
            gid = cand[0]
        g = st.gates[gid]
        return out.done(g.__dict__, gate_detail(st, gid))
    if args.node:
        if args.node not in st.nodes:
            raise FlowerError("node_not_found", f"no node {args.node!r}", f"nodes: {', '.join(st.graph().order)}")
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
                        print(f"{local_clock(ev['occurredAtIso'])}  {d}", flush=True)
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
        raise FlowerError("no_attempt", f"node {args.node} has not run yet")
    a = ns.attempts[-1] if not args.attempt else next((x for x in ns.attempts if x.n == args.attempt), None)
    if a is None:
        raise FlowerError("no_attempt", f"node {args.node} has no attempt {args.attempt}",
                             f"attempts: {', '.join(str(x.n) for x in ns.attempts)}")
    adir = eng.paths.attempt_dir(args.node, a.n)
    spec = st.node_spec(args.node)
    if spec.get("kind") == "agent" and not args.raw:
        text = render_transcript(adir, (spec.get("harness") or {}).get("name", "claude"))
    elif planmod.on_cluster(spec):
        from .hpc import log_files
        jd = Path(a.job.get("job_dir") or "")
        local = jd if jd.exists() else adir / "job"
        jid = a.job.get("job_id")
        parts = []
        for name in (log_files((st.plan.get("clusters") or {}).get(spec.get("cluster")) or {}, jid) if jid else []):
            p = local / name
            if p.exists():
                parts.append(f"==> {p} <==\n" + p.read_text(errors="replace")[-20000:])
        _outputs_part(parts, local / "outputs.json", a)
        text = "\n".join(parts) or "(no job output yet)"
    else:
        parts = []
        for name in ("stdout.log", "stderr.log"):
            p = adir / "proc" / name
            if p.exists() and p.stat().st_size:
                parts.append(f"==> {name} <==\n" + p.read_text(errors="replace")[-20000:])
        _outputs_part(parts, adir / "outputs.json", a)
        text = "\n".join(parts) or "(no output)"
    return out.done({"text": text, "dir": str(adir)}, text)


def _outputs_part(parts: list, path: Path, attempt) -> None:
    """A failed attempt's outputs file: what the step wrote before failing, otherwise invisible (`flower output`
    shows successful results only)."""
    if attempt.status != "succeeded" and path.is_file() and path.stat().st_size:
        parts.append(f"==> {path.name} (written by this attempt, which did not succeed) <==\n"
                     + path.read_text(errors="replace")[-20000:])


def cmd_output(args, out: Out) -> int:
    eng = get_engine(args)
    st = eng.state()
    ns = st.nodes.get(args.node)
    if not ns or not ns.result:
        raise FlowerError("no_result", f"node {args.node} has no successful result")
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
                raise FlowerError("no_key", f"node {args.node} has no output {args.key!r}",
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
            raise FlowerError("which_gate", f"{len(opens)} open gates; say which",
                                 "open gates: " + ", ".join(g.id for g in opens) if opens else "nothing to approve")
        gid = opens[0].id
    g = st.gates.get(gid) or next((x for x in st.open_gates() if x.node == gid), None)
    if g is None:
        raise FlowerError("gate_not_found", f"no gate {gid!r} in run {st.run_id}",
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


def _pick_up_edits(eng: Engine, args, out: Out, node: str | None, via: str) -> tuple[list, int | None]:
    """The development loop: edits to the plan file become a recorded amendment. Returns (notes, exit code if the
    edit waits for approval). ``node`` None: the whole file (`flower sync`)."""
    by = args.actor or default_actor()
    notes: list = []
    eng.tick()  # apply approvals answered since the last pass (e.g. of an earlier edit)
    ed = eng.plan_edits(node, new_inputs=parse_kv(getattr(args, "input", None)))
    notes.extend(f"note: {x}" for x in ed.get("ignored") or [])
    if not ed["ops"]:
        return notes, None
    st = eng.state()
    policy = ((st.plan.get("policies") or {}).get("edits") or "ask")
    auto = bool(args.yes) or policy == "all" or (policy == "unfinished" and not ed["touches_finished"])
    what = ", ".join([f"changed {', '.join(ed['changed'])}"] * bool(ed["changed"])
                     + [f"added {', '.join(ed['added'])}"] * bool(ed["added"])
                     + [f"new input {', '.join(ed['new_inputs'])}"] * bool(ed.get("new_inputs"))
                     + [f"new cluster {', '.join(ed['new_clusters'])}"] * bool(ed.get("new_clusters"))
                     + [f"cluster {', '.join(ed['tuned_clusters'])}"] * bool(ed.get("tuned_clusters")))
    waiting = next((a for a in st.amendments.values() if a.status == "proposed" and a.ops == ed["ops"]), None)
    if waiting is not None and not auto:  # the same edit is already waiting for a decision
        aid = waiting.id
    else:
        aid = eng.propose_amendment(ed["ops"], f"plan file edited ({what}); picked up by `{via}`", by=by,
                                    auto_approve=auto)
    st = eng.state()
    am = st.amendments.get(aid)
    if am is None or am.status != "approved":
        return notes, out.done({"amendment_id": aid, "gate": f"amend-{aid}", "changed": ed["changed"],
                                "added": ed["added"]},
                               f"the plan file changed ({what}); that edit needs approval first:\n"
                               f"  flower show {st.run_id} --gate amend-{aid}      # the diff\n"
                               f"  flower approve {st.run_id} amend-{aid}\n"
                               f"then run `{via}` again (or allow such edits: `policies: {{edits: unfinished}}` "
                               f"in the plan, or `--yes` when you are the approver)", code=3)
    notes.append(f"plan edits applied as amendment {aid} (generation {st.generation}): {what}")
    return notes, None


def cmd_sync(args, out: Out) -> int:
    """Make the run follow its plan file: new steps and inputs, edited unfinished steps, a cluster's pacing settings
    (cpus, max_jobs, min_poll). Nothing is re-run on purpose (that is `flower rerun`)."""
    eng = get_engine(args)
    notes, rc = _pick_up_edits(eng, args, out, None, f"flower sync {eng.state().run_id}")
    if rc is not None:
        return rc
    if not any(n.startswith("plan edits applied") for n in notes):
        notes.append("the run already follows its plan file")
    return _record_and_continue(eng, args, out, "\n".join(notes))


def cmd_rerun(args, out: Out) -> int:
    eng = get_engine(args)
    by = args.actor or default_actor()
    notes = []
    if not args.no_edits:
        notes, rc = _pick_up_edits(eng, args, out, args.node, f"flower rerun {args.node}")
        if rc is not None:
            return rc
    st = eng.state()
    ns = st.nodes.get(args.node)
    if ns is not None and ns.status in ("running", "waiting", "retrying"):
        notes.append(f"{args.node} is already {ns.status}")
    elif ns is not None:
        try:
            targets = eng.rerun(args.node, downstream=not args.only, force=not args.cached, by=by, reason=args.reason,
                                keep_state=getattr(args, "keep_state", False))
            notes.append(f"queued again: {', '.join(targets)}")
        except FlowerError as exc:
            if not (args.cached and exc.code == "node_running" and notes):
                raise
            # --cached with an edit while items run: the edit applies, running items finish as they are, items not
            # started yet use the new definition, finished ones keep their results unless the change affects them
            notes.append(f"edit applied without interrupting running work ({exc.message})")
    elif args.node in st.graph().nodes:
        notes.append(f"{args.node} is new: it runs when its dependencies are done")
    else:
        raise FlowerError("node_not_found", f"no node {args.node!r} in the run or the plan file")
    if args.follow:
        if not out.json:
            print("\n".join(notes), flush=True)
        return _follow(eng, args.node, out, timeout=args.timeout)
    return _record_and_continue(eng, args, out, "\n".join(notes))


def _follow(eng: Engine, node: str, out: Out, timeout: float | None = None) -> int:
    """Watch one node until it finishes: its events, its live output (local), then its result. The rest of the run
    goes on in the background driver."""
    from .render import describe_event
    from .rundir import fs_name
    from .state import TERMINAL_NODE
    from .util import tail_text
    st = eng.state()
    g = st.graph()
    watch = {node} | {c for c, s in g.nodes.items() if s.get("expanded_from") == node}
    start_n = st.nodes[node].last.n if node in st.nodes and st.nodes[node].last else 0
    eng.paths.follow.mkdir(parents=True, exist_ok=True)
    marker = eng.paths.follow / f"{fs_name(node)}.json"
    atomic_write_json(marker, {"node": node, "pid": os.getpid(), "host": hostname(), "at": now_iso()})
    seen = len(eng.journal.read())
    offsets: dict = {}
    t0 = time.time()
    try:
        eng.tick()
        spawn_driver(eng)
        while True:
            evs = eng.journal.read()
            for ev in evs[seen:]:
                nid = ev.get("nodeId")
                if nid in watch or (nid and g.nodes.get(nid, {}).get("expanded_from") == node):
                    d = describe_event(ev)
                    if d and not out.json:
                        print(f"{local_clock(ev['occurredAtIso'])}  {d}", flush=True)
            seen = len(evs)
            st = eng.state()
            ns = st.nodes.get(node)
            if st.status not in TERMINAL_RUN and not driver_alive(eng.paths):
                spawn_driver(eng)   # never wait on a run that nobody drives (a driver that died, or exited idle)
            if not out.json and ns and ns.last and ns.last.status == "running":
                a = ns.last
                adir = eng.paths.attempt_dir(node, a.n)
                jd = Path(a.job.get("job_dir") or "")
                for p in [adir / "proc" / "stdout.log", adir / "proc" / "stderr.log", jd / "job.out", jd / "job.err"]:
                    if str(jd) not in ("", ".") or p.parent.name == "proc":
                        if p.is_file():
                            off = offsets.get(p, 0)
                            data = p.read_bytes()[off:]
                            if data:
                                sys.stdout.write(data.decode(errors="replace"))
                                sys.stdout.flush()
                                offsets[p] = off + len(data)
            done = ns is not None and ns.status in TERMINAL_NODE and ns.last is not None and ns.last.n > start_n
            if done or st.status in TERMINAL_RUN and (ns is None or ns.status in TERMINAL_NODE):
                break
            if timeout is not None and time.time() - t0 > timeout:
                return out.done({"node": node, "status": ns.status if ns else None},
                                f"{node} still {ns.status if ns else 'pending'} after {timeout:.0f}s (it keeps going)",
                                code=3)
            time.sleep(0.3)
    except KeyboardInterrupt:
        return out.done({"node": node}, f"stopped following {node}; it keeps going", code=130)
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    ns = eng.state().nodes.get(node)
    r = ns.last if ns else None
    ok = ns is not None and ns.status == "succeeded"
    if ok:
        text = f"✓ {node}: {r.summary or 'succeeded'}"
    else:
        err = (r.error or {}) if r else {}
        text = f"✗ {node} {ns.status if ns else '?'}: {err.get('error_class', '')} {err.get('message', '')}".rstrip()
        if r:
            for p in (eng.paths.attempt_dir(node, r.n) / "proc" / "stderr.log",):
                tail = tail_text(p, 1500).strip()
                if tail:
                    text += "\n--- stderr ---\n" + tail
    return out.done({"node": node, "status": ns.status if ns else None, "outputs": r.outputs if r else None,
                     "error": r.error if r else None}, text,
                    [f"flower logs {eng.paths.run_id} {node}"] if not ok else None, code=0 if ok else 1)


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
                    f"amendment {aid} proposed; it needs approval:\n  flower show {st.run_id} --gate amend-{aid}\n"
                    f"  flower approve {st.run_id} amend-{aid}", code=3)


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


def _engine_at(ref: str) -> Engine:
    """A run by id (this project) or by the path of its directory (another project, e.g. a fresh clone)."""
    p = Path(ref).expanduser()
    if p.is_dir() and (p / "events.jsonl").is_file():
        return Engine(RunPaths(p.parent.parent.parent, p.name))
    return Engine(resolve_run(find_root(), ref))


def _diff_values(a, b, rtol: float, atol: float, path: str, out: list) -> None:
    if isinstance(a, bool) or isinstance(b, bool) or not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if k not in COMPARE_SKIP:
                    _diff_values(a.get(k), b.get(k), rtol, atol, f"{path}.{k}", out)
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                _diff_values(x, y, rtol, atol, f"{path}[{i}]", out)
        elif a != b:
            out.append({"key": path, "a": a, "b": b, "rel": None})
        return
    rel = abs(a - b) / max(abs(a), abs(b), 1e-300)
    if abs(a - b) > atol and rel > rtol:
        out.append({"key": path, "a": a, "b": b, "rel": rel})


# outputs that name places or processes, not results
COMPARE_SKIP = {"job_id", "job_dir", "local_dir", "dir", "prefix", "seconds", "seconds_opt", "summary", "rationale"}


def cmd_compare(args, out: Out) -> int:
    """Do two runs of a workflow (a rerun, a fork, a fresh clone on another machine) give the same results?
    Compares the outputs of every step both have, numbers within --rtol, everything else exactly."""
    ea, eb = _engine_at(args.a), _engine_at(args.b)
    sa, sb = ea.state(), eb.state()
    rows, same, missing = [], 0, []
    for nid in sorted(set(sa.nodes) | set(sb.nodes)):
        ra = sa.nodes[nid].result if nid in sa.nodes and sa.nodes[nid].status == "succeeded" else None
        rb = sb.nodes[nid].result if nid in sb.nodes and sb.nodes[nid].status == "succeeded" else None
        if ra is None or rb is None:
            missing.append(nid)
            continue
        g = sa.graph().nodes.get(nid) or {}
        if g.get("foreach") is not None:   # a collector repeats its items' outputs: compare the items themselves
            continue
        oa = {k: v for k, v in (ra.outputs or {}).items() if k not in COMPARE_SKIP and k not in set(args.ignore or [])}
        ob = {k: v for k, v in (rb.outputs or {}).items() if k not in COMPARE_SKIP and k not in set(args.ignore or [])}
        diffs: list = []
        _diff_values(oa, ob, args.rtol, args.atol, "", diffs)
        if diffs:
            rows.append({"node": nid, "diffs": diffs})
        else:
            same += 1
    lines = [f"{sa.run_id}  vs  {sb.run_id}: {same} step(s) agree within rtol {args.rtol:g}, "
             f"{len(rows)} differ, {len(missing)} not succeeded in both"]
    for r in rows:
        for d in r["diffs"][:6]:
            rel = f"  (rel {d['rel']:.2e})" if d["rel"] is not None else ""
            lines.append(f"  {r['node']}{d['key']}: {json.dumps(d['a'], default=str)[:40]}  vs  "
                         f"{json.dumps(d['b'], default=str)[:40]}{rel}")
    if missing:
        lines.append(f"  not compared: {', '.join(missing[:12])}" + (" ..." if len(missing) > 12 else ""))
    return out.done({"same": same, "differ": rows, "not_compared": missing}, "\n".join(lines),
                    code=0 if not rows else 1)


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
    except FlowerError as exc:
        chk("project", False, exc.message)
    text = "\n".join(f"{'✓' if c['ok'] else '·'} {c['check']:<16} {c['detail']}" for c in checks)
    return out.done(checks, text)


def _code_stamp() -> tuple:
    """Newest modification time and file count of flower's own source (cheap: a few dozen stats)."""
    pkg = Path(__file__).resolve().parent
    files = [p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts]
    return (max((p.stat().st_mtime_ns for p in files), default=0), len(files))


def cmd_driver(args, out: Out) -> int:  # internal: background driver loop
    root = Path(args.root)
    eng = Engine(RunPaths(root, args.run))
    atomic_write_json(eng.paths.driver_file, {"pid": os.getpid(), "host": hostname(), "started_at": now_iso()})
    eng.emit("driver.started", {"pid": os.getpid(), "host": hostname()})
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
    idle_limit = float(os.environ.get("FLOWER_DRIVER_IDLE_EXIT", 6 * 3600))
    idle = 0.0
    code = _code_stamp()
    errors = 0
    try:
        while not stop["flag"]:
            try:
                rep = eng.drive(until="settled", timeout=60)
                errors = 0
            except Exception as exc:  # noqa: BLE001
                # e.g. flower's code half-written by an edit while we import it: log, pause, carry on (the state is
                # in the journal); a persistent failure still ends the driver after a while
                errors += 1
                import traceback
                traceback.print_exc()
                if errors >= 20:
                    raise
                eng.emit("driver.error", {"pid": os.getpid(), "error": f"{type(exc).__name__}: {exc}"[:300],
                                          "consecutive": errors})
                time.sleep(min(30, 2 * errors))
                continue
            if _code_stamp() != code:
                # flower itself was updated (an agent improving it mid-campaign): continue with the new code.
                # Nothing is lost: the state is in the journal and jobs are detached processes.
                eng.emit("driver.reloaded", {"pid": os.getpid(), "reason": "flower's code changed"})
                os.execv(sys.executable, [sys.executable, "-m", "flower", "_driver", args.run, "--root", str(root)])
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
    from .ui import access_text, background_info, ensure_background, serve, stop_background
    root = find_root()
    action = args.action or "start"
    if action == "stop":
        info = stop_background(root)
        return out.done({"stopped": bool(info), "root": str(root)},
                        f"stopped the UI server of {root} (pid {info['pid']})" if info else
                        f"no UI server is running for {root}")
    if action == "status":
        info = background_info(root)
        if not info:
            return out.done({"running": False, "root": str(root)}, f"no UI server is running for {root}",
                            ["flower ui"])
        return out.done({"running": True, **info},
                        "\n".join([f"flower ui for {root}  (running, pid {info['pid']})", *access_text(info)]))
    if not args.foreground:
        if args.host not in ("127.0.0.1", "localhost"):
            raise FlowerError("usage", "--host only applies to --foreground (the background server is local-only)",
                              "flower ui --foreground --host ...")
        info = ensure_background(root, actor=args.actor, port=args.port)
        how = "already running" if info.get("reused") else "started"
        text = [f"flower ui for {root}  ({how} in the background, pid {info['pid']}; stop it: flower ui stop)",
                *access_text(info),
                "  runs keep going without it; the UI only reads the journal and sends the same actions as the CLI"]
        if args.open:
            import webbrowser
            webbrowser.open(f"http://localhost:{info['port']}/?token={info['token']}")
        return out.done(info, "\n".join(text))

    port = args.port or 8765
    sock = Path(args.socket) if args.socket else None

    def ready(srv, ui):
        host, p = srv.server_address[:2]
        url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{p}/?token={ui.token}"
        lines = [f"flower ui for {root}", f"  open: {url}", f"  acting as: {ui.actor}  (decisions you make here are recorded under this name)"]
        if sock:
            lines.append(f"  owner-only socket (no token needed through it): {sock}")
        if host in ("127.0.0.1", "localhost"):
            lines.append(f"  remote machine? tunnel first:  ssh -N -L {p}:localhost:{p} <this-host>")
        lines.append("  ctrl-c to stop (runs keep going; the UI only reads the journal and sends the same actions as the CLI)")
        if out.json:
            print(json.dumps({"ok": True, "data": {"url": url, "root": str(root), "actor": ui.actor}}), flush=True)
        else:
            print("\n".join(lines), flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)

    try:
        serve(root, host=args.host, port=port, actor=args.actor, token=args.token, ready=ready, unix_socket=sock)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        raise FlowerError("ui_port", f"cannot listen on {args.host}:{port}: {exc}", "pick another --port") from None
    return 0


def _local_port_free(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _local_port_answers(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def cmd_open(args, out: Out) -> int:
    """On your laptop: start (or reuse) a project's UI on a remote machine, tunnel to it, open the browser."""
    import shlex
    host, sep, path = args.target.partition(":")
    if not sep or not host or not path:
        raise FlowerError("usage", f"expected [user@]host:/path/to/project, got {args.target!r}",
                          "e.g. flower open me@cluster.example.org:~/projects/si-study")
    ssh = ["ssh", *(args.ssh_arg or [])]
    remote = f"cd {path if path.startswith('~') else shlex.quote(path)} && {args.flower} ui --json"
    try:  # stderr is not captured: password / MFA prompts and ssh errors reach the terminal
        r = subprocess.run(ssh + [host, "bash -lc " + shlex.quote(remote)], stdout=subprocess.PIPE, text=True,
                           timeout=180)
    except subprocess.TimeoutExpired:
        raise FlowerError("remote", f"no answer from {host} within 180 s") from None
    info = None
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("ok") and isinstance(doc.get("data"), dict) and doc["data"].get("socket"):
            info = doc["data"]
            break
    if info is None:
        raise FlowerError("remote", f"could not start the UI on {host} (ssh exit {r.returncode})",
                          f"check that `{args.flower}` runs there (--flower /path/to/flower) and that {path} "
                          "is a flower project")
    port = args.port or int(info["port"])
    if not _local_port_free(port):
        port = next((p for p in range(port + 1, port + 100) if _local_port_free(p)), port)
    url = f"http://localhost:{port}/"
    tunnel = None
    for fwd, link in ((f"{port}:{info['socket']}", url),  # owner-only socket: no token in the link
                      (f"{port}:localhost:{info['port']}", f"{url}?token={info['token']}")):  # sshd forbids sockets
        tunnel = subprocess.Popen(ssh + ["-N", "-o", "ExitOnForwardFailure=yes", "-L", fwd, host])
        deadline = time.time() + 30
        while tunnel.poll() is None and time.time() < deadline and not _local_port_answers(port):
            time.sleep(0.2)
        if tunnel.poll() is None and _local_port_answers(port):
            url = link
            break
        tunnel.terminate()
        tunnel = None
    if tunnel is None:
        raise FlowerError("remote", f"could not open a tunnel to {host}", "run the ssh command from `flower ui` by hand")
    print(f"flower ui of {info['root']} on {host}\n  open: {url}\n  (keep this running; ctrl-c closes the tunnel, "
          "the UI and your runs keep going on the remote machine)", flush=True)
    def _close(*_):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):  # a closed terminal window must not orphan the tunnel
        signal.signal(sig, _close)
    try:
        if not args.no_browser:
            import webbrowser
            webbrowser.open(url)
        tunnel.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
    return 0


def _plan_cluster(args) -> tuple[dict, Path]:
    """A cluster's settings as a run of ``args.plan`` would use them (inputs from -i / --inputs), or as the run
    ``args.run`` uses them (its clusters were rendered with its inputs when they were defined)."""
    from . import template as tpl
    from .engine import coerce_inputs
    if bool(getattr(args, "run", None)) == bool(args.plan):
        raise FlowerError("usage", "give --run RUN (a run's clusters) or --plan PLAN (a plan file's)",
                          "inside a draft, --run RUN needs no -i / --inputs: the run has them")
    if args.run:
        eng = get_engine(args)
        st = eng.state()
        clusters = {planmod.LOCAL_CLUSTER: {"transport": "local", "scheduler": "none"}, **(st.plan.get("clusters") or {})}
        if args.cluster not in clusters:
            raise FlowerError("usage", f"run {st.run_id} has no cluster {args.cluster!r}",
                              f"clusters: {', '.join(clusters) or 'none'} (a new one: add it to the plan file, "
                              f"then `flower sync {st.run_id}`)")
        src = Path((st.plan.get("_source") or {}).get("dir") or st.meta.get("plan_dir") or ".")
        return {**clusters[args.cluster], "_name": args.cluster}, src
    raw = planmod.load_plan_file(args.plan)
    plan = planmod.normalize(raw)  # not validated: the environment being explored may not be frozen yet
    src = Path((plan.get("_source") or {}).get("dir") or ".")
    inputs = parse_kv(args.input)
    if args.inputs:
        inputs.update(json.loads(Path(args.inputs).read_text()))
    values = coerce_inputs(plan.get("inputs") or {}, inputs, Path.cwd())
    res = tpl.make_resolver({"inputs": values, "env": dict(os.environ), "plan": {"dir": str(src), "id": plan.get("id")}})
    clusters = {planmod.LOCAL_CLUSTER: {"transport": "local", "scheduler": "none"},   # always there: this machine
                **tpl.render(plan.get("clusters") or {}, res)}
    if args.cluster not in clusters:
        raise FlowerError("usage", f"plan {args.plan} has no cluster {args.cluster!r}",
                          f"clusters: {', '.join(clusters) or 'none'}")
    c = dict(clusters[args.cluster])
    c["_name"] = args.cluster
    return c, src


def _remote_home(tr) -> str:
    if tr.is_local:
        return os.path.expanduser("~")
    r = tr.run("echo $HOME", timeout=60)
    if r.rc != 0 or not r.out.strip():
        raise FlowerError("remote", f"cannot reach the cluster: {(r.err or r.out).strip()[-300:]}")
    return r.out.strip().splitlines()[-1]


def _prelude(c: dict) -> str:
    pre = c.get("prelude") or []
    pre = pre if isinstance(pre, list) else [pre]
    mods = [f"module load {m}" for m in (c.get("modules") or [])]
    return "".join(f"{x}\n" for x in [*mods, *pre] if str(x).strip())


def cmd_remote(args, out: Out) -> int:
    """Run one command on a cluster, logged (for agents exploring an environment)."""
    from . import envs as envmod
    from .hpc.transport import make_transport
    cmd = list(args.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise FlowerError("usage", "no command given", "flower remote exec --run RUN --cluster C -- <command>")
    c, src = _plan_cluster(args)
    tr = make_transport(c)
    text = " ".join(cmd) if len(cmd) > 1 else cmd[0]
    pre = _prelude(c)
    d = None
    if args.env and getattr(args, "installed", False):
        # use the frozen recipe's own installation there (what steps get), e.g. to try an API before writing a step
        d = envmod.find(args.env, src)
        state, fz = envmod.status(d) if d is not None else ("missing", None)
        if state != "frozen":
            raise FlowerError("env_not_frozen", f"--installed needs a frozen recipe; {args.env} is {state}",
                              f"flower env freeze {args.env}")
        pfx = envmod.prefix(args.env, fz["hash"])
        pre += (f'export FLOWER_ENV_PREFIX="{pfx}"; export FLOWER_ENV_DIR="$FLOWER_ENV_PREFIX.recipe"\n'
                'if [ ! -d "$FLOWER_ENV_PREFIX" ]; then echo "not installed on this cluster: $FLOWER_ENV_PREFIX '
                '(a step using it, or flower env replay, installs it)" >&2; exit 3; fi\n'
                'set +u; source "$FLOWER_ENV_DIR/activate.sh"\n')
        args.probe = True
    elif args.env:  # explore with what a real setup gets: a prefix to install into, and the activation so far
        d = envmod.find(args.env, src) or (src / envmod.ENVS_DIR / args.env)
        pre += (f'export FLOWER_ENV_PREFIX="$HOME/.flower/envs/{args.env}-explore"\n'
                'export FLOWER_ENV_DIR="$FLOWER_ENV_PREFIX.recipe"; mkdir -p "$FLOWER_ENV_DIR"\n')
        # the recipe as written so far, so exploration can run its own check.sh / setup.sh there
        import base64
        import shlex
        for rel in (envmod.recipe_files(d) if d.is_dir() else {}):
            f = d / rel
            if f.is_file() and f.stat().st_size <= 4 * 1024 * 1024:
                q = shlex.quote(rel)
                pre += (f'mkdir -p "$FLOWER_ENV_DIR/$(dirname {q})"; printf %s '
                        f'{base64.b64encode(f.read_bytes()).decode()} | base64 -d > "$FLOWER_ENV_DIR/"{q}\n')
        act = d / "activate.sh"
        if act.is_file() and act.read_text() != envmod.TEMPLATES["activate.sh"]:
            pre += "set +u\n" + act.read_text() + "\n"
    t0 = time.time()
    r = tr.run(pre + text, timeout=args.timeout)
    entry = {"at": now_iso(), "cluster": c["_name"], "cmd": text, "rc": r.rc,
             "probe": bool(args.probe), "seconds": round(time.time() - t0, 2),
             "out_tail": (r.out or "")[-2000:], "err_tail": (r.err or "")[-2000:], "by": args.actor or default_actor()}
    logged = str(envmod.log_session(d, entry)) if d is not None else None
    if out.json:
        return out.done({**entry, "out": r.out, "err": r.err, "log": logged}, code=0 if r.rc == 0 else 1)
    sys.stdout.write(r.out or "")
    sys.stderr.write(r.err or "")
    return r.rc if r.rc < 256 else 1


def _env_dir(args, src: Path | None = None) -> Path:
    from . import envs as envmod
    d = envmod.find(args.name, getattr(args, "dir", None) or src)
    if d is None:
        raise FlowerError("env_not_found", f"no recipe envs/{args.name}/ here or in a parent directory",
                          f"flower env new {args.name}")
    return d


def cmd_env(args, out: Out) -> int:
    from . import envs as envmod
    act = args.env_action
    if act == "new":
        d = Path(args.dir or ".").resolve() / envmod.ENVS_DIR / args.name
        made = envmod.new(d)
        return out.done({"dir": str(d), "created": made},
                        f"recipe {d}: " + (", ".join(made) + " created from templates" if made else "already there"),
                        [f"flower remote exec --env {args.name} --plan PLAN --cluster C -- <command>   # explore",
                         f"flower env freeze {args.name}"])
    if act == "freeze":
        d = _env_dir(args)
        fz = envmod.freeze(d, by=args.actor or default_actor())
        msg = (f"recipe {d.name} unchanged (frozen {envmod.short(fz['hash'])})" if fz.get("unchanged") else
               f"froze {d.name} as {envmod.short(fz['hash'])}" + (" (setup.sh drafted from the logged commands: "
                                                                     "review it)" if fz.get("drafted_setup") else ""))
        lc = envmod.last_check(d)
        if lc is not None and lc.get("rc") != 0:
            msg += (f"\nwarning: the last check.sh run while exploring ({lc.get('at')}) failed (exit {lc.get('rc')}); "
                    "frozen anyway: `flower env replay --fresh` will show whether it works")
            fz = {**fz, "warning": "last exploration check failed"}
        return out.done(fz, msg, [f"flower env replay {d.name} --plan PLAN --cluster C --fresh   # prove it repeats"])
    if act == "show":
        from .util import read_json
        root = Path(args.dir or ".").resolve()
        names = [args.name] if args.name else sorted(
            {p.name for d in [root, *root.parents] for p in (d / envmod.ENVS_DIR).glob("*") if p.is_dir()})
        rows = []
        for n in names:
            d = envmod.find(n, root)
            if d is None:
                continue
            state, fz = envmod.status(d)
            rows.append({"name": n, "dir": str(d), "state": state, "hash": (fz or {}).get("hash"),
                         "replays": (fz or {}).get("replays", [])[-3:],
                         "sessions": len(envmod.session_commands(d))})
        text = "\n".join(f"{r['name']:<16} {r['state']:<8} {envmod.short(r['hash'] or '') or '-':<13} "
                         f"{r['sessions']} logged command(s), {len(r['replays'])} recent replay(s)  {r['dir']}"
                         for r in rows) or "no environment recipes here"
        return out.done({"envs": rows}, text)
    # replay / check: on a cluster of a plan
    from .hpc.transport import make_transport
    import shlex
    c, src = _plan_cluster(args)
    d = _env_dir(args, src)
    state, fz = envmod.status(d)
    if state != "frozen":
        raise FlowerError("env_not_frozen", f"recipe {d} is {state}", f"flower env freeze {args.name}")
    h = fz["hash"]
    tr = make_transport(c)
    home = _remote_home(tr)
    staging = f"{home}/.flower/envs/.staging/{args.name}-{envmod.short(h)}"
    r = tr.put_tree(d, staging)
    if r.rc != 0:
        raise FlowerError("remote", f"could not upload the recipe: {(r.err or '').strip()[-300:]}")
    fresh = act == "replay" and args.fresh
    pfx = f"{home}/.flower/envs/{args.name}-{envmod.short(h)}" + (f"-replay-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}" if fresh else "")
    ct = envmod.settings(d).get("check_timeout")
    script = envmod.setup_script(args.name, h, recipe_dir=staging, allow_install=(act == "replay"), fresh=fresh,
                                 env_prefix=pfx, check_timeout=parse_duration(ct) if ct else None)
    t0 = time.time()
    r = tr.run(_prelude(c) + f"cd {shlex.quote(staging)} && {{\n{script}}}",
               timeout=args.timeout)
    ok = r.rc == 0
    rec = {"at": now_iso(), "action": act, "cluster": c["_name"], "fresh": fresh,
           "prefix": pfx, "ok": ok, "rc": r.rc, "seconds": round(time.time() - t0, 1),
           "tail": ((r.out or "") + (r.err or ""))[-1500:], "by": args.actor or default_actor()}
    envmod.record_replay(d, rec)
    text = (r.out or "") + (r.err or "")
    head = (f"{act} {args.name} ({envmod.short(h)}) on {c['_name']}: " + ("OK" if ok else f"FAILED (exit {r.rc})")
            + f" in {rec['seconds']}s, prefix {pfx}")
    if not ok:
        raise FlowerError("env_failed", head, text.strip()[-1500:])
    return out.done(rec, head + "\n" + text.strip())


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
    p = argparse.ArgumentParser(prog="flower", description="Durable, file-first workflows for long agent + HPC research runs.")
    p.add_argument("--version", action="version", version=f"flower {__version__}")
    p.add_argument("--json", dest="json_top", action="store_true", help="machine-readable output (any position)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output (one JSON object)")
    common.add_argument("--actor", help="who is acting (default: $FLOWER_ACTOR or human:$USER)")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    s = add("init", cmd_init, "make this directory a flower project (with the agent instructions)")
    s.add_argument("dir", nargs="?")
    s.add_argument("--skill", choices=["claude", "codex", "agents", "project", "all"],
                   help="where to install the agent skill (default: project = .claude/skills + .agents/skills)")
    s.add_argument("--no-skill", action="store_true", help="do not install the skill or the AGENTS.md block")
    s.add_argument("--hook", action="store_true",
                   help="add a Claude Code hook that reminds an agent when it runs compute beside an active run")

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
    s.add_argument("--items", action="store_true", help="list every item of large foreach steps")

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

    s = add("compare", cmd_compare, "do two runs give the same results? (a rerun, a fork, a fresh clone)")
    s.add_argument("a", help="run id, or the path of a run directory (another project)")
    s.add_argument("b")
    s.add_argument("--rtol", type=float, default=1e-6, help="relative tolerance for numbers (default 1e-6)")
    s.add_argument("--atol", type=float, default=0.0, help="absolute tolerance for numbers")
    s.add_argument("--ignore", action="append", help="an output key not to compare (repeatable)")

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
    s.add_argument("--keep-state", action="store_true",
                   help="continue in the last attempt's $FLOWER_STATE_DIR (e.g. a checkpointed job that hit its "
                        "time limit) instead of a fresh one")
    s.add_argument("--reason")
    s.add_argument("--no-continue", action="store_true")
    s.add_argument("--wait", action="store_true", help="drive in the foreground afterwards")
    s.add_argument("--follow", action="store_true",
                   help="watch this node until it finishes (its events, live output, result); exit 0 if it succeeded")
    s.add_argument("--no-edits", action="store_true", help="ignore edits to the plan file")
    s.add_argument("-y", "--yes", action="store_true", help="approve the plan-file edits it picks up (you are the approver)")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE for an input newly declared in the plan file")
    s.add_argument("--timeout", type=float)

    s = add("sync", cmd_sync, "apply the plan file's edits to a run (new steps, edited unfinished steps, a "
                              "cluster's cpus/max_jobs/min_poll) without re-running anything")
    s.add_argument("run")
    s.add_argument("--no-continue", action="store_true")
    s.add_argument("--wait", action="store_true", help="drive in the foreground afterwards")
    s.add_argument("-y", "--yes", action="store_true", help="approve the plan-file edits (you are the approver)")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE for an input newly declared in the plan file")

    s = add("start", cmd_start, "start a run for a goal now, with an empty draft plan that grows step by step")
    s.add_argument("goal", help="what the work is for, in a sentence")
    s.add_argument("--id", help="plan/run id (default: from the goal)")
    s.add_argument("--dir", help="directory for plan.yaml (default: ./<id>)")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE (declared in the plan as a required input)")
    s.add_argument("--inputs", help="JSON file with inputs (declared the same way; values stay in the run only)")
    s.add_argument("--edits", choices=["unfinished", "all", "ask"], default="unfinished",
                   help="which plan-file edits apply without asking (default: unfinished = new or unfinished steps)")
    s.add_argument("--no-ui", action="store_true", help="do not start the project's UI")

    s = add("add", cmd_add, "add a step to a run's plan file and run it: flower add RUN ID [options] -- <command>")
    s.add_argument("run")
    s.add_argument("id", help="step id")
    s.add_argument("command", nargs="*", help="the shell command, after `--`")
    s.add_argument("--title")
    s.add_argument("--needs", action="append", default=[], help="a step this one depends on (repeatable)")
    s.add_argument("--cluster", help="run it on this cluster of the plan (ssh / Slurm)")
    s.add_argument("--env", dest="environment", help="environment recipe (envs/NAME) to activate there")
    s.add_argument("--stage-in", action="append", default=[], help="file to send with it (cluster steps; repeatable)")
    s.add_argument("--retrieve", action="append", default=[], help="glob to fetch back (cluster steps; repeatable)")
    s.add_argument("--file", dest="files", action="append", default=[], help="NAME=PATH declared output file (repeatable)")
    s.add_argument("--out", dest="outs", action="append", default=[],
                   help="NAME[:TYPE] output read from the JSON the command writes to $FLOWER_OUTPUTS (repeatable)")
    s.add_argument("--setenv", action="append", default=[], help="VAR=VALUE for the command (repeatable)")
    s.add_argument("--in", dest="ins", action="append", default=[],
                   help="NAME=VALUE step input, e.g. --in results='${scan.outputs.items}' (a JSON file at $FLOWER_INPUTS)")
    s.add_argument("--timeout-total", help="e.g. 2h")
    s.add_argument("--cpus", type=int, help="cores per run of the step (resources.cpus_per_task; $FLOWER_CPUS)")
    s.add_argument("--mem", help="memory per run of the step, e.g. 16G (resources.mem; $FLOWER_MEM_MB)")
    s.add_argument("--retry", type=int, help="max attempts (retries infrastructure failures: lost, node_fail, ...)")
    s.add_argument("--on-failure", choices=["continue"], help="continue: downstream proceeds if this step fails")
    s.add_argument("--trigger", choices=["all_success", "all_done", "any_success"],
                   help="when the step may start (all_done: also after upstream failures, for partial results)")
    s.add_argument("--foreach", help="fan out: a JSON list (use ${item} / ${index} in the command) or a reference "
                                     "such as '${scan.outputs.items}'")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE for an input newly declared in the plan file")
    s.add_argument("--no-follow", action="store_true", help="do not watch it; return once it is queued")
    s.add_argument("-y", "--yes", action="store_true", help="approve the plan-file edit (you are the approver)")
    s.add_argument("--timeout", type=float, help="stop following after this many seconds")

    s = add("hook", cmd_hook, "agent-harness hooks (installed by `flower init --hook`)")
    s.add_argument("event", choices=["bash"], help="bash: remind an agent that ran compute outside an active run")

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

    s = add("skill", cmd_skill, "install the flower skill for coding agents")
    s.add_argument("target", choices=["claude", "codex", "agents", "project", "all"], nargs="?", default="project")

    add("doctor", cmd_doctor, "check harnesses, Slurm tools and the project")

    s = add("ui", cmd_ui, "web UI of this project: live DAG, node details, decisions, timeline, plan history "
                          "(runs in the background; `flower ui stop` ends it)")
    s.add_argument("action", nargs="?", choices=["start", "status", "stop"], help="default: start (or reuse)")
    s.add_argument("--port", type=int, help="local TCP port (default: a stable port derived from the project)")
    s.add_argument("--foreground", action="store_true", help="serve in this terminal instead of the background")
    s.add_argument("--host", default="127.0.0.1", help="bind address with --foreground (default 127.0.0.1)")
    s.add_argument("--token", help="fixed access token, with --foreground")
    s.add_argument("--socket", help="also serve on this owner-only Unix socket, with --foreground")
    s.add_argument("--open", action="store_true", help="open a browser")

    s = add("open", cmd_open, "on your laptop: open a remote project's UI (starts it there, tunnels over ssh, "
                              "opens the browser)")
    s.add_argument("target", help="[user@]host:/path/to/project (any ssh alias works as host)")
    s.add_argument("--port", type=int, help="local port (default: the project's own port)")
    s.add_argument("--flower", default="flower", help="the flower command on the remote machine")
    s.add_argument("--ssh-arg", action="append", help="extra ssh argument, e.g. --ssh-arg=-F --ssh-arg=~/ssh_config")
    s.add_argument("--no-browser", action="store_true")

    def plan_cluster_args(sp):
        sp.add_argument("--plan", help="the plan whose `clusters:` defines the cluster (or --run)")
        sp.add_argument("--run", help="the run whose cluster to use, with the run's inputs (instead of --plan)")
        sp.add_argument("--cluster", required=True)
        sp.add_argument("-i", "--input", action="append", help="NAME=VALUE for the plan's inputs (repeatable)")
        sp.add_argument("--inputs", help="JSON file with the plan's inputs")
        sp.add_argument("--timeout", type=float, default=7200.0, help="seconds (default 2h)")

    s = add("remote", cmd_remote, "run a command on a cluster, logged (e.g. to explore an environment)")
    rs = s.add_subparsers(dest="remote_action", required=True)
    e = rs.add_parser("exec", parents=[common], help="flower remote exec --run RUN|--plan P --cluster C [--env E] -- <command>")
    plan_cluster_args(e)
    e.add_argument("--env", help="log the command in envs/<env>/sessions/ (exploration of that environment)")
    e.add_argument("--probe", action="store_true", help="a look-only command: not drafted into setup.sh")
    e.add_argument("--installed", action="store_true",
                   help="with --env: run in the frozen recipe's installed prefix (as steps do), not the exploration one")
    e.add_argument("command", nargs="+", help="the command, after --")

    s = add("env", cmd_env, "environment recipes: new, freeze, replay, check, show")
    es = s.add_subparsers(dest="env_action", required=True)
    e = es.add_parser("new", parents=[common], help="create envs/<name>/ with template scripts")
    e.add_argument("name"); e.add_argument("--dir", help="project directory (default: here)")
    e = es.add_parser("freeze", parents=[common], help="pin the recipe (drafts setup.sh from logged commands if needed)")
    e.add_argument("name"); e.add_argument("--dir")
    e = es.add_parser("show", parents=[common], help="recipes and their state")
    e.add_argument("name", nargs="?"); e.add_argument("--dir")
    e = es.add_parser("replay", parents=[common], help="run the frozen recipe on a cluster: check, else setup + check")
    e.add_argument("name"); plan_cluster_args(e); e.add_argument("--dir")
    e.add_argument("--fresh", action="store_true", help="into a new empty prefix: proves the recipe works from scratch")
    e = es.add_parser("check", parents=[common], help="run only check.sh (with activate.sh) on a cluster")
    e.add_argument("name"); plan_cluster_args(e); e.add_argument("--dir")

    s = add("mcp", cmd_mcp, "serve flower over MCP (stdio)")
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
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = None
    if "add" in argv and "--" in argv and argv.index("add") < argv.index("--"):
        k = argv.index("--")   # `flower add RUN ID [options] -- <command>`: the command is never parsed as options
        argv, tail = argv[:k], argv[k + 1:]
    args = parser.parse_args(argv)
    if tail is not None and getattr(args, "cmd", None) == "add":
        args.command = tail
    if not getattr(args, "fn", None):
        parser.print_help()
        return 2
    if args.cmd == "plan" and not getattr(args, "plan_cmd", None):
        parser.parse_args(["plan", "--help"])
    if getattr(args, "actor", None):
        os.environ["FLOWER_ACTOR"] = args.actor
    out = Out(getattr(args, "json", False) or getattr(args, "json_top", False))
    if os.environ.get("FLOWER_INSIDE_RUN") and _is_decision(args):
        return out.error(FlowerError(
            "inside_run", f"`flower {args.cmd}` makes a decision that belongs to the user, and this process is "
            f"running inside a flower node ({os.environ.get('FLOWER_RUN_ID')}/{os.environ.get('FLOWER_NODE_ID')})",
            "finish the node's task and report in its JSON answer; propose plan changes via an `amendment`"), 2)
    try:
        return args.fn(args, out)
    except FlowerError as exc:
        return out.error(exc, 2 if exc.code not in ("journal_corrupt",) else 1)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
