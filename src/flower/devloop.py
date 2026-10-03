"""Keeping an agent's work inside its run: draft plans (`flower start`), one-command steps (`flower add`),
project-level agent instructions (`flower init`) and the harness hook that notices work done beside the run.

The point (learned the hard way): agents drift outside a workflow tool when the early phase has no home and
when running a command by hand is cheaper than recording it. `start` gives the work a run from minute one;
`add` makes a recorded step cost one command; `init` ships the instructions with the project instead of
hoping a skill is installed in someone's session; the hook is the safety net.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path

import yaml

# ---------------------------------------------------------------------- draft plans

DRAFT_HEADER = """\
# A draft started with `flower start`: the run exists from the first minute and grows step by step.
# Each addition is a recorded amendment of the run (see `flower log RUN`).
#   add a step and run it:   flower add RUN ID -- <command>         (on a cluster: --cluster C --env E)
#   or edit this file, then: flower rerun RUN ID --follow
# New `inputs:` (with a default, or `-i NAME=VALUE`) and new `clusters:` are picked up the same way.
"""


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].rstrip("-") or "work")


def input_type(v) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "string"


def draft_plan_text(pid: str, goal: str, inputs: dict, edits: str = "unfinished") -> str:
    lines = [DRAFT_HEADER, "flower: 1", f"id: {pid}", f"title: {json.dumps(goal, ensure_ascii=False)}",
             "description: |", *("  " + ln for ln in goal.strip().splitlines()), "",
             "policies:", f"  edits: {edits}", ""]
    if inputs:
        lines.append("inputs:")
        for k, v in inputs.items():
            lines.append(f"  {k}: {{type: {input_type(v)}, required: true}}")
        lines.append("")
    lines += ["clusters: {}", "", "nodes: []", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------- one-command steps

class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_rep)


def node_from_args(a, command: list[str]) -> dict:
    cmd = command[1:] if command and command[0] == "--" else command
    if not cmd:
        from .util import FlowerError
        raise FlowerError("usage", "no command given", "flower add RUN ID [options] -- <command>")
    run = cmd[0] if len(cmd) == 1 else shlex.join(cmd)
    n: dict = {"id": a.id, "kind": "shell"}
    if a.title:
        n["title"] = a.title
    if a.needs:
        n["needs"] = list(a.needs)
    if a.cluster:
        n["cluster"] = a.cluster
    if a.environment:
        n["environment"] = a.environment
    if a.stage_in:
        n["stage_in"] = list(a.stage_in)
    if a.retrieve:
        n["retrieve"] = list(a.retrieve)
    if a.setenv:
        n["env"] = dict(x.split("=", 1) for x in a.setenv)
    if a.timeout_total:
        n["timeout"] = {"total": a.timeout_total}
    if a.outs:
        n["outputs"] = {o.split(":", 1)[0]: (o.split(":", 1)[1] if ":" in o else "any") for o in a.outs}
    if a.files:
        n["files"] = dict(x.split("=", 1) for x in a.files)
    n["run"] = run
    return n


def node_yaml(node: dict, indent: str) -> str:
    body = yaml.dump(node, Dumper=_Dumper, sort_keys=False, default_flow_style=False, allow_unicode=True,
                     width=1000).rstrip("\n").splitlines()
    out = [indent + "- " + body[0]] + [indent + "  " + ln for ln in body[1:]]
    return "\n".join(out) + "\n"


def insert_node(text: str, node: dict) -> str:
    """Add a node at the end of the `nodes:` list of a plan file, keeping the rest of the file as written."""
    lines = text.splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if re.match(r"^nodes:\s*(\[\s*\])?\s*(#.*)?$", ln)), None)
    if idx is None:
        return text.rstrip("\n") + "\n\nnodes:\n" + node_yaml(node, "  ")
    if "[" in lines[idx]:
        lines[idx] = "nodes:\n"
    indent = None
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if re.match(r"^[A-Za-z_]", lines[j]):
            end = j
            break
        m = re.match(r"^(\s*)- ", lines[j])
        if m and indent is None:
            indent = m.group(1)      # follow the file's own list indentation
    indent = "  " if indent is None else indent
    while end > idx + 1 and not lines[end - 1].strip():
        end -= 1
    block = node_yaml(node, indent)
    head = "".join(lines[:end])
    if not head.endswith("\n"):
        head += "\n"
    sep = "\n" if end > idx + 1 else ""
    tail = "".join(lines[end:])
    return head + sep + block + ("\n" + tail if tail else "")


# ---------------------------------------------------------------------- project instructions for agents

AGENTS_BEGIN = "<!-- flower:begin (managed by `flower init`; edit outside these markers) -->"
AGENTS_END = "<!-- flower:end -->"
AGENTS_BLOCK = f"""{AGENTS_BEGIN}
## Working in this project: flower

Multi-step or long work here is done **inside a flower run**, so it is recorded, resumable and visible in
the project's UI (`flower ui`). The full guide is in `.claude/skills/flower/SKILL.md`
(`.agents/skills/flower/SKILL.md`).

1. **Start the run first**, before exploring: `flower start "<goal>"` (prints RUN). Nothing is "too early".
2. **Every computation is a step**, including the first quick test:
   `flower add RUN ID -- <command>` (remote: `--cluster C --env E --stage-in FILE`). It writes the step into
   the plan file and runs it. To fix a step: edit its code or the plan file, `flower rerun RUN ID --follow`.
3. Explore a remote machine with `flower remote exec --plan P --cluster C [--env E] [--probe] -- <cmd>`
   (logged), not raw ssh.
4. Reading files, papers and results directly is fine; *running* things beside the run is not.

If `FLOWER_INSIDE_RUN` is set you are inside a step: do its task and never call flower.
{AGENTS_END}
"""


def write_agents_md(root: Path) -> Path:
    p = root / "AGENTS.md"
    text = p.read_text() if p.exists() else ""
    if AGENTS_BEGIN in text and AGENTS_END in text:
        a, rest = text.split(AGENTS_BEGIN, 1)
        _, b = rest.split(AGENTS_END, 1)
        text = a + AGENTS_BLOCK.rstrip("\n") + b
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + AGENTS_BLOCK
    p.write_text(text)
    return p


HOOK_MARK = "flower hook bash"


def install_hook(root: Path, python: str) -> Path:
    """Claude Code: a PostToolUse hook on Bash that reminds the agent when it ran compute beside an active run
    (non-blocking: `additionalContext`). Written to settings.local.json: it names this machine's interpreter."""
    p = root / ".claude" / "settings.local.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
    groups = cfg.setdefault("hooks", {}).setdefault("PostToolUse", [])
    cmd = f"{shlex.quote(python)} -m flower hook bash"
    for g in groups:
        for h in g.get("hooks") or []:
            if HOOK_MARK in str(h.get("command", "")).replace(" -m flower ", " flower "):
                h["command"] = cmd
                p.write_text(json.dumps(cfg, indent=2) + "\n")
                return p
    groups.append({"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]})
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    return p


# ---------------------------------------------------------------------- the reminder

_FLOWER_CMD = re.compile(r"(^|[\s;&|(=/])flower(\s|;|$)")          # flower itself, also via F=.../flower
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", re.S)   # file contents being written
_COMPUTE = re.compile(
    r"(^|[;&|(]\s*|&&\s*|\s)(?:\S*/)?(?:"
    r"python[0-9.]*\s+(?!-c\b|-m\s+(?:json|pytest|pip|venv)\b|-V\b|--version\b)\S"
    r"|mpirun\s|mpiexec\s|srun\s|sbatch\s|julia\s+\S|Rscript\s|ssh\s+\S+\s+\S)")
QUIET_S = 900   # at most one reminder per session per 15 minutes
RECENT_S = 12 * 3600


def _project_root(cwd: Path) -> Path | None:
    for d in [cwd, *cwd.parents]:
        if (d / ".flower" / "runs").is_dir():
            return d
    return None


def active_run(root: Path) -> tuple[str, float] | None:
    best = None
    for d in (root / ".flower" / "runs").iterdir():
        ev = d / "events.jsonl"
        if ev.is_file():
            t = ev.stat().st_mtime
            if best is None or t > best[1]:
                best = (d.name, t)
    if best and time.time() - best[1] < RECENT_S:
        return best
    return None


def reminder(command: str, cwd: Path, session: str | None) -> str | None:
    if os.environ.get("FLOWER_INSIDE_RUN") or not command.strip():
        return None
    command = _HEREDOC.sub("\n", command)
    if _FLOWER_CMD.search(command) or not _COMPUTE.search(command):
        return None
    root = _project_root(cwd)
    if root is None:
        return None
    act = active_run(root)
    if act is None:
        return None
    rid = act[0]
    state_p = root / ".flower" / "hook-state.json"
    try:
        st = json.loads(state_p.read_text()) if state_p.exists() else {}
    except (OSError, ValueError):
        st = {}
    key = session or "default"
    if time.time() - float(st.get(key, 0)) < QUIET_S:
        return None
    st[key] = time.time()
    try:
        state_p.write_text(json.dumps(st))
    except OSError:
        pass
    return (f"flower: run {rid} is active in this project, and that command ran beside it: it is not recorded "
            f"and not visible in the UI. If it is part of the work, make it a step instead: "
            f"`flower add {rid} <id> -- <command>` (remote: `--cluster C --env E --stage-in FILE`; remote "
            f"exploration: `flower remote exec`). Reading files or results directly is fine.")
