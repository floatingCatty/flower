"""Plain helpers for the core suite (kept out of conftest.py so tests can import them by a unique name)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FLOWER_EXE = REPO / ".venv" / "bin" / "flower"


TERMINAL = ("succeeded", "failed", "cancelled", "rejected")


def drive(eng, timeout=30.0, until="settled", interval=0.05):
    """Same stopping rule as Engine.drive, but with a short fixed poll interval (keeps the suite fast).

    Engine.drive itself is exercised directly in a few tests."""
    deadline = time.time() + timeout
    while True:
        rep = eng.tick()
        if rep.status in TERMINAL:
            return rep
        if until == "settled" and not rep.busy and rep.status in ("parked", "awaiting_approval"):
            if not (rep.status == "parked" and rep.next_poll_s):
                return rep
        if time.time() > deadline:
            return rep
        time.sleep(interval)


def tick_until(eng, pred, timeout=20.0, interval=0.1):
    """Tick until pred(state) is true; return the state (fails the test on timeout)."""
    deadline = time.time() + timeout
    while True:
        eng.tick()
        st = eng.state()
        if pred(st):
            return st
        if time.time() > deadline:
            raise AssertionError(f"condition not reached in {timeout}s; status={st.status} "
                                 f"nodes={ {k: v.status for k, v in st.nodes.items()} }")
        time.sleep(interval)


def wait_for(pred, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def events(eng, etype=None, node=None):
    evs = eng.journal.read()
    return [e for e in evs if (etype is None or e["eventType"] == etype) and (node is None or e.get("nodeId") == node)]


def out_json(obj) -> str:
    """Shell snippet writing ``obj`` as JSON to $FLOWER_OUTPUTS."""
    return f"cat > \"$FLOWER_OUTPUTS\" <<'FFEOF'\n{json.dumps(obj)}\nFFEOF"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except OSError:
        return False
    return True
