"""Shared helpers for tests/agents (kept out of conftest.py so test modules can import them)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fakes" / "fake_harness.py"
FIXTURES = HERE / "fixtures"
REPO = HERE.parent.parent
FLOWER_BIN = REPO / ".venv" / "bin" / "flower"
PY = sys.executable


class Fake:
    """One scripted fake harness (scenario file + call log)."""

    def __init__(self, base: Path, name: str, flavor: str, turns: list[dict]):
        self.dir = base / f"fake-{name}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flavor = flavor
        self.scenario = self.dir / "scenario.json"
        self.log = self.dir / "calls.jsonl"
        self.set_turns(turns)

    def set_turns(self, turns: list[dict]) -> None:
        self.scenario.write_text(json.dumps({"turns": turns}))

    def harness(self, **extra) -> dict:
        h = {"name": self.flavor, "command": [PY, str(FAKE), self.flavor],
             "env": {"FAKE_SCENARIO": str(self.scenario), "FAKE_LOG": str(self.log)}}
        env = extra.pop("env", None)
        if env:
            h["env"].update(env)
        h.update(extra)
        return h

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


def agent_node(nid: str, harness: dict, prompt: str = "Do the task.", **kw) -> dict:
    n = {"id": nid, "kind": "agent", "prompt": prompt, "harness": harness}
    n.update(kw)
    return n


def make_plan(nodes: list[dict], pid: str = "agent-test", **kw) -> dict:
    p = {"flower": 1, "id": pid, "title": "Agent test", "description": "Tests the agent node contract.",
         "nodes": nodes}
    p.update(kw)
    return p


def events(eng, etype: str | None = None, node: str | None = None) -> list[dict]:
    evs = eng.journal.read()
    return [e for e in evs if (etype is None or e["eventType"] == etype) and (node is None or e.get("nodeId") == node)]


def last_attempt(st, nid: str):
    return st.nodes[nid].attempts[-1]


def write_plan(path: Path, plan: dict) -> Path:
    import yaml
    path.write_text(yaml.safe_dump(plan, sort_keys=False))
    return path


def ff(*args, cwd=None, env=None, input=None, timeout=90, json_out=True):
    """Run the real CLI in a subprocess. Returns (exit_code, parsed_json_or_None, completed_process)."""
    argv = [str(FLOWER_BIN), *[str(a) for a in args]]
    if json_out:
        argv.append("--json")
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(argv, cwd=cwd, env=e, input=input, capture_output=True, text=True, timeout=timeout)
    data = None
    if json_out:
        out = p.stdout.strip()
        try:
            data = json.loads(out.splitlines()[-1]) if out else None
        except ValueError:
            data = None
    return p.returncode, data, p
