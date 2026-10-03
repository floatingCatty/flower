"""Detached process supervisor: ``python -m flower._runner <proc_dir>``.

Every local node process (shell, function, agent harness) runs under one of these. The runner is
started in its own session by the engine and is *not* a child the engine waits on, so a node keeps
running when the CLI/driver exits or crashes; any later ``tick`` re-attaches by reading the files below
(the "park on job" pattern of Smithers 1.0 / Temporal async completion, applied to local processes).

Files in ``proc_dir``::

    spec.json      written by the engine: argv, env, cwd, stdin, timeouts
    runner.json    written at start: runner pid/pgid, child pid/pgid, host, /proc start ticks
    heartbeat      touched every HEARTBEAT seconds while alive (liveness across hosts)
    stdout.log / stderr.log
    exit.json      written atomically when the child ends: returncode, signal, timed_out, cancelled …

Cancellation: SIGTERM to the runner -> it TERMs the child's process group, waits GRACE, then KILLs.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HEARTBEAT = 10.0
GRACE = 10.0


def proc_start_ticks(pid: int) -> int | None:
    """Kernel start time of a pid (detects pid reuse)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode()
        return int(data[data.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    with open(tmp, "w") as fh:
        json.dump(value, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _size(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except FileNotFoundError:
            pass
    return total


def main(proc_dir: str) -> int:
    d = Path(proc_dir)
    spec = json.loads((d / "spec.json").read_text())
    out_p, err_p = d / "stdout.log", d / "stderr.log"
    env = os.environ.copy()
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    for k in spec.get("unset_env") or []:
        env.pop(k, None)
    stdin = subprocess.DEVNULL
    if spec.get("stdin_file"):
        stdin = open(spec["stdin_file"], "rb")
    started = time.time()
    started_iso = _now()
    exe = spec["argv"][0]
    found = (os.access(exe, os.X_OK) and not os.path.isdir(exe)) if os.sep in exe else \
        shutil.which(exe, path=env.get("PATH")) is not None
    if not found:  # report a missing executable as a spawn error, not as a misbehaving payload
        with open(err_p, "ab") as err:
            err.write(f"flower: executable {exe!r} not found (PATH={env.get('PATH', '')})\n".encode())
        _write_json(d / "exit.json", {"returncode": 127, "signal": None, "timed_out": False, "idle_timeout": False,
                                      "cancelled": False, "spawn_error": f"executable {exe!r} not found",
                                      "started_at": started_iso, "ended_at": _now(), "duration_s": 0.0})
        return 0
    with open(out_p, "ab") as out, open(err_p, "ab") as err:
        try:
            # the tiny sh wrapper records the payload's exit code itself, so a verdict survives even if this
            # runner is killed while the payload keeps going (poll_process falls back to child_rc)
            wrapped = ["/bin/sh", "-c", 'trap "" HUP; "$@"; rc=$?; echo "$rc" > "$0.tmp" && mv "$0.tmp" "$0"; exit "$rc"',
                       str(d / "child_rc"), *spec["argv"]]
            child = subprocess.Popen(wrapped, cwd=spec.get("cwd") or None, env=env, stdin=stdin,
                                     stdout=out, stderr=err, start_new_session=True)
        except OSError as exc:
            err.write(f"flower: failed to start {spec['argv'][0]!r}: {exc}\n".encode())
            _write_json(d / "exit.json", {"returncode": 127, "signal": None, "timed_out": False,
                                          "idle_timeout": False, "cancelled": False, "spawn_error": str(exc),
                                          "started_at": started_iso, "ended_at": _now(), "duration_s": 0.0})
            return 0
    if stdin is not subprocess.DEVNULL:
        stdin.close()
    import socket
    _write_json(d / "runner.json", {
        "runner_pid": os.getpid(), "runner_pgid": os.getpgid(0), "runner_start": proc_start_ticks(os.getpid()),
        "child_pid": child.pid, "child_pgid": child.pid, "child_start": proc_start_ticks(child.pid),
        "host": socket.gethostname(), "started_at": started_iso,
    })

    state = {"cancelled": False}

    def on_term(signum, frame):  # noqa: ARG001
        state["cancelled"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    total = spec.get("timeout_total")
    idle = spec.get("timeout_idle")
    last_size, last_change = -1, time.time()
    last_beat = 0.0
    timed_out = idle_out = False
    kill_at = None
    while True:
        rc = child.poll()
        if rc is not None:
            break
        nowt = time.time()
        if nowt - last_beat >= HEARTBEAT:
            (d / "heartbeat").write_text(_now())
            last_beat = nowt
        size = _size([out_p, err_p])
        if size != last_size:
            last_size, last_change = size, nowt
        if not state["cancelled"] and (d / "cancel").exists():  # cross-host cancel request
            state["cancelled"] = True
        if kill_at is None:
            reason = None
            if state["cancelled"]:
                reason = "cancel"
            elif total and nowt - started > total:
                timed_out, reason = True, "timeout"
            elif idle and nowt - last_change > idle:
                idle_out, reason = True, "idle"
            if reason:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                kill_at = nowt + GRACE
        elif nowt >= kill_at:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            kill_at = float("inf")
        time.sleep(0.2)
    # reap stragglers left in the child's group
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _write_json(d / "exit.json", {
        "returncode": rc if rc >= 0 else None, "signal": -rc if rc < 0 else None,
        "timed_out": timed_out, "idle_timeout": idle_out, "cancelled": state["cancelled"],
        "started_at": started_iso, "ended_at": _now(), "duration_s": round(time.time() - started, 3),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
