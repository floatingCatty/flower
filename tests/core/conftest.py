"""Fixtures for the flower core test-suite (journal / plan / template / fold / engine / local executors).

Every test gets an isolated FLOWER_HOME under ``tmp_path`` and a fixed actor. Any detached
``_runner`` (and its child process group) left behind by a test is killed on teardown.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest



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
        except OSError:
            continue
        if token in cmd or token in env:
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


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FLOWER_HOME", str(h))
    monkeypatch.setenv("FLOWER_ACTOR", "human:tester")
    monkeypatch.setenv("FLOWER_MACHINES", str(tmp_path / "machines.yaml"))   # never the person's own
    monkeypatch.delenv("FLOWER_INSIDE_RUN", raising=False)
    monkeypatch.delenv("BASH_ENV", raising=False)  # a sourced ~/.bashrc makes every shell node ~0.5s slower
    monkeypatch.chdir(tmp_path)
    yield h
    kill_leftovers(str(tmp_path))


@pytest.fixture
def src_dir(tmp_path):
    d = tmp_path / "src"
    d.mkdir(exist_ok=True)
    return d


@pytest.fixture
def mkplan(src_dir):
    """mkplan(nodes, **top) -> plan dict with `_source.dir` pointing at the per-test module dir."""

    def make(nodes, **top):
        plan = {"flower": 1, "id": top.pop("id", "t"), "nodes": nodes, "_source": {"dir": str(src_dir)}}
        plan.update(top)
        return plan

    return make


@pytest.fixture
def start(home):
    """start(plan, inputs=None, approve=True) -> Engine."""
    from flower.engine import create_run

    def go(plan, inputs=None, approve=True, **kw):
        return create_run(plan, inputs or {}, root=home, approve=approve, **kw)

    return go


@pytest.fixture
def cli(home, capsys):
    """cli(*argv) -> (exit_code, parsed_json_or_text). Runs flower.cli.main in-process."""
    from flower.cli import main

    def call(*argv, as_json=True):
        argv = list(argv)
        if as_json and "--json" not in argv:
            # before a `--` (whatever follows it is a command, e.g. `flower add RUN ID -- <command>`)
            argv.insert(argv.index("--") if "--" in argv else len(argv), "--json")
        capsys.readouterr()
        code = main(argv)
        cap = capsys.readouterr()
        if as_json:
            text = cap.out.strip()
            try:
                return code, json.loads(text)
            except ValueError:
                raise AssertionError(f"not one JSON object on stdout: {text[:500]!r} (stderr {cap.err[:300]!r})")
        return code, cap.out + cap.err

    return call
