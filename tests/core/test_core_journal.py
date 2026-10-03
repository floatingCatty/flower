"""Journal: contiguous seq, idempotency, torn-tail repair, fail-closed corruption, multi-process appends."""
from __future__ import annotations

import json
import multiprocessing as mp

import pytest

from flower.journal import Journal
from flower.util import FlowerError


def _first(j: Journal):
    return j.emit("run.created", {"plan_id": "p"}, runId="run-1")


def test_seq_contiguous_and_envelope(tmp_path):
    j = Journal(tmp_path / "events.jsonl")
    _first(j)
    for i in range(10):
        j.emit("run.note", {"text": str(i)})
    evs = j.read()
    assert [e["seq"] for e in evs] == list(range(1, 12))
    assert all(e["runId"] == "run-1" for e in evs)
    assert len({e["eventId"] for e in evs}) == 11
    for e in evs:
        assert {"schemaVersion", "seq", "eventId", "runId", "eventType", "occurredAtIso", "actor", "payload"} <= set(e)


def test_first_event_requires_run_id(tmp_path):
    j = Journal(tmp_path / "events.jsonl")
    with pytest.raises(FlowerError) as ei:
        j.emit("run.note", {"text": "x"})
    assert ei.value.code == "bad_event"


def test_unknown_event_type_rejected_and_nothing_written(tmp_path):
    j = Journal(tmp_path / "events.jsonl")
    _first(j)
    with pytest.raises(FlowerError) as ei:
        j.append([{"eventType": "run.note", "payload": {}}, {"eventType": "node.exploded", "payload": {}}])
    assert ei.value.code == "bad_event_type"
    # the batch is atomic: the valid first draft must not have been written either
    assert [e["eventType"] for e in j.read()] == ["run.created"]


def test_idempotency_key_returns_original(tmp_path):
    j = Journal(tmp_path / "events.jsonl")
    _first(j)
    a = j.emit("run.note", {"text": "first"}, idempotencyKey="k1")
    b = j.emit("run.note", {"text": "second"}, idempotencyKey="k1")
    assert a == b
    assert b["payload"]["text"] == "first"
    assert len(j.read()) == 2
    # duplicate keys inside one batch: second is dropped too
    out = j.append([{"eventType": "run.note", "payload": {"text": "x"}, "idempotencyKey": "k2"},
                    {"eventType": "run.note", "payload": {"text": "y"}, "idempotencyKey": "k2"}])
    assert out[0] == out[1]
    assert [e["seq"] for e in j.read()] == [1, 2, 3]


def test_idempotency_survives_new_journal_object(tmp_path):
    p = tmp_path / "events.jsonl"
    j = Journal(p)
    _first(j)
    a = j.emit("run.note", {"text": "a"}, idempotencyKey="same")
    b = Journal(p).emit("run.note", {"text": "b"}, idempotencyKey="same")
    assert a["eventId"] == b["eventId"]


def test_torn_final_line_ignored_on_read_and_repaired_on_append(tmp_path):
    p = tmp_path / "events.jsonl"
    j = Journal(p)
    _first(j)
    j.emit("run.note", {"text": "ok"})
    with open(p, "ab") as fh:
        fh.write(b'{"seq": 3, "eventType": "run.no')  # writer died mid-line
    assert len(Journal(p).read()) == 2
    ev = Journal(p).emit("run.note", {"text": "after"})
    assert ev["seq"] == 3
    lines = p.read_bytes().split(b"\n")
    assert lines[-1] == b""
    assert [json.loads(x)["seq"] for x in lines if x] == [1, 2, 3]


def test_complete_json_without_newline_is_treated_as_torn(tmp_path):
    p = tmp_path / "events.jsonl"
    j = Journal(p)
    _first(j)
    good = j.read()[0]
    fake = dict(good, seq=2, eventId="ev-torn", eventType="run.note")
    with open(p, "ab") as fh:
        fh.write(json.dumps(fake).encode())  # no trailing newline -> never acknowledged
    assert len(Journal(p).read()) == 1
    ev = Journal(p).emit("run.note", {"text": "real"})
    assert ev["seq"] == 2 and ev["eventId"] != "ev-torn"
    assert len(Journal(p).read()) == 2


def test_corrupt_middle_line_is_fatal(tmp_path):
    p = tmp_path / "events.jsonl"
    j = Journal(p)
    _first(j)
    j.emit("run.note", {"text": "a"})
    j.emit("run.note", {"text": "b"})
    lines = p.read_text().splitlines(keepends=True)
    lines[1] = "{this is not json\n"
    p.write_text("".join(lines))
    with pytest.raises(FlowerError) as ei:
        Journal(p).read()
    assert ei.value.code == "journal_corrupt"
    with pytest.raises(FlowerError) as ei2:
        Journal(p).emit("run.note", {"text": "c"})
    assert ei2.value.code == "journal_corrupt"
    # nothing was "repaired" away
    assert p.read_text().count("\n") == 3


def test_seq_gap_is_fatal(tmp_path):
    p = tmp_path / "events.jsonl"
    j = Journal(p)
    _first(j)
    j.emit("run.note", {"text": "a"})
    j.emit("run.note", {"text": "b"})
    lines = p.read_text().splitlines(keepends=True)
    del lines[1]
    p.write_text("".join(lines))
    with pytest.raises(FlowerError) as ei:
        Journal(p).read()
    assert ei.value.code == "journal_gap"


def test_blank_lines_tolerated(tmp_path):
    p = tmp_path / "events.jsonl"
    j = Journal(p)
    _first(j)
    with open(p, "a") as fh:
        fh.write("\n\n")
    j.emit("run.note", {"text": "a"})
    assert [e["seq"] for e in Journal(p).read()] == [1, 2]


def test_read_cache_invalidated_by_external_append(tmp_path):
    p = tmp_path / "events.jsonl"
    j1, j2 = Journal(p), Journal(p)
    _first(j1)
    assert len(j1.read()) == 1
    j2.emit("run.note", {"text": "from j2"})
    assert len(j1.read()) == 2
    # returned list is a copy: mutating it never corrupts the cache
    evs = j1.read()
    evs.clear()
    assert len(j1.read()) == 2


def _worker(path: str, wid: int, n: int) -> None:
    j = Journal(path)
    for i in range(n):
        j.emit("run.note", {"text": f"{wid}:{i}"}, idempotencyKey=f"w{wid}-{i}")
        # retry the same logical event: must dedupe
        if i % 5 == 0:
            j.emit("run.note", {"text": f"{wid}:{i}:retry"}, idempotencyKey=f"w{wid}-{i}")


def test_concurrent_appends_from_processes(tmp_path):
    p = tmp_path / "events.jsonl"
    _first(Journal(p))
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_worker, args=(str(p), w, 25)) for w in range(6)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(60)
        assert pr.exitcode == 0
    evs = Journal(p).read()
    assert [e["seq"] for e in evs] == list(range(1, 1 + 1 + 6 * 25))
    texts = [e["payload"]["text"] for e in evs[1:]]
    assert len(texts) == len(set(texts)) == 150
    assert not any(t.endswith(":retry") for t in texts)
    # per-writer order is preserved
    for w in range(6):
        mine = [int(t.split(":")[1]) for t in texts if t.startswith(f"{w}:")]
        assert mine == sorted(mine)
