"""Fixtures for the agent / harness / CLI / MCP / report test-suite.

Every test runs in its own FORGEFLOW_HOME (tmp_path) and with a PATH whose first entry holds *poisoned*
``claude`` / ``codex`` / ``pi`` executables, so an accidental fallback to a real harness fails loudly
instead of calling an LLM.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentkit import Fake  # noqa: E402

POISON = """#!/bin/sh
echo "REAL HARNESS $0 MUST NOT BE CALLED FROM TESTS" >&2
exit 99
"""


@pytest.fixture(autouse=True)
def ff_home(tmp_path, monkeypatch):
    home = tmp_path / "ffhome"
    home.mkdir()
    poison = tmp_path / "poison-bin"
    poison.mkdir()
    for exe in ("claude", "codex", "pi"):
        p = poison / exe
        p.write_text(POISON)
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{poison}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FORGEFLOW_HOME", str(home))
    monkeypatch.setenv("FORGEFLOW_ACTOR", "test:pytest")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(tmp_path)
    yield home
    _reap_leftovers(home)


def _reap_leftovers(home: Path) -> None:
    """Kill any runner/harness a test left behind (hanging fakes must never outlive the test)."""
    import json
    import signal
    for rj in home.glob(".forgeflow/runs/*/nodes/*/*/proc*/runner.json"):
        if (rj.parent / "exit.json").exists():
            continue
        try:
            info = json.loads(rj.read_text())
        except ValueError:
            continue
        for fn, arg in ((os.killpg, info.get("child_pgid")), (os.kill, info.get("runner_pid"))):
            if arg:
                try:
                    fn(arg, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    drv = list(home.glob(".forgeflow/runs/*/driver.json"))
    for d in drv:
        try:
            pid = json.loads(d.read_text()).get("pid")
            if pid:
                os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


@pytest.fixture
def fake(tmp_path):
    made: dict[str, Fake] = {}

    def make(flavor: str, turns: list[dict], name: str | None = None) -> Fake:
        name = name or f"{flavor}{len(made)}"
        f = Fake(tmp_path, name, flavor, turns)
        made[name] = f
        return f

    return make


@pytest.fixture
def run_plan(ff_home):
    """Create + approve + drive a plan in-process until it settles. Returns (engine, state)."""
    from forgeflow.engine import create_run

    def go(plan: dict, timeout: float = 45.0, approve: bool = True):
        eng = create_run(plan, {}, root=ff_home, actor="test:pytest", approve=approve)
        eng.drive(until="settled", timeout=timeout)
        return eng, eng.state()

    return go


