"""``shell`` steps: local work under the detached runner."""
from __future__ import annotations

import json

from ..util import atomic_write_json, atomic_write_text, first_line, parse_duration, tail_text
from .base import Executor, NodeCtx, Outcome, cancel_process, exit_failure, finish_contract, launch, poll_process


def _timeouts(node: dict) -> tuple[float | None, float | None]:
    t = node.get("timeout") or {}
    return parse_duration(t.get("total")), parse_duration(t.get("idle"))


def _read_outputs(ctx: NodeCtx) -> tuple[dict | None, str | None]:
    path = ctx.attempt_dir / "outputs.json"
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text() or "{}")
    except ValueError as exc:
        return None, f"$FLOWER_OUTPUTS is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "$FLOWER_OUTPUTS must contain a JSON object"
    return data, None


def _finish(ctx: NodeCtx, label: str) -> Outcome | None:
    state, info = poll_process(ctx.proc_dir)
    if state == "running":
        return None
    if state == "lost":
        return Outcome.fail("lost", f"{label} process was lost: {info.get('why')}")
    fail = exit_failure(info, ctx.proc_dir, label)
    if fail:
        out, _ = _read_outputs(ctx)
        if out:
            fail.outputs = out
        return fail
    outputs, err = _read_outputs(ctx)
    if err:
        return Outcome.fail("contract", err, retryable=False)
    summary = outputs.get("summary") if isinstance(outputs.get("summary"), str) else None
    declared = bool((ctx.node.get("outputs") or {}) if isinstance(ctx.node, dict) else False)
    if not summary and not (declared and outputs):   # declared outputs are the contract: show them, not stray stdout
        lines = [l for l in tail_text(ctx.proc_dir / "stdout.log", 2000).splitlines() if l.strip()]
        summary = lines[-1].strip() if lines else ""
    if not summary and outputs:
        shown = ", ".join(f"{k}={json.dumps(v, default=str)[:40]}" for k, v in list(outputs.items())[:3])
        summary = f"outputs: {shown}"
    o = Outcome(status="succeeded", outputs=outputs, summary=first_line(summary, 200),
                rationale=outputs.get("rationale") if isinstance(outputs.get("rationale"), str) else None)
    return finish_contract(o, ctx.node, ctx.workdir)


class ShellExecutor(Executor):
    kind = "shell"

    def start(self, ctx: NodeCtx) -> dict:
        script = ctx.attempt_dir / "script.sh"
        atomic_write_text(script, f"#!/usr/bin/env bash\n# flower node {ctx.node['id']} attempt {ctx.attempt}\n"
                                  f"set -euo pipefail\n{ctx.node['run']}\n")
        atomic_write_json(ctx.attempt_dir / "inputs.json", ctx.inputs)
        total, idle = _timeouts(ctx.node)
        return launch(ctx.proc_dir, ["bash", str(script)], env=ctx.base_env(), cwd=ctx.workdir,
                      timeout_total=total, timeout_idle=idle, unset_env=["BASH_ENV", "ENV"])

    def poll(self, ctx: NodeCtx) -> Outcome | None:
        return _finish(ctx, "shell script")

    def cancel(self, ctx: NodeCtx) -> None:
        cancel_process(ctx.proc_dir)
