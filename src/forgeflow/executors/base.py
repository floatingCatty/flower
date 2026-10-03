"""Executor protocol and the local-process plumbing shared by shell / function / agent nodes.

An executor is *start → poll → collect*, never "run and wait": ``start`` launches work and returns
a handle that is journalled; ``poll`` is called by every ``tick`` and returns ``None`` while the work
is still going or an :class:`Outcome` once it is done. Nothing holds a process open between ticks.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .._runner import proc_start_ticks
from ..util import atomic_write_json, atomic_write_text, hostname, read_json, seconds_since, sha256_file, tail_text

# Failure classes. Retryable-by-default ones are infrastructure problems, not the work itself.
RETRYABLE_DEFAULT = {"lost", "transient", "remote", "node_fail", "preempted", "quota_retry"}


@dataclass
class Outcome:
    status: str                       # succeeded | failed | cancelled
    outputs: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    summary: str | None = None
    rationale: str | None = None
    error_class: str | None = None
    message: str | None = None
    retryable: bool | None = None
    details: dict = field(default_factory=dict)
    amendment: dict | None = None     # agent-proposed plan amendment (validated by the engine)

    @classmethod
    def fail(cls, error_class: str, message: str, **kw: Any) -> "Outcome":
        return cls(status="failed", error_class=error_class, message=message, **kw)


@dataclass
class NodeCtx:
    run_id: str
    run_dir: Path
    node: dict                 # rendered node spec (templates resolved)
    attempt: int
    attempt_dir: Path
    workdir: Path
    inputs: dict
    plan: dict
    emit: Callable[..., Any]   # emit(event_type, payload) for progress events of this attempt
    handle: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    job: dict = field(default_factory=dict)
    session: str | None = None
    repairs: int = 0

    @property
    def proc_dir(self) -> Path:
        return self.attempt_dir / "proc"

    def base_env(self) -> dict:
        env = {
            "FORGEFLOW_INSIDE_RUN": "1",
            "FF_RUN_ID": self.run_id,
            "FF_RUN_DIR": str(self.run_dir),
            "FF_NODE_ID": self.node["id"],
            "FF_ATTEMPT": str(self.attempt),
            "FF_NODE_DIR": str(self.workdir),
            "FF_OUTPUTS": str(self.attempt_dir / "outputs.json"),
            "FF_INPUTS": str(self.attempt_dir / "inputs.json"),
            # anything a node does is attributed to the node, never to the human who launched the run
            "FORGEFLOW_ACTOR": f"agent:{self.run_id}/{self.node['id']}",
        }
        for k, v in (self.inputs or {}).items():
            if isinstance(v, (str, int, float, bool)) and k.replace("_", "").isalnum():
                env[f"FF_IN_{k.upper()}"] = str(v)
        for k, v in (self.node.get("env") or {}).items():
            env[str(k)] = v if isinstance(v, str) else json.dumps(v)
        return env


class Executor:
    kind = "base"

    def start(self, ctx: NodeCtx) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def poll(self, ctx: NodeCtx) -> Outcome | None:  # pragma: no cover - interface
        raise NotImplementedError

    def cancel(self, ctx: NodeCtx) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# ====================================================================== local processes

def launch(proc_dir: Path, argv: list[str], *, env: dict | None = None, cwd: str | Path | None = None,
           stdin_text: str | None = None, timeout_total: float | None = None, timeout_idle: float | None = None,
           unset_env: list[str] | None = None) -> dict:
    """Start ``argv`` under a detached forgeflow runner. Returns the handle to journal."""
    proc_dir = Path(proc_dir)
    proc_dir.mkdir(parents=True, exist_ok=True)
    spec: dict[str, Any] = {"argv": [str(a) for a in argv], "env": env or {}, "cwd": str(cwd) if cwd else None,
                            "timeout_total": timeout_total, "timeout_idle": timeout_idle,
                            "unset_env": unset_env or []}
    if stdin_text is not None:
        atomic_write_text(proc_dir / "stdin.txt", stdin_text)
        spec["stdin_file"] = str(proc_dir / "stdin.txt")
    atomic_write_json(proc_dir / "spec.json", spec)
    with open(proc_dir / "runner.log", "ab") as log:
        p = subprocess.Popen([sys.executable, "-m", "forgeflow._runner", str(proc_dir)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                             close_fds=True, cwd=str(proc_dir))
    return {"proc_dir": str(proc_dir), "runner_pid": p.pid, "host": hostname()}


def _alive(pid: int | None, start_ticks: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if start_ticks is not None:
        cur = proc_start_ticks(pid)
        if cur is not None and cur != start_ticks:
            return False  # pid was reused
    # zombie check
    try:
        with open(f"/proc/{pid}/stat") as fh:
            if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except OSError:
        pass
    return True


def poll_process(proc_dir: Path, start_grace: float = 60.0) -> tuple[str, dict]:
    """('running', {}) | ('exited', exit_info) | ('lost', {'why': …})."""
    proc_dir = Path(proc_dir)
    ex = read_json(proc_dir / "exit.json")
    if ex is not None:
        return "exited", ex
    info = read_json(proc_dir / "runner.json")
    if info is None:
        age = seconds_since(_mtime_iso(proc_dir / "spec.json"))
        if age is not None and age < start_grace:
            return "running", {}
        return "lost", {"why": "runner never started"}
    if info.get("host") == hostname():
        if _alive(info.get("runner_pid"), info.get("runner_start")):
            return "running", info
        ex = read_json(proc_dir / "exit.json")  # it may have just finished
        if ex is not None:
            return "exited", ex
        if _alive(info.get("child_pid"), info.get("child_start")):
            return "running", info  # runner died but the work goes on; we can still collect logs
        rc = _child_rc(proc_dir)
        if rc is not None:
            return "exited", {"returncode": rc, "signal": None, "timed_out": False, "idle_timeout": False,
                              "cancelled": (proc_dir / "cancel").exists(), "recovered": "runner died; child_rc used"}
        return "lost", {"why": "runner and child died without writing exit.json (killed or machine reboot)"}
    hb = proc_dir / "heartbeat"
    age = seconds_since(_mtime_iso(hb)) if hb.exists() else seconds_since(info.get("started_at"))
    if age is not None and age < 90:
        return "running", info
    rc = _child_rc(proc_dir)
    if rc is not None:
        return "exited", {"returncode": rc, "signal": None, "timed_out": False, "idle_timeout": False,
                          "cancelled": False, "recovered": "runner on another host died; child_rc used"}
    return "lost", {"why": f"no heartbeat from host {info.get('host')} for {int(age or 0)}s"}


def _child_rc(proc_dir: Path) -> int | None:
    try:
        return int((Path(proc_dir) / "child_rc").read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _mtime_iso(path: Path) -> str | None:
    import datetime as dt
    try:
        ts = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def cancel_process(proc_dir: Path) -> None:
    proc_dir = Path(proc_dir)
    try:
        (proc_dir / "cancel").write_text("cancel\n")
    except OSError:
        pass
    info = read_json(proc_dir / "runner.json") or {}
    if info.get("host") != hostname():
        return
    if _alive(info.get("runner_pid"), info.get("runner_start")):
        try:
            os.kill(info["runner_pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif _alive(info.get("child_pid"), info.get("child_start")):
        try:
            os.killpg(info["child_pgid"], signal.SIGTERM)
        except ProcessLookupError:
            pass


def exit_failure(ex: dict, proc_dir: Path, label: str = "process") -> Outcome | None:
    """Map a runner exit record to a failure Outcome, or None if the process exited 0."""
    err_tail = tail_text(Path(proc_dir) / "stderr.log", 1500).strip()
    if ex.get("cancelled"):
        return Outcome(status="cancelled", error_class="cancelled", message="cancelled by request")
    if ex.get("spawn_error"):
        return Outcome.fail("spawn", f"could not start {label}: {ex['spawn_error']}", retryable=False)
    if ex.get("timed_out"):
        return Outcome.fail("timeout", f"{label} exceeded its total timeout and was killed", details={"stderr": err_tail})
    if ex.get("idle_timeout"):
        return Outcome.fail("idle_timeout", f"{label} produced no output for too long and was killed",
                            details={"stderr": err_tail})
    if ex.get("signal"):
        return Outcome.fail("killed", f"{label} was killed by signal {ex['signal']}", details={"stderr": err_tail})
    if ex.get("returncode") not in (0, None):
        msg = f"{label} exited with code {ex['returncode']}"
        if err_tail:
            msg += ": " + err_tail.splitlines()[-1][:300]
        return Outcome.fail("exit_nonzero", msg, details={"returncode": ex["returncode"], "stderr": err_tail})
    return None


# ====================================================================== output contract

_JSON_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean",
               "object": "object", "array": "array", "path": "string"}


def outputs_schema(declared: dict) -> dict:
    props, required = {}, []
    for key, spec in (declared or {}).items():
        s: dict[str, Any] = {}
        if spec.get("type") in _JSON_TYPES:
            s["type"] = _JSON_TYPES[spec["type"]]
        if spec.get("description"):
            s["description"] = spec["description"]
        for extra in ("enum", "items", "properties", "minimum", "maximum"):
            if extra in spec:
                s[extra] = spec[extra]
        props[key] = s
        if spec.get("required", True):
            required.append(key)
    return {"type": "object", "properties": props, "required": required}


def check_outputs(outputs: Any, declared: dict) -> list[str]:
    if not isinstance(outputs, dict):
        return [f"outputs must be a JSON object, got {type(outputs).__name__}"]
    if not declared:
        return []
    v = jsonschema.Draft7Validator(outputs_schema(declared))
    errs = []
    for e in sorted(v.iter_errors(outputs), key=lambda e: list(e.path)):
        loc = ".".join(str(x) for x in e.path) or "(top)"
        errs.append(f"{loc}: {e.message}")
    return errs


def collect_files(declared: dict, workdir: Path) -> tuple[dict, list[str]]:
    """Hash declared output files ``{name: relative path}``; return (files, missing)."""
    files, missing = {}, []
    for name, rel in (declared or {}).items():
        p = (Path(workdir) / str(rel)).resolve()
        if p.is_file():
            files[name] = {"path": str(p), "sha256": sha256_file(p), "bytes": p.stat().st_size}
        elif p.is_dir():
            files[name] = {"path": str(p), "sha256": None, "bytes": None, "dir": True}
        else:
            missing.append(f"{name} ({rel})")
    return files, missing


def finish_contract(outcome: Outcome, node: dict, workdir: Path) -> Outcome:
    """Validate declared outputs and files; turn violations into a 'contract' failure."""
    if outcome.status != "succeeded":
        return outcome
    errs = check_outputs(outcome.outputs, node.get("outputs") or {})
    files, missing = collect_files(node.get("files") or {}, workdir)
    outcome.files.update(files)
    problems = errs + [f"missing declared file {m}" for m in missing]
    if problems:
        return Outcome.fail("contract", "result violates the node's declared outputs: " + "; ".join(problems[:6]),
                            outputs=outcome.outputs, files=outcome.files, usage=outcome.usage, retryable=False,
                            details={"problems": problems})
    return outcome
