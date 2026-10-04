"""``flower ui`` — a local web view of runs: live DAG, node details, decisions, timeline, plan history.

Zero dependencies (stdlib ``http.server``), no CDN (works offline on clusters). Everything shown is
folded from each run's ``events.jsonl``; every action goes through the same Engine calls as the CLI.

Security model (it is meant to be reached through an SSH tunnel on shared machines):
* binds to 127.0.0.1 by default;
* every mutating request needs the project's token (``X-Flower-Token``), which the page itself carries;
* on the TCP port, reading needs the token too (other users of a shared host can reach 127.0.0.1);
* the Unix socket ``.flower/ui.sock`` is mode 0600, so only its owner can connect: requests through it
  (an ``ssh -L PORT:/path/ui.sock`` tunnel) need no token in the link. A Host-header check stops other
  web sites from reaching it through the browser (DNS rebinding); the page token still guards actions;
* files are only served from the run directory or from paths the run itself recorded as outputs.

``flower ui`` keeps one server per project running in the background (``.flower/ui.json``); it is
reused by later calls, and ``flower ui stop`` ends it.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .engine import Engine, driver_alive
from .plan import Graph, diff_plans, on_cluster
from .render import bound, command_text, describe_event, display_title, kind_label, node_activity, node_time, what
from .rundir import STATE_DIR, RunPaths, list_runs
from .state import TERMINAL_RUN, NodeState, RunState
from .util import (FlowerError, atomic_write_json, first_line, fmt_duration, hostname, now_iso, parse_iso,
                   read_json, seconds_since, tail_text)

MAX_TEXT = 200_000


def _elapsed(st: RunState) -> float | None:
    if st.completed_at:
        return (parse_iso(st.completed_at) - parse_iso(st.created_at)).total_seconds()
    return seconds_since(st.created_at)


class UIState:
    def __init__(self, root: Path, actor: str, token: str, drive: bool):
        self.root = Path(root)
        self.actor = actor
        self.token = token
        self.drive = drive
        self._lock = threading.Lock()

    def engine(self, run_id: str) -> Engine:
        if run_id not in list_runs(self.root):
            raise FlowerError("run_not_found", f"no run {run_id!r}")
        return Engine(RunPaths(self.root, run_id))

    # ------------------------------------------------------------ read models
    def runs(self) -> list[dict]:
        out = []
        for rid in reversed(list_runs(self.root)):
            try:
                st = Engine(RunPaths(self.root, rid)).state()
            except FlowerError:
                continue
            c = st.counts()
            out.append({"id": rid, "title": st.title, "status": st.status, "created": st.created_at,
                        "done": c.get("succeeded", 0), "total": sum(c.values()), "decisions": len(st.open_gates()),
                        "elapsed": fmt_duration(_elapsed(st))})
        return out

    def run(self, run_id: str) -> dict:
        eng = self.engine(run_id)
        st = eng.state()
        g = st.graph()
        depth = g.depth()
        nodes = []
        for nid in g.topo():
            spec = g.nodes[nid]
            ns = st.nodes.get(nid) or NodeState(nid)
            a = ns.last
            nodes.append({
                "id": nid, "kind": spec.get("kind"), "label": kind_label(spec), "title": display_title(spec, nid),
                "what": what(spec), "description": bound(spec.get("description"), spec),
                "status": ns.status, "depth": depth.get(nid, 0), "needs": g.needs[nid],
                "expanded_from": spec.get("expanded_from"), "foreach": spec.get("foreach") is not None,
                "attempt": a.n if a else 0, "time": node_time(ns),
                "activity": node_activity(st, eng.paths, ns, spec),
                "reused": bool(ns.result and ns.result.reused_from), "gate": ns.gate_id,
                "added_by": self._added_by(st, nid),
            })
        gates = [{"id": gt.id, "subject": gt.subject, "node": gt.node, "message": gt.message,
                  "decisions": gt.decisions, "requested_at": gt.requested_at,
                  "diff": gt.extra.get("diff") if gt.extra else None}
                 for gt in st.open_gates()]
        drv = driver_alive(eng.paths)
        active = st.status not in TERMINAL_RUN and st.status != "awaiting_approval"
        stalled = active and not drv and any(n["status"] in ("running", "retrying", "pending") for n in nodes) \
            and not gates
        return {
            "id": st.run_id, "title": st.title, "plan_id": st.plan_id, "status": st.status, "reason": st.status_reason,
            "created": st.created_at, "completed": st.completed_at, "elapsed": fmt_duration(_elapsed(st)),
            "generation": st.generation, "digest": (st.digest or "")[7:19], "base_digest": (st.base_digest or "")[7:19],
            "approved_by": st.approved_by, "inputs": st.inputs, "counts": st.counts(),
            "description": st.plan.get("description") or "", "nodes": nodes, "gates": gates,
            "driver": drv, "stalled": stalled, "last_seq": st.last_seq, "dir": str(eng.paths.dir),
        }

    def _added_by(self, st: RunState, nid: str) -> str | None:
        for gen in st.generations[1:]:
            am = st.amendments.get(gen.get("amendment") or "")
            if am and nid in (am.effects.get("added") or []):
                return am.proposed_by if not am.id.startswith("fx-") else None
        return None

    def node(self, run_id: str, nid: str) -> dict:
        eng = self.engine(run_id)
        st = eng.state()
        if nid not in st.nodes:
            raise FlowerError("node_not_found", f"no node {nid!r}")
        spec = st.node_spec(nid)
        ns = st.nodes[nid]
        g = st.graph()
        attempts = []
        for a in ns.attempts:
            adir = eng.paths.attempt_dir(nid, a.n)
            logs = {}
            sched = None
            if on_cluster(spec):
                from .hpc import scheduler_for
                sched = scheduler_for((st.plan.get("clusters") or {}).get(spec.get("cluster")) or {}).NAME
            if on_cluster(spec):
                from .hpc import log_files, scheduler_for
                jd = Path(a.job.get("job_dir") or "")
                local = jd if jd.is_dir() else adir / "job"
                jid = a.job.get("job_id")
                if jid:
                    cl = (st.plan.get("clusters") or {}).get(spec.get("cluster")) or {}
                    out_name, err_name = log_files(cl, jid)
                    tag = "slurm" if scheduler_for(cl).NAME == "slurm" else "remote"
                    logs[f"{tag} stdout"] = tail_text(local / out_name, 20000)
                    logs[f"{tag} stderr"] = tail_text(local / err_name, 20000)
            else:
                logs["stdout"] = tail_text(adir / "proc" / "stdout.log", 20000)
                logs["stderr"] = tail_text(adir / "proc" / "stderr.log", 20000)
            attempts.append({
                "n": a.n, "status": a.status, "started": a.started_at, "ended": a.ended_at,
                "duration": fmt_duration(a.duration_s) if a.duration_s is not None else fmt_duration(seconds_since(a.started_at)),
                "summary": a.summary, "rationale": a.rationale, "outputs": a.outputs, "inputs": a.inputs,
                "files": [{"name": k, **v, "image": str(v.get("path", "")).lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".gif"))}
                          for k, v in (a.files or {}).items()],
                "usage": a.usage, "error": a.error, "job": a.job,
                "reused_from": a.reused_from, "actor": a.actor, "dir": str(adir), "scheduler": sched,
                "logs": {k: v for k, v in logs.items() if v},
            })
        return {
            "id": nid, "kind": spec.get("kind"), "label": kind_label(spec), "title": display_title(spec, nid),
            "status": ns.status, "what": what(spec), "description": bound(spec.get("description"), spec),
            "command": command_text(spec), "environment": spec.get("environment"), "cluster": spec.get("cluster"),
            "generated": bool(spec.get("generated")),
            "needs": g.needs[nid], "children": g.children.get(nid, []), "when": spec.get("when"),
            "skipped_reason": ns.skipped_reason if ns.status == "skipped" else None,
            "stale_reason": ns.stale_reason if ns.status == "pending" else None,
            "retry_at": ns.retry_not_before if ns.status == "retrying" else None,
            "spec": {k: v for k, v in spec.items() if k not in ("bind",)}, "bind": spec.get("bind"),
            "attempts": list(reversed(attempts)), "added_by": self._added_by(st, nid),
        }

    def timeline(self, run_id: str) -> list[dict]:
        eng = self.engine(run_id)
        evs = eng.journal.read()
        out = []
        t0 = parse_iso(evs[0]["occurredAtIso"]) if evs else None
        for ev in evs:
            d = describe_event(ev)
            if not d:
                continue
            d = d.replace(" ✓ ", " done: ").replace(" ✗ ", " FAILED: ").replace("→", "->")  # font-safe in browsers
            out.append({"seq": ev["seq"], "at": ev["occurredAtIso"], "rel": fmt_duration((parse_iso(ev["occurredAtIso"]) - t0).total_seconds()),
                        "text": d, "type": ev["eventType"], "node": ev.get("nodeId"), "actor": ev.get("actor")})
        return out

    def history(self, run_id: str) -> list[dict]:
        st = self.engine(run_id).state()
        out = []
        prev = None
        for gen in st.generations:
            am = st.amendments.get(gen.get("amendment") or "")
            out.append({"generation": gen["generation"], "digest": gen["digest"][7:19], "at": gen.get("at"),
                        "by": gen.get("approved_by") or gen.get("by"), "amendment": gen.get("amendment"),
                        "rationale": am.rationale if am else None, "proposed_by": am.proposed_by if am else None,
                        "diff": diff_plans(prev, gen["plan"]) if prev else None,
                        "nodes": len(gen["plan"]["nodes"])})
            prev = gen["plan"]
        rejected = [{"id": am.id, "proposed_by": am.proposed_by, "rationale": am.rationale,
                     "reason": am.effects.get("reason"), "status": am.status}
                    for am in st.amendments.values() if am.status != "approved"]
        return {"generations": out, "other": rejected}

    def allowed_file(self, run_id: str, path: str) -> Path | None:
        eng = self.engine(run_id)
        p = Path(path)
        if not p.is_absolute():
            p = eng.paths.dir / p
        try:
            p = p.resolve()
        except OSError:
            return None
        if str(p).startswith(str(eng.paths.dir.resolve()) + os.sep) and p.is_file():
            return p
        st = eng.state()
        for ns in st.nodes.values():
            for a in ns.attempts:
                for f in (a.files or {}).values():
                    try:
                        if Path(f.get("path", "")).resolve() == p and p.is_file():
                            return p
                    except OSError:
                        continue
        return None

    # ------------------------------------------------------------ actions
    def act(self, run_id: str, action: str, body: dict) -> dict:
        eng = self.engine(run_id)
        by = self.actor
        with self._lock:
            if action == "answer":
                eng.answer(str(body.get("gate")), str(body.get("decision")), text=body.get("text") or None, by=by)
            elif action == "rerun":
                eng.rerun(str(body.get("node")), downstream=not body.get("only"), by=by,
                          reason=body.get("reason") or "rerun requested in flower ui")
            elif action == "cancel":
                eng.cancel(node=body.get("node") or None, reason="cancelled in flower ui", by=by)
            elif action == "note":
                eng.note(str(body.get("text") or ""), node=body.get("node") or None, by=by)
            elif action != "resume":
                raise FlowerError("bad_action", f"unknown action {action!r}")
            st = eng.state()
            driver = None
            if st.status not in TERMINAL_RUN and st.status != "awaiting_approval" or action == "answer":
                eng.tick()
                st = eng.state()
                if st.status not in TERMINAL_RUN and not driver_alive(eng.paths):
                    from .cli import spawn_driver
                    driver = spawn_driver(eng)
            return {"ok": True, "status": st.status, "driver": driver}


# ====================================================================== HTTP

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def make_handler(ui: UIState, trusted: bool = False):
    """``trusted``: the listener only lets this user in (the 0600 Unix socket), so reads need no token."""
    page_path = Path(__file__).parent / "data" / "ui.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = f"flower-ui/{__version__}"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _err(self, exc: Exception) -> None:
            if isinstance(exc, FlowerError):
                code = HTTPStatus.NOT_FOUND if exc.code.endswith("not_found") else HTTPStatus.BAD_REQUEST
                self._json(code, {"ok": False, "error": exc.to_dict()})
            else:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"ok": False, "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}})

        def _local_host(self) -> bool:
            host = (self.headers.get("Host") or "localhost").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            return name in LOCAL_HOSTS

        def _authorized(self, q: dict) -> bool:
            if trusted and self._local_host():
                return True
            given = self.headers.get("X-Flower-Token") or (q.get("token") or [""])[0]
            return secrets.compare_digest(given, ui.token)

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
            q = urllib.parse.parse_qs(u.query)
            if parts == ["favicon.ico"]:
                return self._send(204, b"", "image/x-icon")
            if not self._authorized(q):  # shared hosts: other users can reach 127.0.0.1 too
                msg = ("<!doctype html><meta charset=utf-8><title>flower ui</title>"
                       "<body style='font:15px system-ui;margin:3em'><h2>flower ui</h2>"
                       "<p>This page needs the access token. Open the full URL printed by <code>flower ui</code> "
                       "(it ends with <code>?token=…</code>).</p></body>")
                return self._send(403, msg.encode(), "text/html; charset=utf-8")
            try:
                if not parts or parts == ["index.html"] or (parts[0] == "runs" and len(parts) == 2):
                    page = page_path.read_text(encoding="utf-8")
                    body = page.replace("__FLOWER_TOKEN__", ui.token).replace("__FLOWER_ACTOR__", ui.actor) \
                               .replace("__FLOWER_VERSION__", __version__)
                    return self._send(200, body.encode(), "text/html; charset=utf-8",
                                      {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                                                  "style-src 'unsafe-inline'; script-src 'unsafe-inline'"})
                if parts[0] != "api":
                    return self._json(404, {"ok": False, "error": {"code": "not_found", "message": u.path}})
                if parts[1:] == ["runs"]:
                    return self._json(200, {"ok": True, "runs": ui.runs(), "actor": ui.actor})
                if len(parts) >= 3 and parts[1] == "runs":
                    rid = parts[2]
                    if len(parts) == 3:
                        return self._json(200, {"ok": True, "run": ui.run(rid)})
                    if parts[3] == "node" and len(parts) == 5:
                        return self._json(200, {"ok": True, "node": ui.node(rid, parts[4])})
                    if parts[3] == "timeline":
                        return self._json(200, {"ok": True, "events": ui.timeline(rid)})
                    if parts[3] == "history":
                        return self._json(200, {"ok": True, **ui.history(rid)})
                    if parts[3] == "file":
                        p = ui.allowed_file(rid, (q.get("path") or [""])[0])
                        if p is None:
                            return self._json(403, {"ok": False, "error": {"code": "forbidden",
                                                                           "message": "file is not part of this run"}})
                        data = p.read_bytes()
                        ctype = mimetypes.guess_type(str(p))[0] or "text/plain"
                        if ctype.startswith("text/") or ctype in ("application/json", "application/x-yaml") \
                                or p.suffix in (".log", ".out", ".err", ".xyz", ".md", ".yaml", ".yml", ".in"):
                            ctype = "text/plain; charset=utf-8"
                            data = data[-MAX_TEXT * 5:]
                        return self._send(200, data, ctype)
                return self._json(404, {"ok": False, "error": {"code": "not_found", "message": u.path}})
            except Exception as exc:  # noqa: BLE001
                return self._err(exc)

        def do_POST(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
            if not secrets.compare_digest(self.headers.get("X-Flower-Token", ""), ui.token):
                return self._json(403, {"ok": False, "error": {"code": "forbidden", "message": "missing or wrong token"}})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise FlowerError("bad_request", "body must be a JSON object")
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "runs":
                    return self._json(200, ui.act(parts[2], parts[3], body))
                return self._json(404, {"ok": False, "error": {"code": "not_found", "message": u.path}})
            except Exception as exc:  # noqa: BLE001
                return self._err(exc)

    return Handler


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        req, _ = super().get_request()
        return req, ("unix", 0)  # BaseHTTPRequestHandler expects a (host, port) client address


def _socket_answers(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _bind_unix(path: Path, handler) -> _UnixHTTPServer:
    """Listen on ``path`` with mode 0600 from the first instant (umask), replacing a stale socket."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if _socket_answers(path):
            raise OSError(f"another flower ui already listens on {path}")
        path.unlink()
    old = os.umask(0o177)
    try:
        srv = _UnixHTTPServer(str(path), handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return srv


def serve(root: Path, host: str = "127.0.0.1", port: int | None = 8765, actor: str | None = None,
          token: str | None = None, ready=None, unix_socket: Path | None = None) -> None:
    """Serve the UI on a TCP port (token required) and/or a Unix socket (owner only, no token in the link).
    ``port=None`` serves the socket only. ``ready(srv, ui)`` gets the TCP server when there is one."""
    from .util import default_actor
    ui = UIState(root, actor or default_actor(), token or secrets.token_urlsafe(16), drive=False)
    servers = []
    if port is not None:
        tcp = ThreadingHTTPServer((host, port), make_handler(ui))
        tcp.daemon_threads = True
        servers.append(tcp)
    if unix_socket is not None:
        servers.append(_bind_unix(Path(unix_socket), make_handler(ui, trusted=True)))
    if not servers:
        raise ValueError("nothing to serve on: give a port or a unix socket")
    ui.socket_path = str(unix_socket) if unix_socket is not None else None
    if ready:
        ready(servers[0], ui)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    try:
        servers[0].serve_forever(poll_interval=0.5)
    finally:
        for s in servers[1:]:
            s.shutdown()
        for s in servers:
            s.server_close()
        if unix_socket is not None:
            try:
                Path(unix_socket).unlink()
            except OSError:
                pass


# ====================================================================== one background server per project

def state_file(root: Path) -> Path:
    return Path(root) / STATE_DIR / "ui.json"


def socket_path(root: Path) -> Path:
    """``<project>/.flower/ui.sock``; a short private path when that is too long for a Unix socket (~107 B)."""
    p = Path(root).resolve() / STATE_DIR / "ui.sock"
    if len(str(p)) < 100:
        return p
    d = Path("/tmp") / f"flower-{os.getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d / (hashlib.sha256(str(p).encode()).hexdigest()[:16] + ".sock")


def stable_port(root: Path) -> int:
    """A port derived from the project path (8700-8899), so the project's link stays the same."""
    return 8700 + int(hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:8], 16) % 200


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def background_info(root: Path) -> dict | None:
    """This project's running background server, or None (a dead server's record is ignored)."""
    info = read_json(state_file(root))
    if not info or info.get("host") != hostname() or not _pid_alive(info.get("pid")):
        return None
    if not info.get("socket") or not _socket_answers(Path(info["socket"])):
        return None
    return info


def code_stamp() -> list:
    """flower's own code as the server would load it (Python and the page): newest mtime and file count."""
    pkg = Path(__file__).resolve().parent
    files = [p for p in pkg.rglob("*") if p.suffix in (".py", ".html") and "__pycache__" not in p.parts]
    return [max((p.stat().st_mtime_ns for p in files), default=0), len(files)]


def ensure_background(root: Path, actor: str | None = None, port: int | None = None,
                      wait_s: float = 15.0) -> dict:
    """Start this project's UI server in the background, or return the one already running. A server started
    with older flower code is replaced (same port and token), so an upgrade shows up on the next `flower ui`."""
    from .util import username
    root = Path(root).resolve()
    info = background_info(root)
    if info and info.get("code") == code_stamp():
        return {**info, "reused": True}
    if info:
        stop_background(root)
    old = read_json(state_file(root)) or {}
    token = old.get("token") or secrets.token_urlsafe(16)  # kept across restarts: bookmarks keep working
    want = int(port or old.get("port") or stable_port(root))
    port = want if _port_free(want) else next((p for p in range(want + 1, want + 50) if _port_free(p)), want)
    sock = socket_path(root)
    log = root / STATE_DIR / "ui.log"
    cmd = [sys.executable, "-m", "flower", "ui", "--foreground", "--port", str(port), "--token", token,
           "--socket", str(sock)]
    if actor:
        cmd += ["--actor", actor]
    env = {**os.environ, "FLOWER_HOME": str(root)}
    with open(log, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                                start_new_session=True, close_fds=True, env=env)
    deadline = time.time() + wait_s
    while True:
        if proc.poll() is not None:
            raise FlowerError("ui_start", f"the UI server exited at start-up (code {proc.returncode})", f"see {log}")
        if _socket_answers(sock):
            break
        if time.time() > deadline:
            proc.kill()
            raise FlowerError("ui_start", f"the UI server did not come up within {wait_s:.0f}s", f"see {log}")
        time.sleep(0.1)
    info = {"pid": proc.pid, "host": hostname(), "user": username(), "root": str(root), "socket": str(sock),
            "port": port, "token": token, "started_at": now_iso(), "version": __version__, "code": code_stamp()}
    atomic_write_json(state_file(root), info, mode=0o600)
    return {**info, "reused": False}


def stop_background(root: Path) -> dict | None:
    info = background_info(root)
    if not info:
        return None
    try:
        os.kill(int(info["pid"]), 15)
    except OSError:
        pass
    for _ in range(50):
        if not _pid_alive(info["pid"]):
            break
        time.sleep(0.1)
    return info


def access_text(info: dict) -> list[str]:
    """How to open it: on this machine, from a laptop (one ssh command, no token), or with `flower ui host:path`."""
    port, sock = info["port"], info["socket"]
    target = f"{info.get('user')}@{socket.getfqdn() or info['host']}"
    return [
        f"  on this machine:   http://localhost:{port}/?token={info['token']}",
        "  from your laptop:  run this there and keep it open, then browse to the link (no token needed):",
        f"      ssh -N -L {port}:{sock} {target}",
        f"      http://localhost:{port}",
        f"  or, with flower installed on the laptop:  flower ui {target}:{info['root']}",
        f"  or once, in the laptop's ~/.ssh/config under that host:  LocalForward {port} {sock}",
    ]
