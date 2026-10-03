"""``script`` harness: any executable that reads the prompt on stdin and prints an answer.

Uses: deterministic tests, bring-your-own agent CLIs, and "agent-shaped" steps that should not burn
LLM tokens. The answer is the process stdout; the final JSON object in it (if any) is the structured
result. Lines of the form ``{"forgeflow_action": "..."}`` are shown as live progress.
Repair turns re-run the command with the repair instructions appended to the prompt.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import AgentRequest, Harness, HarnessResult, Invocation, classify_error


class ScriptHarness(Harness):
    name = "script"
    native_schema = False
    can_resume = False

    def build(self, req: AgentRequest) -> Invocation:
        if not req.command:
            raise ValueError("harness `script` needs `command: [executable, args…]`")
        env = dict(req.env or {})
        if req.output_schema:
            env["FF_OUTPUT_SCHEMA"] = json.dumps(req.output_schema)
        if req.model:
            env["FF_MODEL"] = req.model
        return Invocation(argv=list(req.command) + list(req.extra_args or []), env=env, stdin_text=req.prompt)

    def _actions(self, proc_dir: Path) -> list[dict]:
        acts = []
        try:
            for line in (proc_dir / "stdout.log").read_text(errors="replace").splitlines():
                if line.startswith('{"forgeflow_action"'):
                    try:
                        acts.append({"kind": "action", "title": json.loads(line)["forgeflow_action"][:100]})
                    except (ValueError, KeyError):
                        pass
        except FileNotFoundError:
            pass
        return acts[-30:]

    def live(self, proc_dir: Path) -> dict:
        acts = self._actions(proc_dir)
        return {"last_action": acts[-1]["title"] if acts else None, "actions": len(acts)}

    def parse(self, proc_dir: Path, exit_info: dict) -> HarnessResult:
        try:
            out = (proc_dir / "stdout.log").read_text(errors="replace")
        except FileNotFoundError:
            out = ""
        lines = [l for l in out.splitlines() if not l.startswith('{"forgeflow_action"')]
        text = "\n".join(lines).strip()
        rc = exit_info.get("returncode")
        if rc not in (0, None):
            err = ""
            try:
                err = (proc_dir / "stderr.log").read_text(errors="replace")[-2000:]
            except FileNotFoundError:
                pass
            return HarnessResult(ok=False, text=text, error_class=classify_error(err) or "agent_error",
                                 message=(err.strip().splitlines() or [f"exit {rc}"])[-1][:300], actions=self._actions(proc_dir))
        return HarnessResult(ok=True, text=text, actions=self._actions(proc_dir), stop_reason="complete",
                             usage={"cost_usd": 0.0, "cost_source": "harness"})
