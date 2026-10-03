"""Crash resilience: driver death, runner/child death (lost), crash windows around the write-ahead
`node.started`, torn journal tails, concurrent tickers, and journal-only reconstruction."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections import Counter

import pytest

from core_helpers import drive, events, out_json, pid_alive, tick_until, wait_for
from flower.engine import Engine
from flower.state import apply, fold


def sh(id_, run, **kw):
    return {"id": id_, "kind": "shell", "run": run, **kw}


def runner_json(eng, node, attempt=1):
    p = eng.paths.attempt_dir(node, attempt) / "proc" / "runner.json"
    assert wait_for(p.exists, 10)
    return json.loads(p.read_text())


DRIVER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from flower.engine import create_run
    plan = json.loads(Path(sys.argv[1]).read_text())
    eng = create_run(plan, {}, root=Path(sys.argv[2]), approve=True)
    Path(sys.argv[3]).write_text(eng.paths.run_id)
    eng.drive(timeout=120, max_sleep=0.2)
""")


def test_driver_sigkill_node_survives_and_is_collected(mkplan, home, tmp_path):
    marker = tmp_path / "side_effect.log"
    plan = mkplan([sh("a", f'echo run >> "{marker}"; sleep 1.5; ' + out_json({"v": 1})),
                   sh("b", 'echo "{\\"b\\": ${a.outputs.v}}" > "$FLOWER_OUTPUTS"')])
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "driver.py").write_text(DRIVER)
    rid_file = tmp_path / "rid"
    drv = subprocess.Popen([sys.executable, str(tmp_path / "driver.py"), str(tmp_path / "plan.json"), str(home),
                            str(rid_file)], start_new_session=True)
    try:
        assert wait_for(rid_file.exists, 15)
        eng = Engine.open(home, rid_file.read_text())
        info = runner_json(eng, "a")
        os.killpg(drv.pid, signal.SIGKILL)       # the driver dies hard mid-run
        drv.wait(5)
        assert pid_alive(info["runner_pid"]) and pid_alive(info["child_pid"])
        assert eng.state().nodes["a"].status == "running"
        # nothing drives the run for a while; the node still finishes on its own
        assert wait_for(lambda: (eng.paths.attempt_dir("a", 1) / "proc" / "exit.json").exists(), 10)
        rep = drive(eng)                          # a later tick (any process) collects and continues
        assert rep.status == "succeeded"
        st = eng.state()
        assert st.nodes["b"].result.outputs == {"b": 1}
        assert len(st.nodes["a"].attempts) == 1
        assert marker.read_text().count("run") == 1   # no duplicated side effect
    finally:
        if drv.poll() is None:
            drv.kill()


def _kill_runner_and_child(info):
    os.kill(info["runner_pid"], signal.SIGKILL)
    try:
        os.killpg(info["child_pgid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    assert wait_for(lambda: not pid_alive(info["runner_pid"]) and not pid_alive(info["child_pid"]), 5)


def test_runner_and_child_killed_is_lost_and_retried(mkplan, start):
    eng = start(mkplan([sh("a", 'if [ "$FLOWER_ATTEMPT" = 1 ]; then sleep 30; fi; ' + out_json({"ok": True}),
                           retry={"max_attempts": 2, "backoff": "0.1s"})]))
    tick_until(eng, lambda s: s.nodes["a"].status == "running")
    _kill_runner_and_child(runner_json(eng, "a"))
    rep = drive(eng, timeout=15)
    assert rep.status == "succeeded"
    ns = eng.state().nodes["a"]
    assert [a.status for a in ns.attempts] == ["failed", "succeeded"]
    err = ns.attempts[0].error
    assert err["error_class"] == "lost" and err["retryable"] is True
    assert "exit.json" in err["message"]


def test_lost_without_retry_fails(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 30")]))
    tick_until(eng, lambda s: s.nodes["a"].status == "running")
    _kill_runner_and_child(runner_json(eng, "a"))
    assert drive(eng, timeout=10).status == "failed"
    assert eng.state().nodes["a"].last.error["error_class"] == "lost"


def test_runner_killed_child_survives_keeps_running_then_lost(mkplan, start):
    """The runner is gone but the child finishes: the sh wrapper's child_rc recovers the verdict, so the
    work is not repeated (fixed: previously reported as `lost`)."""
    eng = start(mkplan([sh("a", "sleep 1.2; " + out_json({"v": 1}))]))
    tick_until(eng, lambda s: s.nodes["a"].status == "running")
    info = runner_json(eng, "a")
    os.kill(info["runner_pid"], signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(info["runner_pid"]), 5)
    eng.tick()
    assert eng.state().nodes["a"].status == "running"       # child still alive -> still running
    assert wait_for(lambda: not pid_alive(info["child_pid"]), 10)
    drive(eng, timeout=10)
    a = eng.state().nodes["a"].last
    assert a.status == "succeeded" and a.outputs == {"v": 1}


def test_crash_after_write_ahead_before_launch(mkplan, start, monkeypatch, tmp_path):
    from flower.executors import local
    marker = tmp_path / "ran.log"
    eng = start(mkplan([sh("a", f'echo x >> "{marker}"; ' + out_json({"v": 1}),
                           retry={"max_attempts": 2, "backoff": "0.1s"})]))

    def die(self, ctx):
        raise KeyboardInterrupt("simulated kill -9 between node.started and launch")

    monkeypatch.setattr(local.ShellExecutor, "start", die)
    with pytest.raises(KeyboardInterrupt):
        eng.tick()
    st = eng.state()
    assert st.nodes["a"].status == "running" and st.nodes["a"].last.n == 1
    monkeypatch.undo()
    rep = drive(Engine(eng.paths), timeout=15)
    assert rep.status == "succeeded"
    ns = eng.state().nodes["a"]
    assert ns.attempts[0].error["error_class"] == "lost"
    assert ns.result.n == 2
    assert marker.read_text().count("x") == 1


def test_crash_after_launch_before_handle_journalled(mkplan, start, monkeypatch, tmp_path):
    marker = tmp_path / "ran.log"
    eng = start(mkplan([sh("a", f'echo x >> "{marker}"; sleep 0.3; ' + out_json({"v": 1}))]))
    real_emit = Engine.emit

    def flaky_emit(self, etype, payload=None, **kw):
        if etype == "node.progress" and (payload or {}).get("phase") == "launched":
            raise KeyboardInterrupt("simulated crash right after spawning the runner")
        return real_emit(self, etype, payload, **kw)

    monkeypatch.setattr(Engine, "emit", flaky_emit)
    with pytest.raises(KeyboardInterrupt):
        eng.tick()
    monkeypatch.undo()
    rep = drive(Engine(eng.paths), timeout=15)
    assert rep.status == "succeeded"
    assert len(eng.state().nodes["a"].attempts) == 1
    assert marker.read_text().count("x") == 1          # re-attached, not relaunched


def test_torn_journal_tail_mid_run_is_repaired(mkplan, start):
    eng = start(mkplan([sh("a", "sleep 0.5"), sh("b", "true", needs=["a"])]))
    tick_until(eng, lambda s: s.nodes["a"].status == "running")
    with open(eng.paths.events, "ab") as fh:
        fh.write(b'{"seq": 99999, "eventType": "node.succ')
    assert drive(eng).status == "succeeded"
    evs = eng.journal.read()
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))


def _ticker(home: str, rid: str, secs: float) -> None:
    os.environ["FLOWER_HOME"] = home
    eng = Engine.open(__import__("pathlib").Path(home), rid)
    end = time.time() + secs
    while time.time() < end:
        eng.tick()
        time.sleep(0.01)


def test_concurrent_tickers_never_double_start(mkplan, start, home):
    eng = start(mkplan([sh(f"n{i}", "sleep 0.2; " + out_json({"i": i})) for i in range(4)]
                       + [sh("join", "true", needs=[f"n{i}" for i in range(4)])]))
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_ticker, args=(str(home), eng.paths.run_id, 2.0)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    rep = drive(eng)
    assert rep.status == "succeeded"
    starts = Counter((e["nodeId"], e["payload"]["attempt"]) for e in events(eng, "node.started"))
    assert all(c == 1 for c in starts.values()), starts
    assert len(events(eng, "run.completed")) == 1
    assert sum(1 for e in events(eng, "node.succeeded")) == 5


# ------------------------------------------------------------------ reconstruction

def _rich_run(mkplan, start, tmp_path):
    cnt = tmp_path / "c"
    eng = start(mkplan([
        sh("a", f'C=$(cat {cnt} 2>/dev/null || echo 0); C=$((C+1)); echo $C > {cnt}; [ $C -ge 2 ]; '
                + out_json({"items": [1, 2]}), retry={"max_attempts": 2, "on": ["exit_nonzero"], "backoff": "0.1s"}),
        sh("each", 'echo "{\\"v\\": ${item}}" > "$FLOWER_OUTPUTS"', foreach="${a.outputs.items}"),
        {"id": "g", "kind": "gate", "needs": ["each"]},
        sh("z", "true", needs=["g"]),
    ]))
    drive(eng)
    eng.answer("g", "approve", text="fine")
    drive(eng)
    eng.note("all good", node="z")
    return eng


def _norm(st):
    import dataclasses
    return json.loads(json.dumps(dataclasses.asdict(st), default=str, sort_keys=True))


def test_fold_is_pure_and_incremental(mkplan, start, tmp_path):
    eng = _rich_run(mkplan, start, tmp_path)
    evs = eng.journal.read()
    s1, s2 = fold(evs), fold(json.loads(json.dumps(evs)))
    assert _norm(s1) == _norm(s2)
    for k in (1, len(evs) // 3, len(evs) // 2, len(evs) - 1):
        st = fold(evs[:k])
        for ev in evs[k:]:
            apply(st, ev)
        assert _norm(st) == _norm(s1)
    assert s1.status == "succeeded" and s1.generation == 1 and s1.notes[0]["text"] == "all good"


def test_journal_alone_reconstructs_status(mkplan, start, tmp_path, home, cli):
    eng = _rich_run(mkplan, start, tmp_path)
    code, out = cli("status", eng.paths.run_id, "--no-tick")
    assert code == 0 and out["ok"]
    data = out["data"]
    st = fold(eng.journal.read())
    assert data["status"] == st.status == "succeeded"
    assert {n["id"]: n["status"] for n in data["nodes"]} == {k: v.status for k, v in st.nodes.items()}
    assert data["generation"] == st.generation and data["digest"] == st.digest
    # delete every derived file: the journal alone is enough
    import shutil
    for p in eng.paths.dir.iterdir():
        if p.name not in ("events.jsonl",):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    code2, out2 = cli("status", eng.paths.run_id, "--no-tick")
    assert code2 == 0
    assert {n["id"]: n["status"] for n in out2["data"]["nodes"]} == {n["id"]: n["status"] for n in data["nodes"]}
    assert out2["data"]["status"] == "succeeded"
