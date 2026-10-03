"""Every event type the code emits is in the journal's closed vocabulary (BUGS #36: two new driver events were
not, and the first driver to reload or report an error died on its own event)."""
from __future__ import annotations

import re
from pathlib import Path

from flower.journal import EVENT_TYPES

SRC = Path(__file__).resolve().parents[2] / "src" / "flower"


def test_every_emitted_event_type_is_registered():
    emitted = set()
    for p in SRC.rglob("*.py"):
        for m in re.finditer(r"""\bemit\(\s*["']([a-z_.]+)["']""", p.read_text()):
            emitted.add(m.group(1))
    assert emitted, "found no emit() calls: the pattern is stale"
    missing = sorted(emitted - EVENT_TYPES)
    assert not missing, f"emitted but not in journal.EVENT_TYPES: {missing}"
