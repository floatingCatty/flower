"""Small shared helpers: canonical hashing, atomic writes, time, durations, ids."""
from __future__ import annotations

import datetime as _dt
import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any


class FlowerError(Exception):
    """User-facing error with a stable machine code and an optional fix suggestion."""

    def __init__(self, code: str, message: str, suggestion: str | None = None, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.details = details

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.suggestion:
            out["suggestion"] = self.suggestion
        if self.details is not None:
            out["details"] = self.details
        return out


# ---------------------------------------------------------------- hashing

def _str_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _str_keys(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_str_keys(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 (RFC 8785-like for our value space)."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except TypeError:  # mixed key types (e.g. YAML 1.1 booleans) -> compare as strings
        return json.dumps(_str_keys(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return "sha256:" + sha256_text(canonical_json(value))


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_fingerprint(module: str, search_dirs: list[str], max_files: int = 2000) -> str | None:
    """Content digest of a function node's *local* code: the top-level package of ``module`` (every ``.py``
    under it) or the single ``<module>.py``, found in ``search_dirs`` in import order. ``None`` when it is not
    there (an installed library: version it through the environment instead) or the package is too large."""
    top = module.split(".", 1)[0]
    for d in search_dirs:
        pkg, mod = Path(d) / top, Path(d) / f"{top}.py"
        if mod.is_file() and not (pkg / "__init__.py").is_file():  # same precedence as Python's FileFinder
            return digest({mod.name: sha256_file(mod)})
        if pkg.is_dir():
            files = sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
            if len(files) > max_files:
                return None
            return digest({str(p.relative_to(d)): sha256_file(p) for p in files})
    return None


def short(d: str | None, n: int = 12) -> str:
    if not d:
        return ""
    return d.split(":", 1)[-1][:n]


# ---------------------------------------------------------------- files

_UMASK = os.umask(0)
os.umask(_UMASK)

def atomic_write_text(path: str | os.PathLike, text: str, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode if mode is not None else (0o666 & ~_UMASK))  # mkstemp creates 0600; honour umask
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike, value: Any, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", mode)


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def tail_text(path: str | os.PathLike, max_bytes: int = 4000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        nl = text.find("\n")
        text = "…" + (text[nl + 1:] if 0 <= nl < 200 else text)
    return text


# ---------------------------------------------------------------- time

def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    return (now() - parse_iso(iso)).total_seconds()


_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h|hr|d|day|days|w)?\s*$", re.I)
_UNIT = {None: 1, "ms": 0.001, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600,
         "d": 86400, "day": 86400, "days": 86400, "w": 604800}


def parse_duration(value: Any) -> float | None:
    """'90s', '30m', '2h', '7d', 45 -> seconds. Compound forms like '1h30m' are supported."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if ":" in text:  # HH:MM:SS (Slurm style)
        parts = [float(p) for p in text.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    total = 0.0
    pos = 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)", text):
        if text[pos:m.start()].strip():
            raise ValueError(f"bad duration: {value!r}")
        unit = (m.group(2) or "").lower() or None
        if unit not in _UNIT:
            raise ValueError(f"bad duration unit in {value!r}")
        total += float(m.group(1)) * _UNIT[unit]
        pos = m.end()
    if text[pos:].strip() or pos == 0:
        raise ValueError(f"bad duration: {value!r}")
    return total


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


# ---------------------------------------------------------------- identity

def new_run_id(plan_id: str) -> str:
    stamp = now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", plan_id.lower()).strip("-")[:24] or "run"
    return f"{slug}-{stamp}-{secrets.token_hex(2)}"


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def hostname() -> str:
    return socket.gethostname()


def username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - containers without passwd entry
        return os.environ.get("USER", "unknown")


def default_actor() -> str:
    return os.environ.get("FLOWER_ACTOR") or f"human:{username()}"


def truncate(text: str | None, n: int) -> str:
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def tail_str(text: str | None, n: int) -> str:
    """Keep the END of a long text (where an agent's final answer or error explanation lives)."""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= n else "…" + text[-(n - 1):]


def oneline(text: str | None, n: int = 300) -> str:
    """Untrusted free text (agent rationale, error messages) made safe for Markdown/table cells."""
    if not text:
        return ""
    s = " ⏎ ".join(l.strip() for l in str(text).strip().splitlines() if l.strip())
    s = s.replace("```", "ʼʼʼ").replace("|", "/")
    return truncate(s, n)


def first_line(text: str | None, n: int = 100) -> str:
    if not text:
        return ""
    for line in str(text).splitlines():
        if line.strip():
            return truncate(line.strip(), n)
    return ""
