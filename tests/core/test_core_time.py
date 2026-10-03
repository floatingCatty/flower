"""Times are stored in UTC and shown in local time (regression: the log showed UTC clock times unlabelled)."""
from __future__ import annotations

import pytest

from flower import util
from flower.render import timeline


@pytest.fixture
def zone(monkeypatch):
    def use(name: str):
        monkeypatch.setenv("TZ", name)
        monkeypatch.setattr(util, "_LOCAL_TZ", [])  # the zone is resolved once per process
        if util._local_tz() is None:
            pytest.skip("no zone database on this machine")
    return use


def test_local_clock_and_stamp_convert_from_utc(zone):
    zone("America/Toronto")
    assert util.local_clock("2026-10-03T14:30:30.615Z") == "10:30:30"
    assert util.local_stamp("2026-10-03T14:30:30.615Z") == "2026-10-03 10:30:30 EDT"
    assert util.local_stamp("2026-01-15T14:30:30Z") == "2026-01-15 09:30:30 EST"  # DST handled per timestamp
    zone("Asia/Shanghai")
    assert util.local_clock("2026-10-03T14:30:30.615Z") == "22:30:30"
    assert util.local_zone() == "CST (UTC+08:00)"


def test_timeline_is_local_and_says_which_zone(zone):
    zone("America/Toronto")
    evs = [{"eventType": "run.started", "occurredAtIso": "2026-10-03T14:29:23.000Z", "payload": {"by": "me"}},
           {"eventType": "node.started", "occurredAtIso": "2026-10-03T14:30:30.000Z", "nodeId": "scf",
            "payload": {"attempt": 2, "kind": "shell"}}]
    lines = timeline(evs).splitlines()
    assert lines[0].startswith("(times in E") and "UTC-0" in lines[0]
    assert lines[-1].startswith("10:30:30 ")
    assert not any(l.startswith("14:") for l in lines)
