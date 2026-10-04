"""Getting to the UI: the owner-only Unix socket (no token in the link), one background server per project
(``flower ui`` / ``status`` / ``stop``), and ``flower ui host:path`` from a laptop through an ssh tunnel."""
from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flower.engine import create_run
from flower.ui import background_info, ensure_background, serve, stop_background

from core_helpers import drive

PLAN = {"flower": 1, "id": "ui", "title": "UI access test",
        "nodes": [{"id": "a", "kind": "shell", "run": "echo hi"}]}


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=10)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path_)


def via_socket(sock: Path, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    c = UnixHTTP(str(sock))
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


@pytest.fixture
def project(home):
    eng = create_run(PLAN, {}, root=home, approve=True)
    drive(eng, timeout=20)
    return home, eng


@pytest.fixture
def socket_server(project, tmp_path):
    root, eng = project
    sock = tmp_path / "ui.sock"
    ready = threading.Event()
    box = {}

    def on_ready(srv, ui):
        box["srv"] = srv
        ready.set()

    th = threading.Thread(target=serve, kwargs={"root": root, "port": None, "token": "tok", "ready": on_ready,
                                                "unix_socket": sock}, daemon=True)
    th.start()
    assert ready.wait(5)
    yield sock, eng
    box["srv"].shutdown()


def test_socket_is_owner_only_and_needs_no_token_to_read(socket_server):
    sock, eng = socket_server
    assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
    status, body = via_socket(sock, "GET", "/")
    assert status == 200 and b"tok" in body  # the page carries the token its actions need
    status, body = via_socket(sock, "GET", "/api/runs")
    assert status == 200 and json.loads(body)["runs"][0]["id"] == eng.paths.run_id


def test_socket_refuses_foreign_host_headers(socket_server):
    sock, _ = socket_server  # another web site resolving its name to 127.0.0.1 (DNS rebinding) gets nothing
    assert via_socket(sock, "GET", "/api/runs", {"Host": "evil.example:8765"})[0] == 403
    assert via_socket(sock, "GET", "/api/runs", {"Host": "localhost:8765"})[0] == 200
    assert via_socket(sock, "GET", "/api/runs", {"Host": "127.0.0.1:8701"})[0] == 200


def test_socket_actions_still_need_the_page_token(socket_server):
    sock, eng = socket_server
    url = f"/api/runs/{eng.paths.run_id}/note"
    body = json.dumps({"text": "hello"}).encode()
    assert via_socket(sock, "POST", url, {"Content-Type": "application/json"}, body)[0] == 403  # CSRF guard
    status, data = via_socket(sock, "POST", url, {"Content-Type": "application/json", "X-Flower-Token": "tok"}, body)
    assert status == 200 and json.loads(data)["ok"]


def test_background_server_is_started_once_reused_and_stopped(project):
    root, _ = project
    first = ensure_background(root)
    try:
        assert first["reused"] is False and Path(first["socket"]).exists()
        assert stat.S_IMODE(os.stat(root / ".flower" / "ui.json").st_mode) == 0o600
        again = ensure_background(root)
        assert again["reused"] is True and again["pid"] == first["pid"] and again["token"] == first["token"]
        assert via_socket(Path(first["socket"]), "GET", "/api/runs")[0] == 200
    finally:
        assert stop_background(root)["pid"] == first["pid"]
    assert background_info(root) is None
    restarted = ensure_background(root)  # same token and port after a restart: bookmarks keep working
    try:
        assert restarted["reused"] is False and restarted["pid"] != first["pid"]
        assert restarted["token"] == first["token"] and restarted["port"] == first["port"]
    finally:
        stop_background(root)


def test_a_server_running_older_code_is_replaced(project, monkeypatch):
    """After an upgrade (or an edit of flower), `flower ui` reused the old server and the user kept seeing the old
    page and API: a server whose code stamp differs is replaced, with the same token and port."""
    from flower import ui as uimod
    root, _ = project
    first = ensure_background(root)
    try:
        real = uimod.code_stamp()
        monkeypatch.setattr(uimod, "code_stamp", lambda: [real[0] + 1, real[1]])   # flower's code changed
        second = ensure_background(root)
        assert second["reused"] is False and second["pid"] != first["pid"]
        assert second["token"] == first["token"] and second["port"] == first["port"]
        assert ensure_background(root)["reused"] is True
    finally:
        stop_background(root)


def test_cli_ui_start_status_stop(project, cli):
    root, _ = project
    try:
        code, res = cli("ui")
        assert code == 0 and res["data"]["socket"] and res["data"]["reused"] is False
        code, res = cli("ui", "status")
        assert code == 0 and res["data"]["running"] is True
        code, text = cli("ui", "status", as_json=False)
        assert f"ssh -N -L {res['data']['port']}:{res['data']['socket']} " in text
    finally:
        code, res = cli("ui", "stop")
    assert res["data"]["stopped"] is True
    assert cli("ui", "status")[1]["data"]["running"] is False


# ---------------------------------------------------------------- flower ui host:path, with a fake ssh that forwards

FAKE_SSH = r'''#!PYTHON
"""Fake OpenSSH client: runs the remote command locally, and -N -L PORT:TARGET forwards for real."""
import os, socket, subprocess, sys, threading
args = sys.argv[1:]
fwd, nocmd, rest = None, False, []
i = 0
while i < len(args):
    a = args[i]
    if a == "-N": nocmd = True
    elif a == "-L": fwd = args[i + 1]; i += 1
    elif a in ("-o", "-F", "-p", "-i", "-l", "-J"): i += 1
    elif a.startswith("-"): pass
    else: rest.append(a)
    i += 1
host, cmd = rest[0], " ".join(rest[1:])
with open(os.environ["FAKESSH_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")
if not nocmd:
    if cmd.startswith("bash -lc "):
        cmd = "bash -c " + cmd[len("bash -lc "):]
    os.execvp("bash", ["bash", "-c", cmd])
lport, _, target = fwd.partition(":")
if not target.startswith("localhost:") and os.environ.get("FAKESSH_NO_STREAMLOCAL"):
    sys.stderr.write("open failed: administratively prohibited\n"); sys.exit(255)
def dial():
    if target.startswith("localhost:"):
        return socket.create_connection(("127.0.0.1", int(target.split(":")[1])))
    s = socket.socket(socket.AF_UNIX); s.connect(target); return s
def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError: pass
    finally:
        try: b.shutdown(socket.SHUT_WR)
        except OSError: pass
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", int(lport))); srv.listen(16)
while True:
    c, _ = srv.accept(); u = dial()
    threading.Thread(target=pump, args=(c, u), daemon=True).start()
    threading.Thread(target=pump, args=(u, c), daemon=True).start()
'''


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    d = tmp_path / "fakessh-bin"
    d.mkdir()
    p = d / "ssh"
    p.write_text(FAKE_SSH.replace("#!PYTHON", "#!" + sys.executable))
    p.chmod(0o755)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKESSH_LOG", str(log))
    return log


def _flower_open(root: Path, tmp_path: Path, port: int):
    out = tmp_path / "open.out"
    proc = subprocess.Popen([sys.executable, "-m", "flower", "ui", f"box:{root}", "--no-browser",
                             "--port", str(port), "--flower", f"{sys.executable} -m flower"],
                            stdout=open(out, "w"), stderr=subprocess.STDOUT, env={**os.environ}, start_new_session=True)
    t0 = time.time()
    while "open:" not in out.read_text() and time.time() - t0 < 30 and proc.poll() is None:
        time.sleep(0.2)
    text = out.read_text()
    assert "open:" in text, text
    return proc, text.split("open:", 1)[1].split()[0]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _tunnels(log_marker: str) -> list[str]:
    out = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "args="], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "ExitOnForwardFailure" in l and log_marker in l]


@pytest.mark.parametrize("streamlocal", [True, False])
def test_flower_ui_remote_starts_tunnels_and_cleans_up(project, tmp_path, fake_ssh, monkeypatch, streamlocal):
    root, eng = project
    if not streamlocal:  # an sshd that forbids forwarding to Unix sockets: fall back to the TCP port + token
        monkeypatch.setenv("FAKESSH_NO_STREAMLOCAL", "1")
    port = _free_port()
    proc, url = _flower_open(root, tmp_path, port)
    try:
        assert url.startswith(f"http://localhost:{port}/")
        assert ("token=" in url) is (not streamlocal)
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as r:
            assert r.status == 200
        api = f"http://localhost:{port}/api/runs" + (url[url.index("?"):] if "?" in url else "")
        with urllib.request.urlopen(api, timeout=10) as r:
            assert json.loads(r.read())["runs"][0]["id"] == eng.paths.run_id
        assert background_info(root) is not None  # the UI was started on the "remote" side
    finally:
        proc.terminate()  # like closing the terminal: the tunnel must not be orphaned
        proc.wait(10)
        stop_background(root)
    marker = f"{port}:"
    t0 = time.time()
    while _tunnels(marker) and time.time() - t0 < 10:
        time.sleep(0.2)
    assert _tunnels(marker) == []


def test_flower_ui_rejects_a_remote_target_without_path(cli):
    code, res = cli("ui", "justahost:")
    assert code != 0 and res["error"]["code"] == "usage"
    code, res = cli("ui", "frobnicate")
    assert code != 0 and res["error"]["code"] == "usage"
