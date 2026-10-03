"""Where things live on disk.

    <project>/.flower/
        runs/<run-id>/
            events.jsonl            the journal (single source of truth)
            plan.yaml               human-readable copy of the approved base plan (generation 0)
            plan.current.yaml       current plan after approved amendments (regenerated, derived)
            pending/                gate requests (*.request.json) and answers (*.answer.json)
            signals/                external signal drop-box (*.json)
            nodes/<node>/a<N>/      one directory per attempt: work/, proc/, result files
            report.md / report.html generated, derived
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .util import FlowerError

STATE_DIR = ".flower"


def fs_name(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", node_id.replace("[", ".").replace("]", ""))


def find_root(start: str | os.PathLike | None = None, create: bool = False) -> Path:
    env = os.environ.get("FLOWER_HOME")
    if env:
        root = Path(env).expanduser().resolve()
        if create:
            (root / STATE_DIR / "runs").mkdir(parents=True, exist_ok=True)
        return root
    cur = Path(start or os.getcwd()).resolve()
    for d in [cur, *cur.parents]:
        if (d / STATE_DIR).is_dir():
            return d
    if create:
        (cur / STATE_DIR / "runs").mkdir(parents=True, exist_ok=True)
        return cur
    raise FlowerError("no_project", f"no {STATE_DIR}/ directory in {cur} or its parents",
                         "Run `flower init` here, or `flower run <plan.yaml>` (creates it), "
                         "or set FLOWER_HOME.")


class RunPaths:
    def __init__(self, root: Path, run_id: str):
        self.root = Path(root)
        self.run_id = run_id
        self.dir = self.root / STATE_DIR / "runs" / run_id
        self.events = self.dir / "events.jsonl"
        self.plan_file = self.dir / "plan.yaml"
        self.current_plan_file = self.dir / "plan.current.yaml"
        self.pending = self.dir / "pending"
        self.signals = self.dir / "signals"
        self.nodes = self.dir / "nodes"
        self.tick_lock = self.dir / "tick.lock"
        self.driver_file = self.dir / "driver.json"
        self.report_md = self.dir / "report.md"
        self.report_html = self.dir / "report.html"

    def attempt_dir(self, node_id: str, attempt: int) -> Path:
        return self.nodes / fs_name(node_id) / f"a{attempt}"

    def exists(self) -> bool:
        return self.events.exists()


def runs_dir(root: Path) -> Path:
    return Path(root) / STATE_DIR / "runs"


def list_runs(root: Path) -> list[str]:
    d = runs_dir(root)
    if not d.is_dir():
        return []
    runs = [p for p in d.iterdir() if (p / "events.jsonl").exists()]
    runs.sort(key=lambda p: (p / "events.jsonl").stat().st_ctime)
    return [p.name for p in runs]


def resolve_run(root: Path, ref: str | None) -> RunPaths:
    runs = list_runs(root)
    if not runs:
        raise FlowerError("no_runs", f"no runs under {runs_dir(root)}", "Start one with `flower run plan.yaml`.")
    if ref in (None, "", "last", "latest", "@"):
        return RunPaths(root, runs[-1])
    if ref in runs:
        return RunPaths(root, ref)
    matches = [r for r in runs if r.startswith(ref) or ref in r]
    if len(matches) == 1:
        return RunPaths(root, matches[0])
    if not matches:
        raise FlowerError("run_not_found", f"no run matches {ref!r}", "List runs with `flower ls`.")
    raise FlowerError("run_ambiguous", f"{ref!r} matches {len(matches)} runs: {', '.join(matches[:5])}",
                         "Use a longer prefix.")
