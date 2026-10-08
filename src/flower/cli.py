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
import shlex
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
from .util import FlowerError, atomic_write_json, default_actor, first_line, fmt_bytes, hostname, local_clock, now_iso, parse_duration, read_json

EXIT = {"succeeded": 0, "failed": 1, "cancelled": 1, "rejected": 1, "parked": 3, "awaiting_approval": 3,
        "running": 3}


class Out:
    def __init__(self, as_json: bool):
        self.json = as_json
        self.color = (not as_json) and sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def done(self, data: Any = None, text: str | None = None, nxt: list[str] | None = None, code: int = 0) -> int:
        if self.json:
            text = (getattr(self, "prefix", "") + (text or "")) or None
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
        nxt.append(f"flower show {rid} {g.id}")
        nxt.append(f"flower approve {rid} {g.id}" + (f" <{'|'.join(g.decisions)}>" if len(g.decisions) > 2 else "")
                   + " --note '<why>'")
    if st.status == "failed":
        failed = [n for n, s in st.nodes.items() if s.status == "failed"]
        if failed:
            nxt += [f"flower show {rid} {failed[0]}", f"flower rerun {rid} {failed[0]}"]
    if st.status in ("running", "parked") and not st.open_gates():
        nxt.append(f"flower status {rid} --follow --timeout 600")
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
            "generation": st.generation, "digest": st.digest, "dir": str(eng.paths.dir),
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
            sys.stdout.write("\x1b[H\x1b[2J" + view + "\n\n(ctrl-c detaches; nodes keep running — pick up with "
                             f"`flower status {st.run_id}`)\n")
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
    gi = devloop.ensure_gitignore(root)
    if gi:
        files.append(str(gi))
        msg.append(f"added .flower/ to {gi} (runs stay local, never committed)")
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
    import shutil
    missing = [x for x in ("ssh", "rsync", "sbatch") if not shutil.which(x)]
    if missing:
        msg.append(f"not on PATH here: {', '.join(missing)} (needed only for clusters reached over ssh / Slurm)")
    from . import machines as mm
    if not mm.load():
        msg.append("machines: none yet. Add the ones work may run on, once: flower remote add NAME user@host "
                   "(flower probes the rest; this machine: flower remote add here local)")
    return out.done({"root": str(root), "files": files, "missing_tools": missing}, "\n".join(msg),
                    ['flower start "<what the work is for>"', "flower add RUN ID --description \"<what it establishes>\" -- <command>"])


def cmd_start(args, out: Out) -> int:
    """A run from minute one: an empty draft plan, parked until steps are added with `flower add` (or edits of the
    plan file picked up by `flower rerun RUN`)."""
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
    p.write_text(devloop.draft_plan_text(pid, args.goal, inputs))
    by = args.actor or default_actor()
    eng = create_run(planmod.load_plan_file(str(p)), inputs, actor=by, approve=True,
                     note="draft started with `flower start`")
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
    from . import machines as mm
    ms = mm.load()
    where = ("\nmachines (flower remote list):\n  " + "\n  ".join(mm.summary(n, e) for n, e in ms.items()
                                                                 if isinstance(e, dict))) if ms else \
        "\nno machines set up yet: flower remote add NAME user@host (this machine: flower remote add here local)"
    return out.done({"run_id": rid, "plan": str(p), "machines": list(ms)},
                    f"run {rid} started for: {args.goal}\nplan file: {p} (empty draft){where}{ui}",
                    [f"flower add {rid} <id> -- <command>", f"flower status {rid}"])


def cmd_add(args, out: Out) -> int:
    """Write a step into the run's plan file and run it (`flower rerun RUN ID` semantics): the recorded way costs
    one command, like running it by hand."""
    from . import devloop
    eng = get_engine(args)
    st = eng.state()
    src = st.meta.get("plan_source")
    if not src or not Path(src).is_file():
        raise FlowerError("no_plan_file", f"run {st.run_id} has no plan file to add to ({src})",
                          "add the step to the run's plan file, then `flower rerun RUN`")
    node = devloop.node_from_args(args, args.command)
    plan_dir = Path(src).resolve().parent
    for i, f in enumerate(node.get("stage_in") or []):   # a path typed from here means that file, wherever the plan is
        if isinstance(f, str) and "${" not in f and not os.path.isabs(f) and not (plan_dir / f).exists() \
                and Path(f).exists():
            node["stage_in"][i] = os.path.relpath(Path(f).resolve(), plan_dir)
    cur = planmod.load_plan_file(src)
    if any(isinstance(n, dict) and n.get("id") == args.id for n in cur.get("nodes") or []):
        raise FlowerError("exists", f"step {args.id!r} is already in {src}",
                          f"edit it there, then: flower rerun {st.run_id} {args.id}")
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
    ns = argparse.Namespace(run=args.run, node=args.id, only=False, cached=False, keep_state=False,
                            reason="added with `flower add`", follow=args.follow, input=args.input,
                            timeout=args.timeout, json=args.json, actor=args.actor)
    if not node.get("description"):
        ns.pre_notes = [f"warning: step {args.id} has no --description: say in a sentence or two what it establishes "
                        f"and how to read its result (edit plan.yaml, then `flower rerun {st.run_id}`)"]
        if not out.json:
            print(ns.pre_notes[0], flush=True)
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
    plan = planmod.check(planmod.load_plan_file(args.file))
    if args.plan_cmd == "show":
        return out.done({"plan": planmod.contract_view(plan), "overview": plan_overview(plan)}, plan_overview(plan))
    warn = planmod.warnings(plan)
    return out.done({"valid": True, "id": plan["id"], "nodes": len(plan["nodes"]), "digest": planmod.plan_digest(plan),
                     "warnings": warn},
                    f"✓ {args.file} is valid: {len(plan['nodes'])} nodes, digest {planmod.plan_digest(plan)[7:19]}"
                    + "".join(f"\nwarning: {w}" for w in warn),
                    [f"flower plan show {args.file}", f"flower run {args.file} --follow"])


def cmd_run(args, out: Out) -> int:
    """Create a run from a plan and start it in the background (`--follow` watches it). `--review`: wait for the
    plan's approval first (`flower approve RUN`)."""
    from .render import plan_overview
    raw = planmod.load_plan_file(args.plan)
    inputs = parse_kv(args.input)
    if args.inputs:
        inputs.update(json.loads(Path(args.inputs).read_text()))
    for role, name in parse_kv(args.machine).items():
        _use_machine(raw, role, name)
    reuse = []
    if args.reuse:
        root = find_root(create=True)
        reuse = [resolve_run(root, r).run_id for r in args.reuse]
        # re-running a run: its inputs (ssh hosts, ...) unless given again, as far as the plan still declares them
        declared = raw.get("inputs") or {}
        inputs = {**{k: v for k, v in Engine(RunPaths(root, reuse[0])).state().inputs.items() if k in declared},
                  **inputs}
    eng = create_run(raw, inputs, actor=args.actor, approve=not args.review, note=args.note, reuse_from=reuse,
                     rerun_from=args.rerun_from or [])
    st = eng.state()
    rid = st.run_id
    if args.review:
        nxt = [f"flower show {rid} plan", f"flower approve {rid} --note '<who approved and why>'",
               f"flower reject {rid} --note '<what to change>'"]
        return out.done({**summary_data(eng), "overview": plan_overview(st.plan, st.inputs)},
                        plan_overview(st.plan, st.inputs) + f"\n\nrun {rid} is waiting for plan approval.", nxt, code=3)
    return _continue(eng, args, out, f"run {rid} started")


def _use_machine(raw: dict, role: str, name: str) -> None:
    """`run --machine ROLE=NAME`: the plan's steps on cluster ROLE run on the person's machine NAME; the plan's own
    entry for ROLE goes, with the inputs only it used (a protocol's host and ssh options)."""
    from . import machines
    if machines.get(name) is None:
        raise FlowerError("usage", f"no machine {name!r}", f"machines: {', '.join(machines.load()) or 'none'} "
                          "(add one: flower remote add NAME user@host)")
    steps = [n for n in raw.get("nodes") or [] if isinstance(n, dict) and n.get("cluster") == role]
    if not steps:
        raise FlowerError("usage", f"no step of the plan runs on {role!r}",
                          "clusters the steps use: " + ", ".join(sorted({str(n.get("cluster")) for n in raw.get("nodes")
                                                                         or [] if isinstance(n, dict) and n.get("cluster")})))
    for n in steps:
        n["cluster"] = name
    gone = (raw.get("clusters") or {}).pop(role, None)
    rest = json.dumps({k: v for k, v in raw.items() if k != "inputs"}, default=str)
    for k in list(raw.get("inputs") or {}):
        if f"inputs.{k}" in json.dumps(gone, default=str) and f"inputs.{k}" not in rest:
            raw["inputs"].pop(k)


def _continue(eng: Engine, args, out: Out, text: str) -> int:
    """The one waiting rule: return at once with a background driver, or (`--follow`) watch the run here."""
    if getattr(args, "follow", False):
        return _follow_run(eng, out, getattr(args, "timeout", None))
    rid = eng.paths.run_id
    eng.tick()
    st = eng.state()
    if st.status not in TERMINAL_RUN:
        info = spawn_driver(eng)
        text += f"; it continues in the background (driver pid {info.get('pid')})."
    st = eng.state()
    return out.done(summary_data(eng, st), text, next_for(st, rid), code=0)


def cmd_status(args, out: Out) -> int:
    """Where a run is and what needs you (one scheduling pass; a background driver is started if work is under
    way and nobody drives it). Without RUN: the project's runs. ``--follow``: until the run finishes or needs a
    decision, driving it in the foreground if no driver does."""
    from .render import status_view
    if not args.run:
        return _list_runs(out)
    eng = get_engine(args)
    if args.follow:
        return _follow_run(eng, out, args.timeout, args.no_tick)
    st = eng.state()
    note = ""
    approved = st.status != "awaiting_approval" or (st.gates.get("plan") and st.gates["plan"].status == "answered")
    if not args.no_tick and st.status not in TERMINAL_RUN and approved and not driver_alive(eng.paths):
        eng.tick()
        st = eng.state()
        if st.status == "running":   # work under way and nobody driving it: start a driver (and say so)
            try:
                spawn_driver(eng)
                note = "\n(no background driver was running; started one)"
            except FlowerError as exc:
                note = f"\nwarning: no background driver, and starting one failed: {exc.message}"
    elif args.no_tick and st.status == "running" and not driver_alive(eng.paths):
        note = f"\nwarning: running, but no background driver is alive: flower status {st.run_id}"
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color, all_items=getattr(args, "items", False)) + note,
                    next_for(st, st.run_id) if out.json else None,
                    code=run_status_code(st.status))


def _follow_run(eng: Engine, out: Out, timeout: float | None, observe_only: bool = False) -> int:
    from .render import status_view
    start = time.time()
    try:
        if not driver_alive(eng.paths) and not observe_only and eng.state().status not in TERMINAL_RUN:
            live_drive(eng, out, timeout=timeout)
        else:
            while True:
                st = eng.state()
                if st.status in TERMINAL_RUN or st.open_gates() or st.status == "awaiting_approval":
                    break
                if out.color:
                    sys.stdout.write("\x1b[H\x1b[2J" + status_view(st, eng.paths, color=True) + "\n")
                    sys.stdout.flush()
                if timeout and time.time() - start > timeout:
                    break
                time.sleep(2)
    except KeyboardInterrupt:
        pass
    st = eng.state()
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color), next_for(st, st.run_id),
                    code=run_status_code(st.status))


def _list_runs(out: Out, limit: int = 30) -> int:
    from .util import fmt_duration, seconds_since
    root = find_root()
    rows = []
    for rid in list_runs(root)[-limit:]:
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
    """A run, a step (attempts, outputs, errors; --logs: its raw output), one output value (KEY: an output key, a
    dotted path into it, or a declared file's name, which prints its path) or a gate (its id instead of STEP)."""
    from .render import gate_detail, node_detail, status_view
    eng = get_engine(args)
    st = eng.state()
    gate = args.gate or (args.node if args.node and args.node not in st.nodes and
                         (args.node in st.gates or any(g.startswith(args.node) for g in st.gates)) else None)
    if args.logs:
        return _logs(eng, st, args, out)
    if gate:
        gid = gate
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
        r = ns.result
        if args.key:
            if r is None:
                raise FlowerError("no_result", f"node {args.node} has no successful result")
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
        data = {"id": args.node, "status": ns.status, "spec": st.node_spec(args.node),
                "outputs": r.outputs if r else None, "files": r.files if r else None,
                "attempts": [a.__dict__ for a in ns.attempts]}
        return out.done(data, node_detail(st, eng.paths, args.node, color=out.color))
    return out.done(summary_data(eng, st), status_view(st, eng.paths, color=out.color))


def cmd_log(args, out: Out) -> int:
    from .render import timeline
    eng = get_engine(args)
    if args.note:
        eng.note(args.note, node=args.node, by=args.actor or default_actor())
        return out.done({"ok": True}, "noted")
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


def _logs(eng: Engine, st, args, out: Out) -> int:
    """`flower show RUN STEP --logs [--attempt N]`: a step's raw output (stdout/stderr, a job's logs)."""
    ns = st.nodes.get(args.node)
    if not ns or not ns.attempts:
        raise FlowerError("no_attempt", f"node {args.node} has not run yet")
    a = ns.attempts[-1] if not args.attempt else next((x for x in ns.attempts if x.n == args.attempt), None)
    if a is None:
        raise FlowerError("no_attempt", f"node {args.node} has no attempt {args.attempt}",
                             f"attempts: {', '.join(str(x.n) for x in ns.attempts)}")
    adir = eng.paths.attempt_dir(args.node, a.n)
    spec = st.node_spec(args.node)
    if planmod.on_cluster(spec):
        from .hpc import log_files
        jd = Path(a.job.get("job_dir") or "")
        local = jd if jd.exists() else adir / "job"
        jid = a.job.get("job_id")
        parts = []
        cl = (st.plan.get("clusters") or {}).get(spec.get("cluster")) or {}
        if a.status == "running" and jid and not jd.exists() and a.job.get("job_dir"):   # live, from the machine
            from .hpc.transport import make_transport
            names = " ".join(shlex.quote(n) for n in log_files(cl, jid))
            r = make_transport(cl).run(f"cd {shlex.quote(a.job['job_dir'])} && for f in {names}; do "
                                       "[ -s \"$f\" ] && { echo \"==> $f (live, on the machine) <==\"; tail -c 20000 \"$f\"; }; done; true",
                                       timeout=60)
            text = (r.out or "").strip() or f"(no output yet; {(r.err or '').strip()[-200:]})"
            return out.done({"text": text, "dir": a.job["job_dir"], "live": True}, text)
        for name in (log_files(cl, jid) if jid else []):
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
    """A failed attempt's outputs file: what the step wrote before failing, otherwise invisible (`flower show`
    shows successful results only)."""
    if attempt.status != "succeeded" and path.is_file() and path.stat().st_size:
        parts.append(f"==> {path.name} (written by this attempt, which did not succeed) <==\n"
                     + path.read_text(errors="replace")[-20000:])


def cmd_approve(args, out: Out) -> int:
    """approve RUN [GATE] [DECISION]: answer the plan or a gate (its first decision by default; a gate with more
    than approve/reject takes the one named). reject: its rejecting decision. --note: why (a rework step reads a
    rejection's note as ${feedback})."""
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
    if getattr(args, "decision", None):
        decision = args.decision
    elif args.reject:
        decision = "reject" if "reject" in g.decisions else g.decisions[-1]
    else:
        decision = g.decisions[0]
    eng.answer(g.id, decision, text=args.note, by=args.actor or default_actor())
    return _continue(eng, args, out, f"{decision}: {g.id}")


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


def _pick_up_edits(eng: Engine, args, node: str | None) -> list:
    """The development loop: edits of the plan file apply at once, recorded as a change of the plan (who, the
    diff). A finished step whose definition changed runs again with its downstream. ``node`` None: the whole file."""
    by = args.actor or default_actor()
    notes: list = []
    ed = eng.plan_edits(node, new_inputs=parse_kv(getattr(args, "input", None)))
    notes.extend(f"note: {x}" for x in ed.get("ignored") or [])
    if getattr(args, "only", False):   # `rerun --only`: a superseded step does not take its downstream with it (#62)
        for op in ed["ops"]:
            if op.get("op") == "replace" and op.get("supersede"):
                op["only"] = True
    if not ed["ops"]:
        return notes
    what = ", ".join([f"changed {', '.join(ed['changed'])}"] * bool(ed["changed"])
                     + [f"added {', '.join(ed['added'])}"] * bool(ed["added"])
                     + [f"new input {', '.join(ed['new_inputs'])}"] * bool(ed.get("new_inputs"))
                     + [f"new cluster {', '.join(ed['new_clusters'])}"] * bool(ed.get("new_clusters"))
                     + [f"cluster {', '.join(ed['tuned_clusters'])}"] * bool(ed.get("tuned_clusters")))
    descs = [n.get("description") for op in ed["ops"] if op.get("op") == "add" for n in op.get("nodes") or []
             if isinstance(n, dict) and n.get("description") and not n.get("generated")]
    if len(descs) == 1:
        what += f": {first_line(descs[0], 160)}"
    eng.apply_edit(ed["ops"], f"plan file edited ({what})", by=by)
    notes.append(f"plan file applied (generation {eng.state().generation}): {what}")
    return notes


def cmd_rerun(args, out: Out) -> int:
    """rerun RUN [STEP]: apply the plan file's edits (a changed finished step runs again with its downstream), and
    run STEP again with everything downstream of it (--only: STEP alone)."""
    eng = get_engine(args)
    by = args.actor or default_actor()
    notes = list(getattr(args, "pre_notes", None) or [])
    notes += _pick_up_edits(eng, args, args.node)
    st = eng.state()
    if args.node is None:
        if not any(n.startswith("plan file applied") for n in notes):
            notes.append("the run already follows its plan file")
        return _continue(eng, args, out, "\n".join(notes))
    ns = st.nodes.get(args.node)
    if ns is not None and ns.status in ("running", "waiting", "retrying"):
        notes.append(f"{args.node} is already {ns.status}")
    elif ns is not None:
        try:
            targets = eng.rerun(args.node, downstream=not args.only, force=not args.cached, by=by, reason=args.reason,
                                keep_state=getattr(args, "keep_state", False))
            again = [t for t in targets if st.nodes.get(t) and st.nodes[t].attempts]
            new = [t for t in targets if t not in again]   # a step that never ran is queued, not "again"
            notes.append("; ".join([f"queued again: {', '.join(again)}"] * bool(again) + [f"queued: {', '.join(new)}"] * bool(new)))
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
        elif notes:
            out.prefix = "\n".join(notes) + "\n"   # the notes reach --json callers with the follow's result
        return _follow(eng, args.node, out, timeout=args.timeout)
    return _continue(eng, args, out, "\n".join(notes))


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
                    [f"flower show {eng.paths.run_id} {node} --logs"] if not ok else None, code=0 if ok else 1)


def cmd_export(args, out: Out) -> int:
    """A finished run as a reproducibility protocol (protocol.yaml, expected.json, PROTOCOL.md)."""
    from .protocol import export
    eng = get_engine(args)
    res = export(eng, args.steps)
    return out.done(res, f"protocol of {', '.join(args.steps)} ({len(res['steps'])} steps, {res['expected']} expected "
                         f"results) in {res['dir']}:\n  " + "\n  ".join(res["files"]),
                    ["flower run protocol.yaml --follow", "flower compare RUN expected.json"])


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
from .protocol import SKIP as COMPARE_SKIP   # paths, ids and timings differ between runs by nature


def cmd_compare(args, out: Out) -> int:
    """Do two runs of a workflow (a rerun, a fork, a fresh clone on another machine) give the same results?
    Compares the outputs of every step both have, numbers within --rtol, everything else exactly."""
    sides = [_outputs_of(x) for x in (args.a, args.b)]
    (ida, oa_all), (idb, ob_all) = sides
    rows, same, missing = [], 0, []
    expected = [x for x in (args.a, args.b) if str(x).endswith(".json")]
    # against an expected.json: exactly the steps it lists; two runs: every step either has
    names = sorted(ob_all if args.b in expected else oa_all) if expected else sorted(set(oa_all) | set(ob_all))
    for nid in names:
        if oa_all.get(nid) is None or ob_all.get(nid) is None:
            missing.append(nid)
            continue
        oa = {k: v for k, v in oa_all[nid].items() if k not in COMPARE_SKIP and k not in set(args.ignore or [])}
        ob = {k: v for k, v in ob_all[nid].items() if k not in COMPARE_SKIP and k not in set(args.ignore or [])}
        diffs: list = []
        _diff_values(oa, ob, args.rtol, args.atol, "", diffs)
        if diffs:
            rows.append({"node": nid, "diffs": diffs})
        else:
            same += 1
    lines = [f"{ida}  vs  {idb}: {same} step(s) agree within rtol {args.rtol:g}, "
             f"{len(rows)} differ, {len(missing)} not succeeded in both"]
    for r in rows:
        for d in r["diffs"][:6]:
            rel = f"  (rel {d['rel']:.2e})" if d["rel"] is not None else ""
            lines.append(f"  {r['node']}{d['key']}: {json.dumps(d['a'], default=str)[:40]}  vs  "
                         f"{json.dumps(d['b'], default=str)[:40]}{rel}")
    if missing:
        lines.append(f"  not compared: {', '.join(missing[:12])}" + (" ..." if len(missing) > 12 else ""))
    return out.done({"same": same, "differ": rows, "not_compared": missing}, "\n".join(lines),
                    code=0 if not rows and not (expected and missing) else 1)


def _outputs_of(ref: str) -> tuple[str, dict]:
    """(a name, {step: outputs}) of a run (id or run directory) or of an expected.json from `flower export`.
    A foreach step repeats its items' outputs, so it is left out: its items are compared themselves."""
    if str(ref).endswith(".json") and Path(ref).is_file():
        from .protocol import load_expected
        return str(ref), load_expected(Path(ref))["steps"]
    st = _engine_at(ref).state()
    g = st.graph().nodes
    return st.run_id, {n: (ns.result.outputs or {}) for n, ns in st.nodes.items()
                       if ns.status == "succeeded" and ns.result and (g.get(n) or {}).get("foreach") is None}


def _code_stamp() -> tuple:
    """Newest modification time and file count of flower's own source (cheap: a few dozen stats)."""
    pkg = Path(__file__).resolve().parent
    files = [p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts]
    return (max((p.stat().st_mtime_ns for p in files), default=0), len(files))


def _new_code_broken() -> str | None:
    """Why flower's code on disk would not run a driver (None if it would): imported in a fresh interpreter."""
    probe = "import flower.cli, flower.engine; flower.engine._executors()"
    try:
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "importing it timed out"
    return None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["exit code %d" % r.returncode])[-1]


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
                # Nothing is lost: the state is in the journal and jobs are detached processes. Code that does
                # not import (an edit or a pull half done) is not switched to: the driver keeps what it runs (#59)
                code = _code_stamp()
                broken = _new_code_broken()
                if broken:
                    eng.emit("driver.error", {"pid": os.getpid(), "consecutive": 0,
                                              "error": f"flower's changed code does not import, keeping the running "
                                                       f"code until the next change: {broken}"[:300]})
                else:
                    eng.emit("driver.reloaded", {"pid": os.getpid(), "reason": "flower's code changed"})
                    os.execv(sys.executable, [sys.executable, "-m", "flower", "_driver", args.run, "--root", str(root)])
            if rep.status in TERMINAL_RUN:
                break
            if rep.status in ("parked", "awaiting_approval") and not rep.next_poll_s:
                # parked on a human/external event: keep watching cheaply so answers or signals dropped as
                # files are picked up without anyone running a command; give up after a long idle period
                before = eng.state().last_seq
                time.sleep(15)
                pending = list(eng.paths.pending.glob("*.answer.json"))
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
    action = args.action or "start"
    if ":" in action:   # [user@]host:/path: a project on another machine, opened from this one
        args.target = action
        return _open_remote(args, out)
    if action not in ("start", "status", "stop"):
        raise FlowerError("usage", f"unknown ui action {action!r}", "start, status, stop, or [user@]host:/path")
    root = find_root()
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


def _open_remote(args, out: Out) -> int:
    """`flower ui [user@]host:/path` on your laptop: start (or reuse) that project's UI, tunnel to it, open the
    browser."""
    import shlex
    host, sep, path = args.target.partition(":")
    if not sep or not host or not path:
        raise FlowerError("usage", f"expected [user@]host:/path/to/project, got {args.target!r}",
                          "e.g. flower ui me@cluster.example.org:~/projects/si-study")
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
    from . import machines
    if getattr(args, "run", None) and args.plan:
        raise FlowerError("usage", "give --run RUN (a run's clusters) or --plan PLAN (a plan file's), not both")
    if not getattr(args, "run", None) and not args.plan:   # a machine of the person's machines file
        spec = machines.get(args.cluster) or ({"transport": "local", "scheduler": "none"}
                                              if args.cluster == planmod.LOCAL_CLUSTER else None)
        if spec is None:
            raise FlowerError("usage", f"no machine {args.cluster!r} (a cluster of a run or a plan: give --run RUN or "
                              "--plan PLAN)", f"machines: {', '.join(machines.load()) or 'none'} "
                              "(add one: flower remote add NAME user@host)")
        return {**spec, "_name": args.cluster}, Path.cwd()
    if args.run:
        eng = get_engine(args)
        st = eng.state()
        clusters = {planmod.LOCAL_CLUSTER: {"transport": "local", "scheduler": "none"}, **(st.plan.get("clusters") or {})}
        if args.cluster not in clusters and machines.get(args.cluster):
            clusters[args.cluster] = machines.get(args.cluster)
        if args.cluster not in clusters:
            raise FlowerError("usage", f"run {st.run_id} has no cluster {args.cluster!r}",
                              f"clusters: {', '.join(clusters) or 'none'} (a new one: add it to the plan file, "
                              f"then `flower rerun {st.run_id}`)")
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
    """Machines: add (probe one), list, check (probe again), login (a shared ssh connection); exec (one command)."""
    if args.remote_action != "exec":
        return _machines(args, out)
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
        pfx = envmod.prefix(args.env, fz["hash"], c.get("env_dir"))
        pre += (f'export FLOWER_ENV_PREFIX="{pfx}"; export FLOWER_ENV_DIR="$FLOWER_ENV_PREFIX.recipe"\n'
                'if [ ! -d "$FLOWER_ENV_PREFIX" ]; then echo "not installed on this cluster: $FLOWER_ENV_PREFIX '
                '(a step using it, or flower env replay, installs it)" >&2; exit 3; fi\n'
                'set +u; source "$FLOWER_ENV_DIR/activate.sh"\n')
    elif args.env:  # explore with what a real setup gets: a prefix to install into, and the activation so far
        d = envmod.find(args.env, src) or (src / envmod.ENVS_DIR / args.env)
        pre += (f'export FLOWER_ENV_PREFIX="{envmod.env_root(c.get("env_dir"))}/{args.env}-explore"\n'
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
    entry = {"at": now_iso(), "cluster": c["_name"], "cmd": text, "rc": r.rc, "env": args.env,
             "seconds": round(time.time() - t0, 2), "by": args.actor or default_actor()}
    _note_in_run(args, f"remote exec on {c['_name']}" + (f" (env {args.env})" if args.env else "")
                 + f": {first_line(text, 200)} -> exit {r.rc}")
    if out.json:
        return out.done({**entry, "out": r.out, "err": r.err}, code=0 if r.rc == 0 else 1)
    sys.stdout.write(r.out or "")
    sys.stderr.write(r.err or "")
    return r.rc if r.rc < 256 else 1


def _machines(args, out: Out) -> int:
    from . import machines as mm
    data = mm.load()
    act = args.remote_action
    if act == "add":
        if isinstance(data.get(args.name), dict) and data[args.name].get("probed"):   # one never reached: replaced
            raise FlowerError("exists", f"machine {args.name!r} is already in {mm.path()}",
                              f"refresh it: flower remote check {args.name} (or edit the file)")
        entry: dict = {"ssh": args.target}
        if args.identity_file:
            entry["identity_file"] = args.identity_file
        if args.note:
            entry["note"] = args.note
        if args.cores:
            entry["agent_may_use"] = {"cores": args.cores}
        if args.login:
            _open_master(args.name, entry)
        known = entry["ssh"] == "local" or mm.host_key(entry) is not None
        entry["probed"] = mm.probe(args.name, entry, first=True)   # only a machine flower reached is saved
        data[args.name] = entry
        p = mm.save(data)
        fp = None if known else mm.host_key(entry)
        return out.done({"name": args.name, "file": str(p), "machine": mm.effective(args.name, entry), "host_key": fp},
                        f"added to {p}:\n  {mm.summary(args.name, entry)}\n"
                        + (f"first connection: accepted its host key {fp} (compare with what the centre publishes)\n"
                           if fp else "")
                        + "edit the file to change anything flower found; set agent_may_use and a note for agents", [f"flower remote exec --cluster {args.name} -- uptime"])
    if act == "login":
        entry = data.get(args.name)
        if not isinstance(entry, dict) or entry.get("ssh") == "local":
            raise FlowerError("usage", f"no ssh machine {args.name!r}",
                              "a new machine that needs a password or a code: flower remote add NAME TARGET --login")
        _open_master(args.name, entry)
        return out.done({"name": args.name}, f"connected to {args.name}; flower reuses this connection until it "
                        f"drops (the network, a reboot) or `flower remote remove {args.name}`")
    if act == "shell":
        return _shell(args, data)
    if act == "clean":
        return _clean(args, out, data)
    if act == "remove":
        entry = data.pop(args.name, None)
        if not isinstance(entry, dict):
            raise FlowerError("usage", f"no machine {args.name!r}", "flower remote list")
        if entry.get("ssh") != "local":   # close its shared connection, if one is open
            import subprocess
            host, opts = mm.ssh_target(entry)
            subprocess.run(["ssh", *opts, "-O", "exit", host], capture_output=True, timeout=10)
        mm.save(data)
        return out.done({"removed": args.name}, f"removed {args.name} from {mm.path()}")
    names = [args.name] if getattr(args, "name", None) else list(data)
    missing = [n for n in names if not isinstance(data.get(n), dict)]
    if missing:
        raise FlowerError("usage", f"no machine {missing[0]!r} in {mm.path()}", "flower remote add NAME TARGET")
    if act == "check":
        rows, bad = [], []
        for n in names:
            try:
                data[n]["probed"] = mm.probe(n, data[n])
                rows.append(mm.summary(n, data[n]))
            except FlowerError as exc:
                bad.append(n)
                rows.append(f"{n:<14} UNREACHABLE: {exc.message} ({exc.suggestion})")
        mm.save(data)
        return out.done({"machines": {n: mm.effective(n, data[n]) for n in names}, "unreachable": bad},
                        "\n".join(rows) or "no machines yet: flower remote add NAME user@host", code=1 if bad else 0)
    text = "\n\n".join(mm.details(n, data[n]) for n in names) or \
        "no machines yet: flower remote add NAME user@host   (or local: flower remote add here local)"
    return out.done({"file": str(mm.path()), "machines": {n: mm.effective(n, data[n]) for n in names}}, text)


def _shell(args, data: dict) -> int:
    """`remote shell NAME`, or `remote shell --run RUN STEP`: an interactive shell there, in the step's folder."""
    import subprocess
    from . import machines as mm
    spec, where = None, None
    if args.run:
        args.step = args.step or args.name   # `remote shell --run RUN STEP`
        eng = get_engine(args)
        st = eng.state()
        ns = st.nodes.get(args.step or "")
        if ns is None or not ns.last or not (ns.last.job or {}).get("job_dir"):
            raise FlowerError("usage", f"step {args.step!r} of {st.run_id} has no job folder on a machine",
                              "steps that ran on a machine: " + ", ".join(n for n, s in st.nodes.items()
                                                                        if s.last and (s.last.job or {}).get("job_dir")))
        spec = (st.plan.get("clusters") or {}).get(st.node_spec(args.step).get("cluster")) or {}
        where = ns.last.job["job_dir"]
        eng.note(f"shell opened in the folder of {args.step} on {spec.get('machine') or st.node_spec(args.step).get('cluster')}",
                 node=args.step, by=args.actor or default_actor())
    elif args.name:
        if args.name not in data:
            raise FlowerError("usage", f"no machine {args.name!r}", "flower remote list")
        spec = mm.cluster_spec(args.name, data[args.name])
    else:
        raise FlowerError("usage", "say where: flower remote shell NAME, or flower remote shell --run RUN STEP")
    go = f"cd {shlex.quote(where)} && exec \"${{SHELL:-bash}}\" -l" if where else None
    if spec.get("transport", "local") != "ssh":
        return subprocess.call(["bash", "-c", go] if go else ["bash", "-l"])
    cmd = ["ssh", "-t", *(spec.get("ssh_options") or []), spec["host"]] + ([go] if go else [])
    return subprocess.call(cmd)


def _clean(args, out: Out, data: dict) -> int:
    """`remote clean NAME [-y]`: the job folders of this project's finished runs on that machine (what was fetched
    stays in the record; nothing else on the machine is touched)."""
    from . import machines as mm
    from .hpc.transport import make_transport
    if args.name not in data:
        raise FlowerError("usage", f"no machine {args.name!r}", "flower remote list")
    spec = mm.cluster_spec(args.name, data[args.name])
    root = find_root()
    dirs, runs = [], []
    for rid in list_runs(root):
        try:
            st = Engine(RunPaths(root, rid)).state()
        except FlowerError:
            continue
        if st.status not in TERMINAL_RUN:
            continue
        for c in (st.plan.get("clusters") or {}).values():
            same = c.get("machine") == args.name or (c.get("host") and c.get("host") == spec.get("host")) or \
                (c.get("transport", "local") == "local" and spec.get("transport") == "local" and c.get("remote_root"))
            if same:
                base = str(c.get("remote_root") or "~/flower-runs").rstrip("/")
                d = ("$HOME" + base[1:] if base.startswith("~") else base) + "/" + rid
                if d not in dirs:
                    dirs.append(d)
                    runs.append(rid)
    if not dirs:
        return out.done({"dirs": []}, f"nothing to clean on {args.name}: no finished run of this project used it")
    tr = make_transport(spec)
    r = tr.run("for d in " + " ".join(f'"{d}"' for d in dirs) + "; do [ -d \"$d\" ] && du -sk \"$d\"; done; true", timeout=600)
    sizes = {ln.split("\t", 1)[1].strip(): int(ln.split("\t", 1)[0]) for ln in r.out.splitlines() if "\t" in ln}
    found = [(rid, d) for rid, d in zip(runs, dirs) if any(k.endswith("/" + rid) for k in sizes)]
    total = sum(sizes.values())
    lines = [f"  {rid}  {fmt_bytes(1024 * sizes[next(k for k in sizes if k.endswith('/' + rid))])}" for rid, _ in found]
    if not found:
        return out.done({"dirs": []}, f"nothing to clean on {args.name}: those runs' folders are gone already")
    if not args.yes:
        return out.done({"would_remove": [d for _, d in found], "kb": total},
                        f"the job folders of {len(found)} finished run(s) on {args.name}, {fmt_bytes(1024 * total)}:\n"
                        + "\n".join(lines) + "\nremove them: flower remote clean " + args.name + " -y "
                        "(what each run fetched stays in its record here)")
    r = tr.run("rm -rf " + " ".join(f'"{d}"' for _, d in found), timeout=1800)
    if r.rc != 0:
        raise FlowerError("remote", f"could not remove them: {(r.err or '').strip()[-300:]}")
    for rid, d in found:
        Engine(RunPaths(root, rid)).note(f"job folders on {args.name} removed ({d}); what was fetched stays here",
                                         by=args.actor or default_actor())
    return out.done({"removed": [d for _, d in found], "kb": total},
                    f"removed {fmt_bytes(1024 * total)} on {args.name}:\n" + "\n".join(lines))


def _open_master(name: str, entry: dict) -> str:
    """One shared ssh connection, opened here (the person types a password or a code once); flower reuses it with no
    time limit, until it drops or the machine is removed."""
    import subprocess
    from . import machines as mm
    host, opts = mm.ssh_target(entry)
    Path(mm.control_path()).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    rc = subprocess.call(["ssh", *opts, "-o", "ControlMaster=yes", "-o", "ControlPersist=yes", "-fN", host])
    if rc != 0:
        raise FlowerError("login", f"ssh to {name} failed (exit {rc}); nothing was saved")
    return host


def _note_in_run(args, text: str) -> None:
    """Work on a machine done with `--run RUN` is part of that run's record."""
    if getattr(args, "run", None):
        Engine(resolve_run(find_root(), args.run)).note(text, by=args.actor or default_actor())


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
               f"froze {d.name} as {envmod.short(fz['hash'])}")
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
                         "frozen_at": (fz or {}).get("frozen_at")})
        text = "\n".join(f"{r['name']:<16} {r['state']:<8} {envmod.short(r['hash'] or '') or '-':<13} "
                         f"{(r['frozen_at'] or '')[:10]:<11} {r['dir']}" for r in rows) or "no environment recipes here"
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
    root = envmod.env_root(c.get("env_dir")).replace("$HOME", home)
    staging = f"{root}/.staging/{args.name}-{envmod.short(h)}"
    r = tr.put_tree(d, staging)
    if r.rc != 0:
        raise FlowerError("remote", f"could not upload the recipe: {(r.err or '').strip()[-300:]}")
    fresh = act == "replay" and args.fresh
    pfx = f"{root}/{args.name}-{envmod.short(h)}" + (f"-replay-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}" if fresh else "")
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
    _note_in_run(args, f"env {act} {args.name} ({envmod.short(h)}) on {c['_name']}: "
                 + ("OK" if ok else f"FAILED (exit {r.rc})") + f", prefix {pfx}")
    text = (r.out or "") + (r.err or "")
    head = (f"{act} {args.name} ({envmod.short(h)}) on {c['_name']}: " + ("OK" if ok else f"FAILED (exit {r.rc})")
            + f" in {rec['seconds']}s, prefix {pfx}")
    if not ok:
        raise FlowerError("env_failed", head, text.strip()[-1500:])
    return out.done(rec, head + "\n" + text.strip())


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

    s = add("plan", cmd_plan, "validate or show a plan file")
    ps = s.add_subparsers(dest="plan_cmd", metavar="SUBCOMMAND")
    for name, h in (("validate", "check a plan and list every problem"), ("show", "human overview of a plan")):
        x = ps.add_parser(name, parents=[common], help=h)
        x.add_argument("file")

    s = add("run", cmd_run, "create a run from a plan and start it (in the background; --follow watches it)")
    s.add_argument("plan")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE (repeatable)")
    s.add_argument("--inputs", help="JSON file with inputs")
    s.add_argument("--review", action="store_true", help="wait for the plan's approval first (`flower approve RUN`)")
    s.add_argument("--machine", action="append", metavar="ROLE=NAME",
                   help="run the steps on cluster ROLE (e.g. a protocol's `remote`) on your machine NAME (repeatable)")
    s.add_argument("--note", help="recorded with the run")
    s.add_argument("--follow", action="store_true", help="watch it until it finishes or needs a decision")
    s.add_argument("--timeout", type=float, help="with --follow: seconds")
    s.add_argument("-y", "--yes", action="store_true", help=argparse.SUPPRESS)   # what `run` does now; old docs say -y
    s.add_argument("--reuse", action="append", metavar="RUN",
                   help="reuse recorded results of RUN where a node's definition and inputs are unchanged")
    s.add_argument("--rerun-from", action="append", metavar="NODE", help="with --reuse: re-execute NODE and its downstream")

    s = add("status", cmd_status, "where a run is and what needs you (without RUN: the project's runs)")
    s.add_argument("run", nargs="?")
    s.add_argument("--follow", action="store_true",
                   help="until the run finishes or needs a decision (drives it here if no background driver does)")
    s.add_argument("--timeout", type=float, help="with --follow: seconds; exit 3 if still running")
    s.add_argument("--no-tick", action="store_true", help="pure read; do not advance the run")
    s.add_argument("--items", action="store_true", help="list every item of large foreach steps")

    s = add("show", cmd_show, "a run, a step (attempts, outputs, errors; --logs: its raw output), one output, or a gate")
    s.add_argument("run", nargs="?")
    s.add_argument("node", nargs="?", metavar="STEP|GATE")
    s.add_argument("key", nargs="?", help="one output (dotted path) or a declared file's name: print only that")
    s.add_argument("--logs", action="store_true", help="the step's raw output (stdout/stderr, a job's logs)")
    s.add_argument("--attempt", type=int, help="with --logs: which attempt (default: the last)")
    s.add_argument("--gate", help=argparse.SUPPRESS)

    s = add("log", cmd_log, "human timeline of everything that happened")
    s.add_argument("run", nargs="?")
    s.add_argument("--node")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--note", metavar="TEXT", help="add a note to the run's record (with --node: about that step)")

    s = add("compare", cmd_compare, "do two runs give the same results? (a rerun, a fresh clone, or a protocol's expected.json)")
    s.add_argument("a", help="run id, the path of a run directory (another project), or an expected.json")
    s.add_argument("b")
    s.add_argument("--rtol", type=float, default=1e-6, help="relative tolerance for numbers (default 1e-6)")
    s.add_argument("--atol", type=float, default=0.0, help="absolute tolerance for numbers")
    s.add_argument("--ignore", action="append", help="an output key not to compare (repeatable)")

    for name, help_ in (("approve", "answer the plan or a gate: its first decision, or the DECISION named"),
                        ("reject", "reject the plan or a gate (a gate with on_reject sends work back with your note)")):
        s = add(name, cmd_approve, help_)
        s.add_argument("run", nargs="?")
        s.add_argument("gate", nargs="?")
        if name == "approve":
            s.add_argument("decision", nargs="?", help="for a gate with its own decisions: which one")
        s.add_argument("-m", "--note", "--text", dest="note", help="why; for a rejection, what to change")
        s.add_argument("--follow", action="store_true", help="then watch the run until it finishes or needs a decision")
        s.add_argument("--timeout", type=float, help="with --follow: seconds")
        s.set_defaults(reject=name == "reject")

    s = add("cancel", cmd_cancel, "cancel a run (or one node)")
    s.add_argument("run", nargs="?")
    s.add_argument("--node")
    s.add_argument("--reason")

    s = add("rerun", cmd_rerun, "apply the plan file's edits; with STEP, run it again (and everything downstream)")
    s.add_argument("run")
    s.add_argument("node", nargs="?", metavar="STEP")
    s.add_argument("--only", action="store_true", help="do not re-run downstream steps")
    s.add_argument("--cached", action="store_true", help="allow reuse if definition+inputs are unchanged")
    s.add_argument("--keep-state", action="store_true",
                   help="continue in the last attempt's $FLOWER_STATE_DIR (e.g. a checkpointed job that hit its "
                        "time limit) instead of a fresh one")
    s.add_argument("--reason")
    s.add_argument("--follow", action="store_true",
                   help="watch STEP (without STEP: the run) until it finishes; exit 0 if it succeeded")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE for an input newly declared in the plan file")
    s.add_argument("--timeout", type=float, help="with --follow: seconds")

    s = add("start", cmd_start, "start a run for a goal now, with an empty draft plan that grows step by step")
    s.add_argument("goal", help="what the work is for, in a sentence")
    s.add_argument("--id", help="plan/run id (default: from the goal)")
    s.add_argument("--dir", help="directory for plan.yaml (default: ./<id>)")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE (declared in the plan as a required input)")
    s.add_argument("--inputs", help="JSON file with inputs (declared the same way; values stay in the run only)")
    s.add_argument("--no-ui", action="store_true", help="do not start the project's UI")

    s = add("add", cmd_add, "add a step to a run's plan file and run it: flower add RUN ID [options] -- <command>")
    s.add_argument("run")
    s.add_argument("id", help="step id")
    s.add_argument("command", nargs="*", help="the shell command, after `--`")
    s.add_argument("--description", help="what the step establishes and how to read its result (1-2 sentences; "
                                         "shown in the UI and in `flower show`)")
    s.add_argument("--needs", action="append", default=[], help="a step this one depends on (repeatable)")
    s.add_argument("--cluster", help="run it on this cluster of the plan (ssh / Slurm)")
    s.add_argument("--env", dest="environment", help="environment recipe (envs/NAME) to activate there")
    s.add_argument("--stage-in", action="append", default=[], help="file to send with it (cluster steps; repeatable)")
    s.add_argument("--out", dest="outs", action="append", default=[],
                   help="NAME[:TYPE] output, from the JSON object the command writes to $FLOWER_OUTPUTS or prints "
                        "last (repeatable)")
    s.add_argument("--file", dest="files", action="append", default=[], help="NAME=PATH declared output file (repeatable)")
    s.add_argument("--in", dest="ins", action="append", default=[],
                   help="NAME=VALUE step input, e.g. --in results='${scan.outputs.items}' (a JSON file at $FLOWER_INPUTS)")
    s.add_argument("--foreach", help="fan out: a JSON list (use ${item} / ${index} in the command) or a reference "
                                     "such as '${scan.outputs.items}'")
    s.add_argument("--gate", metavar="MESSAGE", help="a person's decision instead of a command (kind: gate)")
    s.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE",
                   help="any other step key, VALUE in YAML: retry=3, timeout.total=2h, resources.cpus_per_task=8, "
                        "on_failure=continue, retrieve=[out/*], tmpdir=job, title=... (repeatable)")
    s.add_argument("-i", "--input", action="append", help="NAME=VALUE for an input newly declared in the plan file")
    s.add_argument("--follow", action="store_true", help="watch it until it finishes (its output, then its result)")
    s.add_argument("--timeout", type=float, help="with --follow: seconds")

    s = sub.add_parser("hook", parents=[common], help=argparse.SUPPRESS)   # called by the hook `init --hook` installs
    s.set_defaults(fn=cmd_hook)
    s.add_argument("event", choices=["bash"], help="bash: remind an agent that ran compute outside an active run")

    s = add("export", cmd_export, "a finished run as a reproducibility protocol: protocol.yaml (the steps behind "
                                  "STEP), expected.json (their results), PROTOCOL.md")
    s.add_argument("run")
    s.add_argument("steps", nargs="+", help="the step(s) whose result the protocol reproduces, e.g. the report")

    s = add("ui", cmd_ui, "web UI of this project: live DAG, node details, decisions, timeline, plan history "
                          "(runs in the background; `flower ui stop` ends it)")
    s.add_argument("action", nargs="?", metavar="start|status|stop|[user@]host:/path",
                   help="default: start (or reuse) this project's UI; host:/path opens another machine's project "
                        "through an ssh tunnel")
    s.add_argument("--port", type=int, help="local TCP port (default: a stable port derived from the project)")
    s.add_argument("--flower", default="flower", help="host:/path: the flower command on that machine")
    s.add_argument("--ssh-arg", action="append", help="host:/path: extra ssh argument, e.g. --ssh-arg=-F --ssh-arg=~/ssh_config")
    s.add_argument("--no-browser", action="store_true", help="host:/path: only tunnel")
    s.add_argument("--foreground", action="store_true", help="serve in this terminal instead of the background")
    s.add_argument("--host", default="127.0.0.1", help="bind address with --foreground (default 127.0.0.1)")
    s.add_argument("--token", help="fixed access token, with --foreground")
    s.add_argument("--socket", help="also serve on this owner-only Unix socket, with --foreground")
    s.add_argument("--open", action="store_true", help="open a browser")

    def plan_cluster_args(sp):
        sp.add_argument("--plan", help="the plan whose `clusters:` defines the cluster (or --run)")
        sp.add_argument("--run", help="the run whose cluster to use, with the run's inputs (instead of --plan)")
        sp.add_argument("--cluster", required=True, help="a machine (flower remote list), or a cluster of the run/plan")
        sp.add_argument("-i", "--input", action="append", help="NAME=VALUE for the plan's inputs (repeatable)")
        sp.add_argument("--inputs", help="JSON file with the plan's inputs")
        sp.add_argument("--timeout", type=float, default=7200.0, help="seconds (default 2h)")

    s = add("remote", cmd_remote, "machines work runs on: add (probes it), list, check, login, remove, shell, exec, clean")
    rs = s.add_subparsers(dest="remote_action", required=True)
    e = rs.add_parser("add", parents=[common], help="add a machine: flower remote add NAME TARGET (an ssh alias, "
                                                    "user@host[:port], or local); flower probes the rest")
    e.add_argument("name")
    e.add_argument("target")
    e.add_argument("-i", "--identity-file", help="ssh key file (default: what ssh would use)")
    e.add_argument("--note", help="for agents and people, e.g. 'shared: ask before using more than 16 cores'")
    e.add_argument("--cores", type=int, help="the cores an agent may use there without asking")
    e.add_argument("--login", action="store_true",
                   help="the machine asks for a password or a code: open a shared connection here first (it stays "
                        "open until it drops or the machine is removed)")
    e = rs.add_parser("list", parents=[common], help="the machines, what they have, and what agents may use")
    e.add_argument("name", nargs="?")
    e = rs.add_parser("check", parents=[common], help="reach a machine (or all) and refresh what was probed")
    e.add_argument("name", nargs="?")
    e = rs.add_parser("login", parents=[common], help="reopen the shared ssh connection after it dropped (type your "
                                                      "password or second factor once)")
    e.add_argument("name")
    e = rs.add_parser("remove", parents=[common], help="remove a machine (and close its shared connection)")
    e.add_argument("name")
    e = rs.add_parser("shell", parents=[common], help="an interactive shell on a machine (NAME), or in a step's folder "
                                                      "there (--run RUN STEP)")
    e.add_argument("name", nargs="?")
    e.add_argument("step", nargs="?", help="with --run: the step whose folder to open")
    e.add_argument("--run")
    e = rs.add_parser("clean", parents=[common], help="the job folders of this project's finished runs on a machine: "
                                                      "list them with their size; -y removes them")
    e.add_argument("name")
    e.add_argument("-y", "--yes", action="store_true")
    e = rs.add_parser("exec", parents=[common], help="flower remote exec --cluster MACHINE [--run RUN] [--env E] -- <command>")
    plan_cluster_args(e)
    e.add_argument("--env", help="explore environment NAME: an exploration prefix, its activate.sh sourced")
    e.add_argument("--installed", action="store_true",
                   help="with --env: run in the frozen recipe's installed prefix (as steps do), not the exploration one")
    e.add_argument("command", nargs="+", help="the command, after --")

    s = add("env", cmd_env, "environment recipes: new, freeze, replay, check, show")
    es = s.add_subparsers(dest="env_action", required=True)
    e = es.add_parser("new", parents=[common], help="create envs/<name>/ with template scripts")
    e.add_argument("name"); e.add_argument("--dir", help="project directory (default: here)")
    e = es.add_parser("freeze", parents=[common], help="pin the recipe (hash its files)")
    e.add_argument("name"); e.add_argument("--dir")
    e = es.add_parser("show", parents=[common], help="recipes and their state")
    e.add_argument("name", nargs="?"); e.add_argument("--dir")
    e = es.add_parser("replay", parents=[common], help="run the frozen recipe on a cluster: check, else setup + check")
    e.add_argument("name"); plan_cluster_args(e); e.add_argument("--dir")
    e.add_argument("--fresh", action="store_true", help="into a new empty prefix: proves the recipe works from scratch")
    e = es.add_parser("check", parents=[common], help="run only check.sh (with activate.sh) on a cluster")
    e.add_argument("name"); plan_cluster_args(e); e.add_argument("--dir")

    s = sub.add_parser("_driver", help=argparse.SUPPRESS)
    s.add_argument("run")
    s.add_argument("--root", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_driver)
    sub._choices_actions = [a for a in sub._choices_actions if a.dest not in ("hook", "_driver")]  # internal
    return p


def _is_decision(args) -> bool:
    return getattr(args, "cmd", None) in ("approve", "reject")


ALIASES = {"sync": ["rerun"], "logs": ["show"]}   # earlier names: `sync RUN` = `rerun RUN`, `logs RUN STEP` = `show --logs`


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ALIASES:
        argv = ALIASES[argv[0]] + argv[1:] + (["--logs"] if argv[0] == "logs" else [])
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
            "finish the step's task and report it in the step's outputs"), 2)
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
