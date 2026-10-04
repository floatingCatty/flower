"""Append-only event journal (``events.jsonl``) — the single source of truth of a run.

Design (see context/notes/DEV_PLAN.md D2/D3, borrowed from LabFlow's envelope, yak's journal-only state and
Smithers' producer idempotency):

* one JSON object per line, appended under an exclusive ``flock`` and fsync'd before returning;
* ``seq`` is contiguous per run and assigned by the writer holding the lock;
* ``idempotencyKey`` makes appends safe to retry: a duplicate key is dropped, the original returned;
* a torn final line (crash mid-write, never acknowledged) is repaired on the next append;
  a corrupt line anywhere else is fatal — we never guess at history.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .util import FlowerError, now_iso

SCHEMA_VERSION = 1

# Closed vocabulary. Anything else is a programming error (LabFlow emitted f-string types; we don't).
EVENT_TYPES = {
    # run lifecycle
    "run.created", "run.started", "run.parked", "run.reopened", "run.completed",
    "run.cancel_requested", "run.note", "driver.started", "driver.stopped", "driver.reloaded", "driver.error",
    # plan contract
    "plan.proposed", "plan.approved", "plan.rejected",
    "plan.amendment.proposed", "plan.amendment.approved", "plan.amendment.rejected",
    # nodes
    "node.started", "node.progress", "node.succeeded", "node.failed", "node.skipped",
    "node.cancelled", "node.stale", "node.retry_scheduled", "node.held", "node.released",
    # hpc job specifics
    "job.submit_intent", "job.staged", "job.submitted", "job.observed", "job.exited", "job.retrieved",
    "job.remote_error", "job.lost", "job.cancel_requested", "job.orphan_detected",
    # gates
    "gate.requested", "gate.answered",
}


class Journal:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._cache_key: tuple[int, int] | None = None
        self._cache: list[dict] = []

    # ------------------------------------------------------------ reading
    def read(self) -> list[dict]:
        """Return all committed events. A torn last line is ignored (it was never acknowledged)."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return []
        key = (st.st_size, st.st_mtime_ns)
        if key == self._cache_key:
            return list(self._cache)
        events, _ = self._parse()
        self._cache_key, self._cache = key, events
        return list(events)

    def _parse(self) -> tuple[list[dict], int | None]:
        """Parse the file; return (events, byte offset of a torn tail or None)."""
        events: list[dict] = []
        with open(self.path, "rb") as fh:
            data = fh.read()
        offset = 0
        lines = data.split(b"\n")
        for i, raw in enumerate(lines):
            is_last = i == len(lines) - 1
            if not raw.strip():
                offset += len(raw) + 1
                continue
            try:
                ev = json.loads(raw.decode("utf-8"))
                if is_last:  # complete JSON but missing newline -> still torn (writer died before \n)
                    raise ValueError("no trailing newline")
            except (ValueError, UnicodeDecodeError) as exc:
                if is_last:
                    return events, offset
                raise FlowerError(
                    "journal_corrupt",
                    f"{self.path}: line {i + 1} is not valid JSON ({exc}); refusing to guess at history",
                    "Inspect the file by hand; only the final line may be repaired automatically.",
                )
            expected = len(events) + 1
            if ev.get("seq") != expected:
                raise FlowerError(
                    "journal_gap",
                    f"{self.path}: line {i + 1} has seq {ev.get('seq')} but {expected} was expected",
                )
            events.append(ev)
            offset += len(raw) + 1
        return events, None

    # ------------------------------------------------------------ writing
    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)

    def append(self, drafts: Iterable[dict]) -> list[dict]:
        """Append event drafts atomically; return the committed events (existing ones for duplicates).

        A draft is ``{eventType, payload, nodeId?, attemptId?, actor?, planGeneration?, idempotencyKey?}``.
        """
        drafts = list(drafts)
        if not drafts:
            return []
        with self._locked():
            events: list[dict] = []
            if self.path.exists():
                events, torn_at = self._parse()
                if torn_at is not None:
                    with open(self.path, "r+b") as fh:
                        fh.truncate(torn_at)
                        fh.flush()
                        os.fsync(fh.fileno())
            by_key = {e["idempotencyKey"]: e for e in events if e.get("idempotencyKey")}
            run_id = events[0]["runId"] if events else None
            out: list[dict] = []
            new: list[dict] = []
            seq = len(events)
            for d in drafts:
                etype = d["eventType"]
                if etype not in EVENT_TYPES:
                    raise FlowerError("bad_event_type", f"unknown event type {etype!r}")
                key = d.get("idempotencyKey")
                if key and key in by_key:
                    out.append(by_key[key])
                    continue
                seq += 1
                ev = {
                    "schemaVersion": SCHEMA_VERSION,
                    "seq": seq,
                    "eventId": f"ev-{secrets.token_hex(6)}",
                    "runId": d.get("runId") or run_id,
                    "eventType": etype,
                    "occurredAtIso": d.get("occurredAtIso") or now_iso(),
                    "actor": d.get("actor") or "system",
                    "payload": d.get("payload") or {},
                }
                for opt in ("nodeId", "attemptId", "planGeneration", "idempotencyKey"):
                    if d.get(opt) is not None:
                        ev[opt] = d[opt]
                if ev["runId"] is None:
                    raise FlowerError("bad_event", "first event of a journal must carry runId")
                run_id = ev["runId"]
                if key:
                    by_key[key] = ev
                new.append(ev)
                out.append(ev)
            if new:
                blob = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True, default=str) + "\n" for e in new)
                with open(self.path, "ab") as fh:
                    fh.write(blob.encode("utf-8"))
                    fh.flush()
                    os.fsync(fh.fileno())
            self._cache_key = None
            return out

    def emit(self, event_type: str, payload: dict | None = None, **kw: Any) -> dict:
        return self.append([{"eventType": event_type, "payload": payload or {}, **kw}])[0]
