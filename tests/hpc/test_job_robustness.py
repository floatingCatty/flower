"""Crash safety, idempotent submit, sacct lag / MinJobAge, lost detection, remote errors, poll errors."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import flower.executors.job as jobmod
from flower.engine import Engine
from flower.executors.job import JobExecutor
from flower.hpc import slurm
from flower.hpc.transport import CmdResult

OUT = 'printf \'{"x": 1}\' > "$FLOWER_OUTPUTS"'
SSH_DOWN = 'echo "ssh: connect to host hpc port 22: Connection timed out" >&2; exit 255'
SLURMCTLD_DOWN = 'echo "slurm_load_jobs error: Unable to contact slurm controller (connect failure)" >&2; exit 1'


def _ts(ev) -> float:
    return dt.datetime.fromisoformat(ev["occurredAtIso"].replace("Z", "+00:00")).timestamp()


def _crash_after_sbatch(ff, monkeypatch, eng, *, leave_jobid=True):
    """First tick: really stage + sbatch, then 'crash' before job.submitted reaches the journal."""
    real = JobExecutor._submit

    def crashing(self, ctx, cluster, tr=None):
        tr = tr or jobmod.make_transport(cluster)
        self._stage(ctx, cluster, tr, ctx.job["job_dir"])
        r = tr.run(slurm.submit_command(ctx.job["job_dir"], ctx.job["submit_key"], ctx.job["fingerprint"],
                                        jobmod._cmds(cluster)))
        assert slurm.parse_submit(r.out) and slurm.parse_submit(r.out)[1] == "submitted", (r.out, r.err)
        if not leave_jobid:  # crash between `sbatch > jobid.tmp` and `mv jobid.tmp jobid`
            flower_dir = Path(ctx.job["job_dir"]) / ".flower"
            (flower_dir / "jobid").rename(flower_dir / "jobid.tmp")
        raise KeyboardInterrupt("simulated crash between sbatch and job.submitted")

    monkeypatch.setattr(JobExecutor, "_submit", crashing)
    try:
        with pytest.raises(KeyboardInterrupt):
            eng.tick()
    finally:
        monkeypatch.setattr(JobExecutor, "_submit", real)
    st = eng.state()
    types = [e["eventType"] for e in ff.events(eng, "job.")]
    assert types == ["job.submit_intent"], types
    assert len(ff.jobs()) == 1
    return st


# ====================================================================== idempotent submit / crash safety

def test_crash_after_sbatch_reattaches_via_jobid_file(ff, monkeypatch):
    eng = ff.run(ff.plan([ff.job("a", "sleep 1\n" + OUT)]))
    _crash_after_sbatch(ff, monkeypatch, eng)
    eng2 = Engine.open(ff.home, eng.paths.run_id)  # a different process picks the run up
    st = ff.drive(eng2, timeout=30)
    assert st.status == "succeeded", ff.why(eng2)
    assert len(ff.jobs()) == 1
    sub = ff.events(eng2, "job.submitted")
    assert len(sub) == 1 and sub[0]["payload"]["via"] == "existing"
    assert sub[0]["payload"]["job_id"] == ff.jobs()[0]["id"] == st.nodes["a"].result.outputs["job_id"]


def test_crash_before_jobid_written_reattaches_via_squeue_name(ff, monkeypatch):
    eng = ff.run(ff.plan([ff.job("a", "sleep 2\n" + OUT)]))
    _crash_after_sbatch(ff, monkeypatch, eng, leave_jobid=False)
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    assert len(ff.jobs()) == 1
    assert ff.events(eng, "job.submitted")[0]["payload"]["via"] == "existing"


def test_crash_before_jobid_written_reattaches_via_sacct_after_minjobage(ff, monkeypatch):
    monkeypatch.setenv("FAKESLURM_MINJOBAGE", "0")
    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    _crash_after_sbatch(ff, monkeypatch, eng, leave_jobid=False)
    jid = ff.jobs()[0]["id"]
    ff.wait_job_state(jid, "COMPLETED")
    time.sleep(0.1)  # left squeue (MinJobAge 0): only sacct --name can find it now
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    assert len(ff.jobs()) == 1
    assert ff.events(eng, "job.submitted")[0]["payload"]["job_id"] == jid


def test_crash_before_jobid_written_with_squeue_down_does_not_duplicate(ff, monkeypatch):
    eng = ff.run(ff.plan([ff.job("a", "sleep 3\n" + OUT)]))
    _crash_after_sbatch(ff, monkeypatch, eng, leave_jobid=False)
    ff.replace_cmd("squeue", 'echo "slurm_load_jobs error: Socket timed out on send/recv operation" >&2; exit 1')
    monkeypatch.setenv("FAKESLURM_SACCT_LAG", "1000")  # slurmdbd has not seen the job yet
    eng.tick()
    ff.restore_cmd("squeue")
    monkeypatch.setenv("FAKESLURM_SACCT_LAG", "0")
    assert len(ff.jobs()) == 1, f"duplicate submission: {[(j['id'], j['name']) for j in ff.jobs()]}"
    # (today the duplicate exits 97, flower tracks it and fails the node as exit_nonzero while the
    #  original job is still RUNNING, orphaned)
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)


def test_crash_during_staging_submits_exactly_once(ff, monkeypatch):
    real = JobExecutor._stage

    def crashing(self, ctx, cluster, tr, job_dir):
        raise KeyboardInterrupt("crash while staging")

    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    monkeypatch.setattr(JobExecutor, "_stage", crashing)
    with pytest.raises(KeyboardInterrupt):
        eng.tick()
    monkeypatch.setattr(JobExecutor, "_stage", real)
    assert ff.jobs() == []
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    assert len(ff.jobs()) == 1
    assert ff.events(eng, "job.submitted")[0]["payload"]["via"] == "submitted"


def test_crash_before_submit_intent_recovers(ff, monkeypatch):
    real = JobExecutor.start

    def crashing(self, ctx):
        raise KeyboardInterrupt("crash before submit_intent")

    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    monkeypatch.setattr(JobExecutor, "start", crashing)
    with pytest.raises(KeyboardInterrupt):
        eng.tick()
    monkeypatch.setattr(JobExecutor, "start", real)
    assert [e["eventType"] for e in ff.events(eng, "job.")] == []
    st = ff.drive(eng, timeout=30)  # must not raise; either resubmits or fails the attempt cleanly
    assert st.status in ("succeeded", "failed")
    assert len(ff.jobs()) <= 1


def test_crash_after_job_exited_collects_once(ff, monkeypatch):
    real = JobExecutor._collect
    calls = {"n": 0}

    def crashing(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt("crash while collecting")
        return real(self, *a, **kw)

    monkeypatch.setattr(JobExecutor, "_collect", crashing)
    eng = ff.run(ff.plan([ff.job("a", OUT, outputs={"x": "integer"})]))
    with pytest.raises(KeyboardInterrupt):
        ff.drive(eng, timeout=30)
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded"
    assert calls["n"] == 2
    assert len(ff.events(eng, "job.exited")) == 1  # idempotency key
    assert len(ff.events(eng, "node.succeeded")) == 1 and len(ff.jobs()) == 1


def test_duplicate_copy_of_attempt_exits_97(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 2\necho run >> runs.log\n" + OUT, outputs={"x": "integer"})]))
    ff.drive(eng, until=lambda st: st.nodes.get("a") and st.nodes["a"].last
             and st.nodes["a"].last.job.get("state") == "RUNNING", timeout=20)
    jd = Path(eng.state().nodes["a"].last.job["job_dir"])
    # an operator (or a buggy resubmit) runs the same attempt's script a second time
    r = subprocess.run([str(ff.bin / "sbatch"), "--parsable", f"--chdir={jd}", "job.sh"], capture_output=True,
                       text=True, cwd=jd, check=True)
    dup = r.stdout.strip()
    ff.wait_job_state(dup, "FAILED")
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    assert (jd / f"slurm-{dup}.err").read_text().strip().endswith("another job already owns this attempt; exiting")
    assert [j for j in ff.jobs() if j["id"] == dup][0]["exit_code"] == 97
    assert (jd / "runs.log").read_text().count("run") == 1   # payload ran exactly once
    assert (jd / ".flower" / "owner" / "id").read_text().strip() == st.nodes["a"].result.outputs["job_id"]


def test_rerun_makes_a_new_attempt_and_key(ff):
    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    assert ff.drive(eng).status == "succeeded"
    eng.rerun("a")
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded"
    names = [j["name"] for j in ff.jobs()]
    assert len(names) == 2 and names[0].endswith("-a1") and names[1].endswith("-a2")


# ====================================================================== sacct lag / MinJobAge / lost

@pytest.mark.parametrize("code,want", [(0, "succeeded"), (4, "exit_nonzero")])
def test_sacct_lag_and_minjobage_use_evidence(ff, monkeypatch, code, want):
    monkeypatch.setenv("FAKESLURM_MINJOBAGE", "0")
    monkeypatch.setenv("FAKESLURM_SACCT_LAG", "1000")
    eng = ff.run(ff.plan([ff.job("a", f"sleep 0.5\n{OUT}\nexit {code}")],
                         clusters={"c": ff.cluster(lost_after="0.1s")}))
    st = ff.drive(eng, timeout=30)
    assert ff.events(eng, "job.lost") == []
    if want == "succeeded":
        assert st.status == "succeeded", ff.why(eng)
    else:
        assert ff.error_class(st, "a") == "exit_nonzero"
    ex = ff.events(eng, "job.exited")[0]["payload"]
    assert ex["ec"] == code and ex["final"] is None
    assert ff.events(eng, "job.observed")[-1]["payload"]["source"] == "evidence"


def test_lost_after_grace_and_five_misses_then_retried(ff, monkeypatch):
    monkeypatch.setenv("FAKESLURM_MINJOBAGE", "0")
    monkeypatch.setenv("FAKESLURM_SACCT_LAG", "1000")
    ff.faults([{"match": "-a1$", "state": "NODE_FAIL", "after_s": 0.2, "times": 1}])  # dies without writing ec
    script = 'if [ "$FLOWER_ATTEMPT" = 1 ]; then sleep 5; fi\n' + OUT
    eng = ff.run(ff.plan([ff.job("a", script, retry={"max_attempts": 2, "backoff": "0s"})],
                         clusters={"c": ff.cluster(lost_after="3s")}))
    st = ff.drive(eng, timeout=40)
    assert st.status == "succeeded", ff.why(eng)
    lost = ff.events(eng, "job.lost")
    assert len(lost) == 1
    assert lost[0]["payload"]["misses"] >= 5
    assert "started but died" in lost[0]["payload"]["why"]
    end1 = ff.jobs()[0]["end"]
    assert _ts(lost[0]) - end1 >= 3.0, "declared lost before the lost_after grace expired"
    a1, a2 = st.nodes["a"].attempts
    assert a1.error["error_class"] == "lost" and a1.error["retryable"] is True
    assert a2.status == "succeeded" and len(ff.jobs()) == 2


def test_lost_requires_five_misses_even_with_tiny_grace(ff, monkeypatch, clock):
    monkeypatch.setenv("FAKESLURM_MINJOBAGE", "0")
    monkeypatch.setenv("FAKESLURM_SACCT_LAG", "1000")
    monkeypatch.setenv("FAKESLURM_PEND_S", "60")
    eng = ff.run(ff.plan([ff.job("a", OUT)], clusters={"c": ff.cluster(lost_after="0.01s")}))
    ff.drive(eng, until=lambda st: st.nodes.get("a") and st.nodes["a"].last
             and st.nodes["a"].last.job.get("state") == "QUEUED", timeout=10)
    subprocess.run([str(ff.bin / "scancel"), ff.jobs()[0]["id"]], check=True)  # vanishes, never started
    for i in range(1, 5):
        clock.advance(1.0)
        eng.tick()
        assert ff.events(eng, "job.lost") == [], f"lost after only {i} misses"
    clock.advance(1.0)
    eng.tick()
    lost = ff.events(eng, "job.lost")
    assert len(lost) == 1 and lost[0]["payload"]["misses"] == 5
    assert "vanished" in lost[0]["payload"]["why"]
    st = ff.drive(eng, timeout=10)
    assert st.status == "failed" and ff.error_class(st, "a") == "lost"


def test_lost_with_missing_job_dir(ff, monkeypatch, clock):
    monkeypatch.setenv("FAKESLURM_MINJOBAGE", "0")
    monkeypatch.setenv("FAKESLURM_SACCT_LAG", "1000")
    monkeypatch.setenv("FAKESLURM_PEND_S", "60")
    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    ff.drive(eng, until=lambda st: st.nodes.get("a") and st.nodes["a"].last
             and st.nodes["a"].last.job.get("state") == "QUEUED", timeout=10)
    subprocess.run([str(ff.bin / "scancel"), ff.jobs()[0]["id"]], check=True)
    shutil.rmtree(eng.state().nodes["a"].last.job["job_dir"])
    for _ in range(8):
        clock.advance(1.0)
        eng.tick()
    lost = ff.events(eng, "job.lost")
    assert len(lost) == 1 and "directory is gone" in lost[0]["payload"]["why"]


def test_unknown_streak_resets_when_job_reappears(ff, monkeypatch, clock):
    eng = ff.run(ff.plan([ff.job("a", "sleep 3\n" + OUT)], clusters={"c": ff.cluster(lost_after="0.01s")}))
    ff.drive(eng, until=lambda st: st.nodes.get("a") and st.nodes["a"].last
             and st.nodes["a"].last.job.get("state") == "RUNNING", timeout=10)
    # squeue and sacct "forget" the job for 4 polls (fewer than 5), then it is visible again
    ff.replace_cmd("squeue", "exit 0")
    ff.replace_cmd("sacct", "exit 0")
    for _ in range(4):
        clock.advance(1.0)
        eng.tick()
    ff.restore_cmd("squeue")
    ff.restore_cmd("sacct")
    clock.advance(1.0)
    eng.tick()
    adir = eng.paths.attempt_dir("a", 1)
    assert not (adir / "job_poll.json").exists()
    ff.replace_cmd("squeue", "exit 0")
    ff.replace_cmd("sacct", "exit 0")
    for _ in range(3):
        clock.advance(1.0)
        eng.tick()
    ff.restore_cmd("squeue")
    ff.restore_cmd("sacct")
    assert ff.events(eng, "job.lost") == []
    st = ff.drive(eng, timeout=20)
    assert st.status == "succeeded"


# ====================================================================== transient remote errors (submit)

def test_submit_remote_errors_back_off_then_fail_remote(ff, clock):
    ff.replace_cmd("sbatch", SSH_DOWN)
    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    eng.tick()
    errs = ff.events(eng, "job.remote_error")
    assert len(errs) == 1 and errs[0]["payload"]["op"] == "submit" and errs[0]["payload"]["transient"] is True
    deltas = [errs[0]["payload"]["retry_at"] - clock.now()]
    # nothing happens before retry_at
    for _ in range(5):
        eng.tick()
    assert len(ff.events(eng, "job.remote_error")) == 1
    for n in range(2, jobmod.MAX_REMOTE_ERRORS + 1):
        clock.advance(deltas[-1] + 1)
        eng.tick()
        errs = ff.events(eng, "job.remote_error")
        assert len(errs) == n
        deltas.append(errs[-1]["payload"]["retry_at"] - clock.now())
        assert eng.state().nodes["a"].status == "running"
    want = [min(1200, 30 * 2 ** i) for i in range(jobmod.MAX_REMOTE_ERRORS)]
    assert [round(d) for d in deltas] == pytest.approx(want, abs=2)
    clock.advance(deltas[-1] + 1)
    st = ff.drive(eng, timeout=10)
    assert st.status == "failed"
    err = st.nodes["a"].last.error
    assert err["error_class"] == "remote" and "Connection timed out" in err["message"]
    assert ff.jobs() == [] and ff.events(eng, "job.submitted") == []


def test_submit_remote_error_then_recovers_without_duplicate(ff, clock):
    ff.replace_cmd("sbatch", SSH_DOWN)
    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    eng.tick()
    clock.advance(31)
    eng.tick()
    assert len(ff.events(eng, "job.remote_error")) == 2
    ff.restore_cmd("sbatch")
    clock.advance(61)
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    assert len(ff.jobs()) == 1


def test_connection_drop_after_sbatch_reattaches(ff, clock):
    # sbatch really queues the job, then the connection dies before the id is recorded anywhere
    ff.replace_cmd("sbatch", '"$REAL" "$@"; echo "Connection reset by peer" >&2; exit 255')
    eng = ff.run(ff.plan([ff.job("a", "sleep 1\n" + OUT)]))
    eng.tick()
    assert len(ff.events(eng, "job.remote_error")) == 1 and len(ff.jobs()) == 1
    ff.restore_cmd("sbatch")
    clock.advance(31)
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    assert len(ff.jobs()) == 1
    assert ff.events(eng, "job.submitted")[0]["payload"]["via"] == "existing"


def test_permanent_sbatch_rejection_fails_fast(ff):
    ff.replace_cmd("sbatch", 'echo "sbatch: error: Batch job submission failed: Invalid partition name specified" '
                             '>&2; exit 1')
    eng = ff.run(ff.plan([ff.job("a", OUT)]))
    for _ in range(3):
        eng.tick()
    errs = ff.events(eng, "job.remote_error")
    assert errs and errs[0]["payload"]["transient"] is False
    st = eng.state()
    assert st.nodes["a"].status == "failed", "permanent submit error left the node waiting for a backoff retry"
    assert "Invalid partition" in st.nodes["a"].last.error["message"]


def test_exception_during_resubmit_does_not_crash_tick(ff, clock, tmp_path):
    src = tmp_path / "input.dat"
    src.write_text("x")
    ff.replace_cmd("sbatch", SSH_DOWN)
    eng = ff.run(ff.plan([ff.job("a", OUT, stage_in=[str(src)])]))
    eng.tick()
    assert len(ff.events(eng, "job.remote_error")) == 1
    src.unlink()
    ff.restore_cmd("sbatch")
    clock.advance(31)
    eng.tick()  # must not raise
    st = ff.drive(eng, timeout=10)
    assert st.nodes["a"].status == "failed"
    assert ff.error_class(st, "a") == "stage_in"


def test_relative_stage_in_resolves_against_plan_dir(ff, monkeypatch, tmp_path):
    plan_dir = tmp_path / "project"
    plan_dir.mkdir()
    (plan_dir / "POSCAR").write_text("STRUCT")
    monkeypatch.chdir(plan_dir)
    eng = ff.run(ff.plan([ff.job("a", "cat POSCAR\n" + OUT, stage_in=["POSCAR"])]))
    monkeypatch.chdir(tmp_path)  # e.g. `flower tick --all` from cron
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)


def test_relative_stage_in_from_plan_dir_cwd(ff, monkeypatch, tmp_path):
    plan_dir = tmp_path / "project"
    plan_dir.mkdir()
    (plan_dir / "POSCAR").write_text("STRUCT")
    monkeypatch.chdir(plan_dir)
    eng = ff.run(ff.plan([ff.job("a", "cat POSCAR\n" + OUT, stage_in=["POSCAR"])]))
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)


# ====================================================================== poll errors never change verdicts

class _Flaky:
    def __init__(self, inner, state):
        self.inner, self.state, self.is_local = inner, state, inner.is_local

    def run(self, script, timeout=120.0):
        if self.state["down"] and "@@SQUEUE" in script:
            self.state["failed_polls"] += 1
            return CmdResult(255, "", "ssh: connect to host hpc port 22: Connection timed out")
        return self.inner.run(script, timeout)

    def put(self, *a, **kw):
        return self.inner.put(*a, **kw)

    def get(self, *a, **kw):
        return self.inner.get(*a, **kw)


def _flaky_transport(monkeypatch):
    state = {"down": False, "failed_polls": 0}
    real = jobmod.make_transport
    monkeypatch.setattr(jobmod, "make_transport", lambda c: _Flaky(real(c), state))
    return state


def _ticks(eng, clock, n, step=1.0):
    """n ticks, each one min_poll-eligible, with the executor clock moving ``step`` s per tick."""
    for _ in range(n):
        clock.advance(step)
        eng.tick()


def _wait_running(ff, eng):
    ff.drive(eng, until=lambda st: st.nodes.get("a") and st.nodes["a"].last
             and st.nodes["a"].last.job.get("state") == "RUNNING", timeout=10)


def test_transport_poll_failure_never_changes_verdict(ff, monkeypatch, clock):
    net = _flaky_transport(monkeypatch)
    eng = ff.run(ff.plan([ff.job("a", "sleep 0.5\n" + OUT)], clusters={"c": ff.cluster(lost_after="0.1s")}))
    _wait_running(ff, eng)
    net["down"] = True
    ff.wait_job_state(ff.jobs()[0]["id"], "COMPLETED")  # job finishes while we are blind
    _ticks(eng, clock, 8)
    assert net["failed_polls"] >= 1  # polls back off during an outage (P2-3), so not one per tick
    st = eng.state()
    assert st.nodes["a"].status == "running" and st.nodes["a"].last.job["state"] == "RUNNING"
    errs = ff.events(eng, "job.remote_error")
    assert errs and all(e["payload"]["op"] == "poll" and e["payload"]["transient"] for e in errs)
    assert ff.events(eng, "job.lost") == []
    net["down"] = False
    clock.advance(1300)  # past the longest poll backoff
    st = ff.drive(eng, timeout=20)
    assert st.status == "succeeded"


def test_squeue_down_sacct_up_keeps_tracking(ff, monkeypatch, clock):
    eng = ff.run(ff.plan([ff.job("a", "sleep 1\n" + OUT)], clusters={"c": ff.cluster(lost_after="0.1s")}))
    _wait_running(ff, eng)
    ff.replace_cmd("squeue", SLURMCTLD_DOWN)
    _ticks(eng, clock, 6)
    assert eng.state().nodes["a"].status == "running"
    ff.restore_cmd("squeue")
    st = ff.drive(eng, timeout=20)
    assert st.status == "succeeded"
    assert ff.events(eng, "job.lost") == []


def test_squeue_and_sacct_down_never_declares_lost(ff, clock):
    eng = ff.run(ff.plan([ff.job("a", "sleep 2\n" + OUT)], clusters={"c": ff.cluster(lost_after="1s")}))
    _wait_running(ff, eng)
    ff.replace_cmd("squeue", SLURMCTLD_DOWN)
    ff.replace_cmd("sacct", 'echo "sacct: error: Problem talking to the database: Connection refused" >&2; exit 1')
    _ticks(eng, clock, 8)  # 8 polls spanning 8 s while the job is still RUNNING
    ff.restore_cmd("squeue")
    ff.restore_cmd("sacct")
    assert ff.events(eng, "job.lost") == []
    st = ff.drive(eng, timeout=20)
    assert st.status == "succeeded"
    assert len(ff.jobs()) == 1


def test_truncated_poll_output_is_a_poll_error(ff, monkeypatch, clock):
    monkeypatch.setenv("FAKESLURM_MINJOBAGE", "0")
    eng = ff.run(ff.plan([ff.job("a", "sleep 0.5\n" + OUT)], clusters={"c": ff.cluster(lost_after="0.5s")}))
    _wait_running(ff, eng)
    # the connection drops while sacct runs: the remote shell is killed, stdout is cut after "@@SACCT"
    ff.replace_cmd("sacct", "kill -9 $PPID")
    ff.wait_job_state(ff.jobs()[0]["id"], "COMPLETED")
    _ticks(eng, clock, 8)
    ff.restore_cmd("sacct")
    assert ff.events(eng, "job.lost") == []
    clock.advance(1300)  # past the poll backoff
    st = ff.drive(eng, timeout=20)
    assert st.status == "succeeded"


def test_poll_errors_back_off(ff, monkeypatch, clock):
    net = _flaky_transport(monkeypatch)
    eng = ff.run(ff.plan([ff.job(f"j{i}", "sleep 30") for i in range(3)]))
    ff.drive(eng, until=lambda st: all(st.nodes.get(f"j{i}") and st.nodes[f"j{i}"].last
                                       and st.nodes[f"j{i}"].last.job.get("job_id") for i in range(3)), timeout=10)
    net["down"] = True
    for _ in range(20):  # 20 s of outage at min_poll 0.5 s
        clock.advance(1.0)
        eng.tick()
    assert net["failed_polls"] <= 3, f"{net['failed_polls']} failed polls in 20 s of outage"
    assert len(ff.events(eng, "job.remote_error")) <= 9


# ====================================================================== cancel in the crash window

def test_cancel_in_crash_window_does_not_leak_job(ff, monkeypatch):
    eng = ff.run(ff.plan([ff.job("a", "sleep 3\n" + OUT)]))
    _crash_after_sbatch(ff, monkeypatch, eng)
    eng.cancel(node="a")
    st = ff.drive(eng, timeout=10)
    assert st.nodes["a"].status == "cancelled"
    jid = ff.jobs()[0]["id"]
    end = ff.wait_job_state(jid, ("CANCELLED", "COMPLETED", "FAILED"), timeout=10)
    assert end["state"] == "CANCELLED", "Slurm job of a cancelled attempt ran to completion"
