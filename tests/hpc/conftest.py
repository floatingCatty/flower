"""Fixtures for the HPC ``job`` node test-suite (fake Slurm, local + ssh transports).

Every test gets:
  * an isolated FORGEFLOW_HOME under ``tmp_path``;
  * a fresh fake Slurm (``tmp_path/fs/{bin,state}``) with fast defaults (PEND 0.2 s, MinJobAge 5 s, no sacct lag);
  * teardown that kills every leftover fake-slurm runner / payload process that mentions ``tmp_path``.

``ff`` is the main helper object: it builds plans, creates runs, ticks them quickly (``Engine.drive`` sleeps up
to 15 s between ticks when only jobs run, too slow for tests), and inspects the fake Slurm state.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.engine import Engine, create_run
from forgeflow.testing.fakeslurm import install

TERMINAL = ("succeeded", "failed", "cancelled", "rejected")


# ---------------------------------------------------------------- process hygiene

def _procs_mentioning(token: str) -> list[int]:
    out = []
    me = os.getpid()
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace")
            with open(f"/proc/{d}/environ", "rb") as fh:
                env = fh.read().replace(b"\0", b" ").decode(errors="replace")
            try:
                cwd = os.readlink(f"/proc/{d}/cwd")
            except OSError:
                cwd = ""
        except OSError:
            continue
        if token in cmd or token in env or cwd.startswith(token):
            out.append(int(d))
    return out


def kill_leftovers(token: str) -> None:
    mypg = os.getpgid(0)
    for pid in _procs_mentioning(token):
        try:
            pg = os.getpgid(pid)
            if pg != mypg:
                os.killpg(pg, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------- the helper

class FF:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.fs = tmp_path / "fs"
        self.bin = install(self.fs)
        self.state_dir = self.fs / "state"
        # a BASH_ENV (e.g. ~/.bashrc) would be sourced by every `bash -c` the local transport runs: slow + noisy
        monkeypatch.delenv("BASH_ENV", raising=False)
        monkeypatch.delenv("ENV", raising=False)
        monkeypatch.setenv("FORGEFLOW_HOME", str(self.home))
        monkeypatch.setenv("FORGEFLOW_ACTOR", "test:hpc")
        monkeypatch.setenv("FAKESLURM_PEND_S", "0.1")
        monkeypatch.setenv("FAKESLURM_MINJOBAGE", "5")
        monkeypatch.setenv("FAKESLURM_SACCT_LAG", "0")
        monkeypatch.setenv("USER", os.environ.get("USER") or "tester")

    # ------------------------------------------------ plans / runs
    def cluster(self, **kw) -> dict:
        c = {"transport": "local", "bin_dir": str(self.bin), "min_poll": "0.5s", "lost_after": "3s"}
        c.update(kw)
        return c

    def plan(self, nodes: list[dict], clusters: dict | None = None, **kw) -> dict:
        p = {"forgeflow": 1, "id": kw.pop("id", "hpc-test"), "clusters": clusters or {"c": self.cluster()},
             "nodes": nodes}
        p.update(kw)
        return p

    @staticmethod
    def job(nid: str, script: str, **kw) -> dict:
        n = {"id": nid, "kind": "job", "cluster": "c", "script": script}
        n.update(kw)
        return n

    def run(self, plan: dict, inputs: dict | None = None) -> Engine:
        return create_run(plan, inputs or {}, root=self.home, approve=True)

    def drive(self, eng: Engine, timeout: float = 30.0, until=None, interval: float = 0.15):
        """Tick until terminal (or ``until(state)`` is true). Returns the final state."""
        t0 = time.time()
        while True:
            rep = eng.tick()
            st = eng.state()
            if until is not None:
                if until(st):
                    return st
            elif rep.status in TERMINAL or st.status in TERMINAL:
                return st
            if time.time() - t0 > timeout:
                raise AssertionError(f"timed out after {timeout}s; run status {st.status}; nodes "
                                     + ", ".join(f"{k}={v.status}" for k, v in st.nodes.items())
                                     + "\nlast events:\n" + "\n".join(
                                         f"  {e['eventType']} {json.dumps(e.get('payload'))[:200]}"
                                         for e in eng.journal.read()[-12:]))
            time.sleep(interval)

    def tick_for(self, eng: Engine, seconds: float, interval: float = 0.15) -> None:
        t0 = time.time()
        while time.time() - t0 < seconds:
            eng.tick()
            time.sleep(interval)

    # ------------------------------------------------ journal
    @staticmethod
    def events(eng: Engine, etype: str | None = None, node: str | None = None) -> list[dict]:
        out = []
        for e in eng.journal.read():
            if etype and not (e["eventType"] == etype or (etype.endswith(".") and e["eventType"].startswith(etype))):
                continue
            if node and e.get("nodeId") != node:
                continue
            out.append(e)
        return out

    @staticmethod
    def why(eng: Engine) -> str:
        """Compact description of failures / last events, for assertion messages."""
        lines = [f"{e['eventType']} {e.get('nodeId')} {json.dumps(e.get('payload'), default=str)[:400]}"
                 for e in eng.journal.read() if e["eventType"] in ("node.failed", "job.remote_error", "job.lost")]
        lines += [f"  {e['eventType']} {json.dumps(e.get('payload'), default=str)[:200]}" for e in eng.journal.read()[-6:]]
        return "\n".join(lines)

    @staticmethod
    def errors(st, nid: str) -> list[dict]:
        """Per-attempt error payloads (``None`` for attempts that did not fail)."""
        return [a.error for a in st.nodes[nid].attempts]

    @staticmethod
    def error_class(st, nid: str) -> str | None:
        a = st.nodes[nid].last
        return (a.error or {}).get("error_class") if a else None

    # ------------------------------------------------ fake slurm state
    def jobs(self) -> list[dict]:
        d = self.state_dir / "jobs"
        if not d.exists():
            return []
        out = []
        for p in sorted(d.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
            try:
                out.append(json.loads(p.read_text()))
            except ValueError:
                pass
        return out

    def jobs_named(self, suffix: str) -> list[dict]:
        return [j for j in self.jobs() if j["name"].endswith(suffix)]

    def wait_job_state(self, jid: str, states, timeout: float = 10.0) -> dict:
        states = {states} if isinstance(states, str) else set(states)
        t0 = time.time()
        while time.time() - t0 < timeout:
            for j in self.jobs():
                if j["id"] == jid and j["state"] in states:
                    return j
            time.sleep(0.1)
        raise AssertionError(f"fake job {jid} never reached {states}: {[ (j['id'], j['state']) for j in self.jobs()]}")

    def faults(self, rules: list[dict]) -> None:
        (self.state_dir / "faults.json").write_text(json.dumps(rules))

    # ------------------------------------------------ command wrapping
    def real_cmd(self, name: str) -> Path:
        """Path to the original fake-slurm wrapper (saved the first time a command is replaced)."""
        real = self.bin / f"{name}.real"
        if not real.exists():
            (self.bin / name).rename(real)
        return real

    def replace_cmd(self, name: str, body: str) -> None:
        """Replace bin/<name> with a shell script. ``$REAL`` in the body is the original command."""
        real = self.real_cmd(name)
        p = self.bin / name
        p.write_text(f"#!/bin/sh\nREAL={shlex.quote(str(real))}\n{body}\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def restore_cmd(self, name: str) -> None:
        real = self.bin / f"{name}.real"
        if real.exists():
            real.replace(self.bin / name)

    def log_cmd(self, name: str) -> Path:
        """Wrap a command so every invocation's argv is appended to a log file; returns the log path."""
        log = self.tmp / f"{name}.calls"
        self.replace_cmd(name, f'echo "$*" >> {shlex.quote(str(log))}\nexec "$REAL" "$@"')
        return log


@pytest.fixture
def ff(tmp_path, monkeypatch):
    h = FF(tmp_path, monkeypatch)
    yield h
    kill_leftovers(str(tmp_path))


@pytest.fixture
def clock(monkeypatch):
    """Shift the job executor's notion of time (backoffs, min_poll, lost grace) without sleeping."""
    import forgeflow.executors.job as jobmod

    state = {"offset": 0.0}
    fake = SimpleNamespace(time=lambda: time.time() + state["offset"], sleep=time.sleep)
    monkeypatch.setattr(jobmod, "time", fake)

    class Clock:
        def advance(self, s: float) -> None:
            state["offset"] += s

        def now(self) -> float:
            return fake.time()

    return Clock()


def node_attempts(st, nid: str):
    return st.nodes[nid].attempts


FORGEFLOW_BIN = Path(sys.executable).parent / "forgeflow"
