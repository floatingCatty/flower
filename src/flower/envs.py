"""Environments: an agent explores a target and writes the recipe; flower freezes it and replays it.

Flower deliberately knows nothing about conda, containers or modules: *how* to install software is the
agent's job (and gets better as agents do). Flower only guarantees that the recipe that worked is
frozen (content-hashed, versioned) and replayed the same way, and that steps run with it activated.

A recipe is a directory ``envs/<name>/`` next to the plan (or in a parent directory)::

    setup.sh      installs into $FLOWER_ENV_PREFIX         (bash -e, cwd = the recipe directory)
    activate.sh   sourced before every step that uses it    (make the tools available, nothing else)
    check.sh      exit 0 iff the environment works          (runs after activate; print versions)
    env.yaml      optional notes: description, setup_timeout, check_timeout
    FROZEN.json   written by `flower env freeze`: hash of the three scripts, who, when, replays
    sessions/     `flower remote exec --env <name>` logs: how the recipe was found (provenance)
    history/      earlier frozen versions

On a target the recipe installs into ``$HOME/.flower/envs/<name>-<hash12>`` ($FLOWER_ENV_PREFIX, which
setup.sh creates) and its files are copied next to it, ``…/<name>-<hash12>.recipe`` ($FLOWER_ENV_DIR). A
changed recipe gets a new prefix: it never mutates one that results were produced with.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .util import FlowerError, atomic_write_json, now_iso, parse_duration, read_json

ENVS_DIR = "envs"
SCRIPTS = ("setup.sh", "activate.sh", "check.sh")
DEFAULT_CHECK_TIMEOUT = 600
DEFAULT_SETUP_TIMEOUT = "2h"

TEMPLATES = {
    "setup.sh": """# Install the environment into $FLOWER_ENV_PREFIX (it does not exist yet: create it).
# Runs with `bash -e` in a copy of this recipe directory ($FLOWER_ENV_DIR), so files you add here (lock
# files, patches) are available. Pin exact versions / digests so a replay gives the same result.
# Must not need a terminal (no prompts).
echo "write the install steps here" >&2; exit 1
""",
    "activate.sh": """# Sourced (bash) before every step that uses this environment. Make the tools available, e.g.
#   export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"
""",
    "check.sh": """# Exit 0 if and only if the environment works; print the versions you rely on (they are recorded).
# Runs after activate.sh. Keep it fast (a tiny real calculation is ideal).
echo "write the checks here" >&2; exit 1
""",
}


def find(name: str, start: str | os.PathLike | None) -> Path | None:
    """``envs/<name>`` in ``start`` or the nearest parent directory that has one."""
    cur = Path(start or os.getcwd()).resolve()
    for d in [cur, *cur.parents]:
        p = d / ENVS_DIR / name
        if p.is_dir():
            return p
    return None


BOOKKEEPING = ("FROZEN.json", "sessions", "history")


def recipe_files(d: Path) -> dict[str, str]:
    """Every file of the recipe (the scripts plus whatever the agent added: lock files, patches, env.yaml)
    -> sha256, excluding flower's own bookkeeping."""
    out = {}
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d)
        if p.is_file() and rel.parts[0] not in BOOKKEEPING and "__pycache__" not in rel.parts:
            out[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    for f in SCRIPTS:
        out.setdefault(f, "")
    return out


def recipe_hash(d: Path) -> str:
    """Content hash of the recipe (the frozen identity of the environment)."""
    h = hashlib.sha256()
    for f, sha in recipe_files(d).items():
        h.update(f.encode() + b"\0" + sha.encode() + b"\n")
    return "sha256:" + h.hexdigest()


def short(h: str) -> str:
    return h.split(":", 1)[-1][:12]


def status(d: Path) -> tuple[str, dict | None]:
    """('frozen'|'changed'|'draft', FROZEN.json): changed = the scripts were edited after the freeze."""
    fz = read_json(d / "FROZEN.json")
    if not fz:
        return "draft", None
    return ("frozen" if fz.get("hash") == recipe_hash(d) else "changed"), fz


def settings(d: Path) -> dict:
    p = d / "env.yaml"
    if not p.is_file():
        return {}
    from .plan import yaml_load
    data = yaml_load(p.read_text()) or {}
    return data if isinstance(data, dict) else {}


def prefix(name: str, h: str) -> str:
    """Install location on the target (shell syntax: $HOME is expanded there)."""
    return f"$HOME/.flower/envs/{name}-{short(h)}"


def node_id(name: str, cluster: str) -> str:
    return f"env-{name}-{cluster}".replace(".", "-")


def setup_script(name: str, h: str, *, recipe_dir: str, allow_install: bool, fresh: bool = False,
                 env_prefix: str | None = None, check_timeout: float | None = None) -> str:
    """The shell that makes an environment ready on a target: check; if that fails, setup then check.

    ``recipe_dir`` is where the recipe's files are on the target (relative to the cwd or absolute).
    Writes check.log / setup.log in the cwd and the outputs JSON to $FLOWER_OUTPUTS when set."""
    pfx = env_prefix or prefix(name, h)
    lines = [
        f'export FLOWER_ENV_PREFIX="{pfx}"',
        'export FLOWER_ENV_DIR="$FLOWER_ENV_PREFIX.recipe"',
        'mkdir -p "$FLOWER_ENV_DIR"',
        f'(cd "{recipe_dir}" && tar cf - --exclude=./sessions --exclude=./history --exclude=./FROZEN.json .) '
        '| (cd "$FLOWER_ENV_DIR" && tar xf -)',
        # check.sh is bounded on its own (env.yaml check_timeout, default 10 min): a check that hangs on a new
        # host (an MPI/OpenMP program started without its settings, say) must fail fast and say so
        f'CT={int(check_timeout or DEFAULT_CHECK_TIMEOUT)}',
        'if command -v timeout >/dev/null 2>&1; then TO="timeout -k 10 $CT"; else TO=""; fi',
        'chk() { ( cd "$FLOWER_ENV_DIR" && set +eu && source ./activate.sh && $TO bash ./check.sh ) > check.log 2>&1; '
        'r=$?; if [ $r -eq 124 ] || [ $r -eq 137 ]; then printf "check.sh did not finish within %ss (env.yaml '
        'check_timeout); a program it runs may hang on this host\\n" "$CT" >> check.log; fi; return $r; }',
        'how=""',
    ]
    if fresh:
        lines.append('if [ -e "$FLOWER_ENV_PREFIX" ]; then '
                     'echo "a fresh replay needs a prefix that does not exist yet: $FLOWER_ENV_PREFIX" >&2; exit 2; fi')
    else:
        lines.append('if chk; then how=present; fi')
    if allow_install:
        lines += [
            # one installer per prefix: runs (or clusters naming the same machine) that find it missing at the same
            # time wait for the first, then find it present (concurrent `conda create` into one prefix corrupts it)
            'if [ -z "$how" ]; then',
            '  mkdir -p "$(dirname "$FLOWER_ENV_PREFIX")"',
            '  if command -v flock >/dev/null 2>&1; then exec 9>"$FLOWER_ENV_PREFIX.lock"; flock 9; else',
            '    until mkdir "$FLOWER_ENV_PREFIX.lockdir" 2>/dev/null; do sleep 5; done',
            '    trap \'rmdir "$FLOWER_ENV_PREFIX.lockdir" 2>/dev/null\' EXIT; fi',
            '  if chk; then how=present; fi',
            'fi',
            'if [ -z "$how" ]; then',
            '  if ( cd "$FLOWER_ENV_DIR" && bash -e ./setup.sh ) > setup.log 2>&1; then :; else',
            '    echo "setup.sh failed; last lines:" >&2; tail -n 25 setup.log >&2; exit 3; fi',
            '  if chk; then how=installed; else',
            '    echo "check.sh failed after setup.sh; last lines:" >&2; tail -n 25 check.log >&2; exit 4; fi',
            'fi',
        ]
    else:
        lines += ['if [ -z "$how" ]; then tail -n 25 check.log >&2; echo "check.sh failed (output above) and '
                  'installing is not allowed on this cluster (install: never)" >&2; exit 5; fi']
    lines += [
        'tail -n 5 check.log; echo "environment ' + name + ' $how in $FLOWER_ENV_PREFIX"',
        'if [ -n "${FLOWER_OUTPUTS:-}" ]; then',
        f'  printf \'{{"env": "{name}", "recipe": "{h}", "prefix": "%s", "how": "%s"}}\\n\' '
        '"$FLOWER_ENV_PREFIX" "$how" > "$FLOWER_OUTPUTS"',
        'fi',
    ]
    return "\n".join(lines) + "\n"


def activation(name: str, h: str) -> list[str]:
    """Prelude lines a consumer step runs before its payload."""
    pfx = prefix(name, h)
    return [f'export FLOWER_ENV_PREFIX="{pfx}" FLOWER_ENV_DIR="{pfx}.recipe"',
            'set +u; source "$FLOWER_ENV_DIR/activate.sh"']


def _env_description(name: str, cluster: str, h: str, what: str | None, allow_install: bool) -> str:
    sw = f"the software environment {name}" + (f" ({what})" if what else "") + f", frozen recipe {short(h)}"
    if allow_install:
        return (f"Makes sure {sw} works on {cluster}: runs the recipe's check and, if that fails, installs it "
                f"(setup.sh) and checks again. Output `how` says whether it was already present or installed; "
                f"check.log has the versions the check printed.")
    return (f"Checks that {sw} works on {cluster} (nothing is installed there). check.log has the versions the "
            f"check printed.")


def env_node(name: str, cluster: str, d: Path, h: str, allow_install: bool) -> dict:
    """The generated step that makes ``name`` ready on ``cluster`` (shown in the plan the user approves)."""
    st = settings(d)
    return {
        "id": node_id(name, cluster), "kind": "shell", "cluster": cluster,
        "title": f"environment {name} on {cluster}" + ("" if allow_install else " (check only)"),
        "description": _env_description(name, cluster, h, st.get("description"), allow_install),
        "stage_in": [{"from": str(d), "to": "recipe"}],
        "run": setup_script(name, h, recipe_dir="recipe", allow_install=allow_install,
                            check_timeout=parse_duration(st.get("check_timeout")) if st.get("check_timeout") else None),
        "outputs": {"env": "string", "recipe": "string", "prefix": "string", "how": "string"},
        "files": {"check": "check.log"},
        "timeout": {"total": st.get("setup_timeout") or DEFAULT_SETUP_TIMEOUT},
        "cache": False,  # cheap, and must notice an environment deleted since the last run
        "generated": f"env:{name}",
    }


# ---------------------------------------------------------------------------- freezing

def new(d: Path) -> list[str]:
    d.mkdir(parents=True, exist_ok=True)
    made = []
    for f, text in TEMPLATES.items():
        if not (d / f).exists():
            (d / f).write_text(text)
            made.append(f)
    return made


def last_check(d: Path) -> dict | None:
    """The most recent logged exploration command that ran check.sh (None if there was none)."""
    best = None
    for f in sorted((d / "sessions").glob("*.jsonl")):
        for ln in f.read_text().splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if "check.sh" in str(e.get("cmd", "")) and (best is None or str(e.get("at")) >= str(best.get("at"))):
                best = e
    return best


def log_session(d: Path, entry: dict) -> Path:
    s = d / "sessions"
    s.mkdir(parents=True, exist_ok=True)
    p = s / f"{entry.get('session') or now_iso()[:10]}.jsonl"
    with open(p, "a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return p


def session_commands(d: Path) -> list[dict]:
    out = []
    for p in sorted((d / "sessions").glob("*.jsonl")):
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def draft_setup(d: Path) -> str | None:
    """A setup.sh drafted from the logged exploration: the successful, non-probe commands, in order."""
    cmds = [e for e in session_commands(d) if e.get("rc") == 0 and not e.get("probe")]
    if not cmds:
        return None
    body = [f"# drafted by `flower env freeze` from {len(cmds)} logged command(s); review and edit freely",
            'cd "$FLOWER_ENV_DIR"']
    for e in cmds:
        body.append(f"# {e.get('at')} on {e.get('cluster')}")
        body.append(e["cmd"])
    return "\n".join(body) + "\n"


def freeze(d: Path, by: str) -> dict:
    """Pin the recipe: hash the scripts, keep the previous version, record who/when."""
    drafted = False
    setup = d / "setup.sh"
    if not setup.is_file() or setup.read_text() == TEMPLATES["setup.sh"]:
        text = draft_setup(d)
        if text is None:
            raise FlowerError("env_incomplete", f"{setup} is missing or still the template, and no logged "
                              "commands to draft it from", "write setup.sh, or explore with "
                              f"`flower remote exec --env {d.name} …` first")
        setup.write_text(text)
        drafted = True
    for f in ("activate.sh", "check.sh"):
        p = d / f
        if not p.is_file() or p.read_text() == TEMPLATES[f]:
            raise FlowerError("env_incomplete", f"{p} is missing or still the template",
                              f"write {f} (see the comments in the template from `flower env new`)")
    h = recipe_hash(d)
    old = read_json(d / "FROZEN.json") or {}
    if old.get("hash") == h:
        return {**old, "unchanged": True}
    if old.get("hash"):  # its scripts were snapshotted into history/<hash>/ when it was frozen
        keep = d / "history" / short(old["hash"])
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copy2(d / "FROZEN.json", keep / "FROZEN.json")
    fz = {"name": d.name, "hash": h, "frozen_at": now_iso(), "by": by, "drafted_setup": drafted,
          "files": recipe_files(d),
          "previous": old.get("hash"), "sessions": sorted(p.name for p in (d / "sessions").glob("*.jsonl")),
          "replays": []}
    atomic_write_json(d / "FROZEN.json", fz)
    snap = d / "history" / short(h)
    snap.mkdir(parents=True, exist_ok=True)
    for f in recipe_files(d):
        (snap / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(d / f, snap / f)
    return fz


def record_replay(d: Path, entry: dict) -> None:
    fz = read_json(d / "FROZEN.json") or {}
    fz.setdefault("replays", []).append(entry)
    atomic_write_json(d / "FROZEN.json", fz)
