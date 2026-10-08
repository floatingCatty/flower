"""The ``ssh`` transport.

* ``test_real_ssh_localhost`` runs only if ``ssh -o BatchMode=yes localhost true`` works on this machine.
* Everything else uses a *fake* OpenSSH client put first on PATH: it parses ssh's options, then runs the remote
  command locally with ``bash -c`` (exactly what sshd does with the joined argv), with HOME pointed at a temp
  "remote home". rsync's ``-e ssh …`` goes through it too, so SSHTransport.run/put/get are exercised for real.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flower.hpc.transport import SSHTransport

OUT = 'printf \'{"x": 1}\' > "$FLOWER_OUTPUTS"\necho result > result.txt'

FAKE_SSH = r"""#!/usr/bin/env bash
# fake OpenSSH client for tests
while [ $# -gt 0 ]; do
  case "$1" in
    -O) echo "Control socket connect(none): No such file or directory" >&2; exit 255 ;;
    -o|-p|-i|-l|-F|-J) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
host="$1"; shift
printf '%s\0' "$host $*" >> "$FAKESSH_STATE/log"
if [ -e "$FAKESSH_STATE/down" ]; then
  echo "ssh: connect to host $host port 22: Connection timed out" >&2; exit 255
fi
case "$*" in
  *"rsync --server --sender"*)
    if [ -e "$FAKESSH_STATE/down_get" ]; then rm -f "$FAKESSH_STATE/down_get"
      echo "Connection reset by peer" >&2; exit 255; fi ;;
esac
export HOME="$FAKESSH_STATE/remote-home"
cmd="$*"
# SSHTransport sends `bash -lc '<script>'`; a login shell would source /etc/profile, which on some hosts resets
# PATH (and with it the bash / python the fake Slurm needs). Run it as a plain `bash -c` instead.
case "$cmd" in "bash -lc "*) cmd="bash -c ${cmd#bash -lc }" ;; esac
exec bash -c "$cmd"
"""


@pytest.fixture
def fakessh(ff, tmp_path, monkeypatch):
    if not shutil.which("rsync"):
        pytest.skip("rsync is not installed")
    state = tmp_path / "fakessh"
    (state / "remote-home").mkdir(parents=True)
    bindir = tmp_path / "fakessh-bin"
    bindir.mkdir()
    (bindir / "ssh").write_text(FAKE_SSH)
    (bindir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKESSH_STATE", str(state))
    return state


def _ssh_cluster(ff, **kw):
    c = ff.cluster(transport="ssh", host="hpc-alias", remote_root="~/ffruns")
    c.update(kw)
    return {"c": c}


def test_ssh_transport_run_put_get_with_fake_client(fakessh, tmp_path):
    tr = SSHTransport({"host": "h", "env": {"FOO": "b a'r"}})
    r = tr.run('printf "%s|%s" "$FOO" "$HOME"')
    assert r.rc == 0, r.err
    assert r.out == f"b a'r|{fakessh / 'remote-home'}"
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f.txt").write_text("F")
    (src / "top.txt").write_text("T")
    dst = tmp_path / "remote" / "x"
    dst.parent.mkdir()
    assert tr.put(src, str(dst)).rc == 0
    assert (dst / "d" / "f.txt").read_text() == "F"
    back = tmp_path / "back"
    r = tr.get(str(dst), back, ["*.txt"])
    assert r.rc == 0, r.err
    assert sorted(str(p.relative_to(back)) for p in back.rglob("*") if p.is_file()) == ["d/f.txt", "top.txt"]
    (fakessh / "down").touch()
    r = tr.run("true")
    assert r.rc == 255 and r.transient


def test_ssh_job_end_to_end(ff, fakessh, tmp_path):
    src = tmp_path / "POSCAR"
    src.write_text("STRUCT")
    script = "cat POSCAR > seen.txt\nmkdir -p extra && echo e > extra/e.log\n" + OUT
    eng = ff.run(ff.plan([ff.job("a", script, stage_in=[str(src)], files={"res": "result.txt"},
                                 retrieve=["extra/*.log"], outputs={"x": "integer"})],
                         clusters=_ssh_cluster(ff)))
    st = ff.drive(eng, timeout=40)
    assert st.status == "succeeded", ff.why(eng)
    r = st.nodes["a"].result
    remote_dir = Path(r.outputs["job_dir"])
    assert remote_dir == fakessh / "remote-home" / "ffruns" / st.run_id / "a" / "a1"
    assert (remote_dir / "seen.txt").read_text() == "STRUCT"
    local = eng.paths.attempt_dir("a", 1) / "job"
    assert (local / "outputs.json").exists() and (local / "extra" / "e.log").exists()
    assert (local / f"slurm-{r.outputs['job_id']}.out").exists()
    assert (local / ".flower" / "ec").read_text().strip() == "0"
    assert not (local / "seen.txt").exists()  # only declared/retrieved things come back
    assert Path(r.files["res"]["path"]) == (local / "result.txt").resolve()
    assert ff.events(eng, "job.retrieved")
    assert len(ff.jobs()) == 1
    # one ssh round trip per poll: every poll call carries squeue + sacct + evidence together
    calls = (fakessh / "log").read_text().split("\0")
    polls = [c for c in calls if "echo @@SQUEUE" in c]
    assert polls and all("@@SACCT" in l and "@@EV" in l for l in polls)


def test_ssh_submit_outage_is_remote_error_then_recovers(ff, fakessh, clock):
    eng = ff.run(ff.plan([ff.job("a", OUT)], clusters=_ssh_cluster(ff)))
    eng.tick()  # HOME probe + staging + submit all succeed
    assert ff.events(eng, "job.submitted")
    (fakessh / "down").touch()
    ff.tick_for(eng, 1.5)
    errs = ff.events(eng, "job.remote_error")
    assert errs and all(e["payload"]["op"] == "poll" and e["payload"]["transient"] for e in errs)
    assert eng.state().nodes["a"].status == "running"
    (fakessh / "down").unlink()
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    assert len(ff.jobs()) == 1


def test_ssh_unreachable_at_start_is_retried(ff, fakessh, clock):
    (fakessh / "down").touch()
    eng = ff.run(ff.plan([ff.job("a", OUT)], clusters=_ssh_cluster(ff)))
    eng.tick()
    assert eng.state().nodes["a"].status != "failed", ff.why(eng)
    (fakessh / "down").unlink()
    clock.advance(3600)
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded"


def test_ssh_retrieve_blip_does_not_discard_completed_job(ff, fakessh):
    (fakessh / "down_get").touch()  # the first rsync download fails once
    eng = ff.run(ff.plan([ff.job("a", OUT, retry={"max_attempts": 2, "backoff": "0s"})],
                         clusters=_ssh_cluster(ff)))
    st = ff.drive(eng, timeout=40)
    assert not (fakessh / "down_get").exists()
    assert st.status == "succeeded"
    assert len(st.nodes["a"].attempts) == 1 and len(ff.jobs()) == 1


def test_ssh_direct_job_end_to_end(ff, fakessh, tmp_path):
    """scheduler: none over ssh: a detached process on the host, outputs/logs fetched back with rsync."""
    clusters = _ssh_cluster(ff, scheduler="none", min_poll="0.2s")
    eng = ff.run(ff.plan([
        ff.job("a", "echo hi\n" + OUT, files={"res": "result.txt"}, outputs={"x": "integer"}),
        ff.job("b", 'echo "{\\"y\\": ${a.outputs.x}}" > "$FLOWER_OUTPUTS"', outputs={"y": "integer"}),
    ], clusters=clusters))
    st = ff.drive(eng, timeout=40)
    assert st.status == "succeeded", ff.why(eng)
    a = st.nodes["a"].result
    remote_dir = Path(a.outputs["job_dir"])
    assert remote_dir == fakessh / "remote-home" / "ffruns" / st.run_id / "a" / "a1"
    local = eng.paths.attempt_dir("a", 1) / "job"
    assert a.outputs["local_dir"] == str(local)  # where the fetched files are, for local analysis steps
    assert (local / "job.out").read_text().strip() == "hi" and (local / "outputs.json").exists()
    assert Path(a.files["res"]["path"]) == (local / "result.txt").resolve()
    assert ff.jobs() == []  # no Slurm
    calls = [c for c in (fakessh / "log").read_text().split("\0") if c]
    assert any("nohup $L bash job.sh" in c for c in calls)
    # few round trips: the job dir's content goes up in ONE rsync (which also creates it), then ONE launch call;
    # $HOME is asked once per run, not per step
    b = st.nodes["b"].result
    assert b.outputs["y"] == 1
    for d in (str(remote_dir), b.outputs["job_dir"]):
        launch = [c for c in calls if d in c and "@@EV" not in c and "--sender" not in c]
        assert len(launch) == 2, launch
    assert sum(1 for c in calls if c.endswith("echo $HOME'") or "echo $HOME" in c) == 1


def _real_ssh_ok() -> tuple[bool, str]:
    if not shutil.which("ssh"):
        return False, "no ssh client"
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "localhost", "true"],
                           capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stderr or "").strip()[:200]


def test_real_ssh_localhost(ff, tmp_path):
    ok, why = _real_ssh_ok()
    if not ok:
        pytest.skip(f"`ssh -o BatchMode=yes localhost true` does not work here ({why}); "
                    "the ssh transport is covered by the fake-ssh tests in this file")
    root = tmp_path / "remote-root"
    eng = ff.run(ff.plan([ff.job("a", OUT, files={"res": "result.txt"})],
                         clusters=_ssh_cluster(ff, host="localhost", remote_root=str(root))))
    st = ff.drive(eng, timeout=60)
    assert st.status == "succeeded", ff.why(eng)
    assert Path(st.nodes["a"].result.outputs["job_dir"]).is_relative_to(root)
    assert (eng.paths.attempt_dir("a", 1) / "job" / "result.txt").read_text().strip() == "result"


def test_ssh_retrieve_over_the_limit_fetches_nothing(ff, fakessh):
    """A broad `retrieve:` must not pull gigabytes unannounced: over `retrieve_limit` nothing comes back."""
    eng = ff.run(ff.plan([ff.job("a", "head -c 3000000 /dev/zero > big.bin\n" + OUT, retrieve=["big.bin"],
                                 retrieve_limit="1M")], clusters=_ssh_cluster(ff, scheduler="none", min_poll="0.2s")))
    st = ff.drive(eng, timeout=40)
    err = st.nodes["a"].last.error
    assert st.nodes["a"].status == "failed" and err["error_class"] == "retrieve_limit", err
    assert "2.86 MB" in err["message"] and "1 MB" in err["message"]
    assert not (eng.paths.attempt_dir("a", 1) / "job" / "big.bin").exists()


def test_ssh_a_running_job_shows_what_it_printed_last(ff, fakessh):
    eng = ff.run(ff.plan([ff.job("a", "echo 'step 1 of 2'; sleep 3; echo 'step 2 of 2'\n" + OUT)],
                         clusters=_ssh_cluster(ff, scheduler="none", min_poll="0.2s")))
    live = eng.paths.attempt_dir("a", 1) / "live.txt"
    ff.drive(eng, timeout=20, until=lambda st: live.exists() and "step 1" in live.read_text())
    from flower.render import status_view
    assert "step 1 of 2" in status_view(eng.state(), eng.paths)
    assert ff.drive(eng, timeout=30).status == "succeeded"
