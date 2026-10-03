"""Pure unit tests for forgeflow.hpc.slurm and forgeflow.hpc.transport (no engine, no fake Slurm daemons)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from forgeflow.hpc import slurm
from forgeflow.hpc.transport import CmdResult, LocalTransport, SSHTransport, make_transport

# ====================================================================== base_state


@pytest.mark.parametrize("raw,want", [
    ("COMPLETED", "COMPLETED"), ("completed", "COMPLETED"), ("CANCELLED by 123", "CANCELLED"),
    ("CANCELLED by 0", "CANCELLED"), ("CANCELLED+", "CANCELLED"), ("  RUNNING  ", "RUNNING"),
    ("CD", "COMPLETED"), ("F", "FAILED"), ("CA", "CANCELLED"), ("TO", "TIMEOUT"), ("OOM", "OUT_OF_MEMORY"),
    ("NF", "NODE_FAIL"), ("PR", "PREEMPTED"), ("BF", "BOOT_FAIL"), ("DL", "DEADLINE"), ("RV", "REVOKED"),
    ("OUT_OF_MEMORY", "OUT_OF_MEMORY"), ("", ""), ("   ", ""), (None, ""),
])
def test_base_state(raw, want):
    assert slurm.base_state(raw) == want


# ====================================================================== parse_poll

def test_parse_poll_full_sections():
    out = "\n".join([
        "@@SQUEUE",
        "101|RUNNING|None",
        "102|PENDING|Priority",
        "103|COMPLETING",
        "garbage-without-pipe",
        "@@SACCT",
        "104|CANCELLED by 1234|0:15|00:00:07",
        "105|COMPLETED|0:0|00:01:00",
        "106|OUT_OF_ME+|0:125|00:00:02",
        "@@EV 101",
        "ec=",
        "started",
        "@@EV 104",
        "ec=0",
        "started",
        "cancelled",
        "outputs",
        "@@EV 107",
        "missing_dir",
        "@@EV 108",
        "ec=-1",
        "@@EV 109",
        "ec=abc",
    ])
    p = slurm.parse_poll(out)
    assert p["squeue_ok"] and p["sacct_ok"]
    assert p["squeue"] == {"101": ("RUNNING", "None"), "102": ("PENDING", "Priority"), "103": ("COMPLETING", "")}
    assert p["sacct"]["104"] == ("CANCELLED", "0:15", "00:00:07")
    assert p["sacct"]["105"] == ("COMPLETED", "0:0", "00:01:00")
    assert p["sacct"]["106"][0] == "OUT_OF_ME"  # truncated names are not something -P produces; kept raw
    assert p["evidence"]["101"] == {"ec": None, "started": True}
    assert p["evidence"]["104"] == {"ec": 0, "started": True, "cancelled": True, "outputs": True}
    assert p["evidence"]["107"] == {"missing_dir": True}
    assert p["evidence"]["108"]["ec"] == -1
    assert p["evidence"]["109"]["ec"] is None


def test_parse_poll_error_markers_and_empty():
    p = slurm.parse_poll("@@SQUEUE\n@@SQUEUE_ERR\n@@SACCT\n@@SACCT_ERR\n")
    assert p["squeue_ok"] is False and p["sacct_ok"] is False
    assert p["squeue"] == {} and p["sacct"] == {}
    assert slurm.parse_poll("") == {"squeue": {}, "sacct": {}, "evidence": {}, "squeue_ok": True, "sacct_ok": True,
                                    "complete": False}  # no @@END sentinel -> incomplete (P1-2)


def test_poll_command_is_one_script_and_quotes_dirs(tmp_path):
    d = tmp_path / "it's a dir"
    (d / ".forgeflow").mkdir(parents=True)
    (d / ".forgeflow" / "ec").write_text("0\n")
    (d / ".forgeflow" / "started").write_text("x")
    (d / "outputs.json").write_text("{}")
    cmd = slurm.poll_command([("11", str(d)), ("12", str(tmp_path / "nope"))],
                             {"squeue": "false", "sacct": "true"})
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert r.returncode == 0
    p = slurm.parse_poll(r.stdout)
    assert p["squeue_ok"] is False and p["sacct_ok"] is True
    assert p["evidence"]["11"] == {"ec": 0, "started": True, "outputs": True}
    assert p["evidence"]["12"] == {"missing_dir": True}
    assert "-j 11,12" in cmd


# ====================================================================== observe

def _poll(squeue=None, sacct=None, ev=None):
    return {"squeue": squeue or {}, "sacct": sacct or {}, "evidence": ev or {}, "squeue_ok": True, "sacct_ok": True}


SQUEUE_TABLE = [
    # raw squeue %T (or %t)      -> forgeflow state
    ("PENDING", "QUEUED"), ("CONFIGURING", "QUEUED"), ("REQUEUED", "QUEUED"), ("REQUEUE_HOLD", "QUEUED"),
    ("REQUEUE_FED", "QUEUED"), ("RESV_DEL_HOLD", "QUEUED"), ("SPECIAL_EXIT", "QUEUED"),
    ("PD", "QUEUED"), ("CF", "QUEUED"), ("RQ", "QUEUED"), ("RH", "QUEUED"), ("SE", "QUEUED"),
    ("RUNNING", "RUNNING"), ("COMPLETING", "RUNNING"), ("STAGE_OUT", "RUNNING"), ("SUSPENDED", "RUNNING"),
    ("SIGNALING", "RUNNING"), ("STOPPED", "RUNNING"), ("RESIZING", "RUNNING"),
    ("R", "RUNNING"), ("CG", "RUNNING"), ("SO", "RUNNING"), ("S", "RUNNING"), ("SI", "RUNNING"), ("ST", "RUNNING"),
    ("COMPLETED", "EXITED"), ("FAILED", "EXITED"), ("CANCELLED", "EXITED"), ("TIMEOUT", "EXITED"),
    ("OUT_OF_MEMORY", "EXITED"), ("NODE_FAIL", "EXITED"), ("PREEMPTED", "EXITED"), ("BOOT_FAIL", "EXITED"),
    ("DEADLINE", "EXITED"), ("REVOKED", "EXITED"),
    ("CD", "EXITED"), ("F", "EXITED"), ("CA", "EXITED"), ("TO", "EXITED"), ("OOM", "EXITED"), ("NF", "EXITED"),
    ("PR", "EXITED"), ("BF", "EXITED"), ("DL", "EXITED"), ("RV", "EXITED"),
]


@pytest.mark.parametrize("raw,want", SQUEUE_TABLE)
def test_observe_squeue_states(raw, want):
    st = slurm.base_state(raw)
    obs = slurm.observe("7", _poll(squeue={"7": (st, "")}))
    assert obs["state"] == want, (raw, obs)
    assert obs["source"] == "squeue"
    if want == "EXITED":
        assert obs["final"] == slurm.SHORT.get(raw, raw)


def test_observe_short_code_RS_is_resizing_running():
    obs = slurm.observe("7", _poll(squeue={"7": ("RS", "")}))
    assert obs["state"] == "RUNNING"


@pytest.mark.parametrize("started,want", [(True, "RUNNING"), (False, "QUEUED")])
def test_observe_unknown_squeue_state_uses_started_marker(started, want):
    ev = {"7": {"ec": None, "started": True}} if started else {}
    assert slurm.observe("7", _poll(squeue={"7": ("SOMETHING_NEW", "")}, ev=ev))["state"] == want


SACCT_TABLE = [
    ("PENDING", "QUEUED"), ("REQUEUED", "QUEUED"), ("RUNNING", "RUNNING"), ("SUSPENDED", "RUNNING"),
    ("COMPLETED", "EXITED"), ("FAILED", "EXITED"), ("CANCELLED by 123", "EXITED"), ("CANCELLED+", "EXITED"),
    ("TIMEOUT", "EXITED"), ("OUT_OF_MEMORY", "EXITED"), ("NODE_FAIL", "EXITED"), ("PREEMPTED", "EXITED"),
    ("BOOT_FAIL", "EXITED"), ("DEADLINE", "EXITED"), ("REVOKED", "EXITED"),
]


@pytest.mark.parametrize("raw,want", SACCT_TABLE)
def test_observe_sacct_states(raw, want):
    obs = slurm.observe("9", _poll(sacct={"9": (slurm.base_state(raw), "0:0", "00:00:03")}))
    assert obs["state"] == want
    assert obs["source"] == "sacct"
    assert obs["exit"] == "0:0" and obs["elapsed"] == "00:00:03"


def test_observe_squeue_terminal_prefers_sacct_detail():
    obs = slurm.observe("9", _poll(squeue={"9": ("COMPLETED", "")}, sacct={"9": ("COMPLETED", "0:0", "00:00:03")},
                                   ev={"9": {"ec": 0}}))
    assert obs["state"] == "EXITED" and obs["source"] == "sacct" and obs["final"] == "COMPLETED"
    assert obs["ec"] == 0


def test_observe_squeue_live_wins_over_stale_sacct():
    obs = slurm.observe("9", _poll(squeue={"9": ("RUNNING", "")}, sacct={"9": ("PENDING", "0:0", "")}))
    assert obs["state"] == "RUNNING"


def test_observe_squeue_terminal_without_sacct():
    obs = slurm.observe("9", _poll(squeue={"9": ("TIMEOUT", "TimeLimit")}))
    assert obs["state"] == "EXITED" and obs["final"] == "TIMEOUT" and obs["source"] == "squeue"


def test_observe_evidence_only_when_scheduler_forgot():
    obs = slurm.observe("9", _poll(ev={"9": {"ec": 4, "started": True}}))
    assert obs["state"] == "EXITED" and obs["source"] == "evidence" and obs["ec"] == 4
    assert obs.get("final") is None


@pytest.mark.parametrize("ev", [{}, {"9": {"ec": None, "started": True}}, {"9": {"missing_dir": True}}])
def test_observe_unknown(ev):
    obs = slurm.observe("9", _poll(ev=ev))
    assert obs["state"] == "UNKNOWN"
    assert obs["missing_dir"] == bool(ev.get("9", {}).get("missing_dir"))


def test_observe_other_jobs_do_not_leak():
    obs = slurm.observe("9", _poll(squeue={"10": ("RUNNING", "")}, sacct={"11": ("COMPLETED", "0:0", "")}))
    assert obs["state"] == "UNKNOWN"


# ====================================================================== verdict truth table

VERDICTS = [
    # (ec, final, cancel_marker, exit) -> (status, class, retryable)
    (0, "COMPLETED", False, "0:0", ("succeeded", None, False)),
    (0, None, False, None, ("succeeded", None, False)),                      # evidence only
    (3, "FAILED", False, "3:0", ("failed", "exit_nonzero", False)),
    (3, None, False, None, ("failed", "exit_nonzero", False)),
    (None, "FAILED", False, "1:0", ("failed", "exit_nonzero", False)),
    (None, "TIMEOUT", False, "0:15", ("failed", "timeout", False)),
    (0, "TIMEOUT", False, "0:15", ("failed", "timeout", False)),
    (2, "TIMEOUT", False, "0:15", ("failed", "timeout", False)),
    (None, "DEADLINE", False, None, ("failed", "timeout", False)),
    (None, "OUT_OF_MEMORY", False, "0:125", ("failed", "oom", False)),
    (0, "OUT_OF_MEMORY", False, "0:125", ("failed", "oom", False)),
    (None, "NODE_FAIL", False, None, ("failed", "node_fail", True)),
    (None, "BOOT_FAIL", False, None, ("failed", "node_fail", True)),
    (None, "PREEMPTED", False, None, ("failed", "preempted", True)),
    (0, "PREEMPTED", False, None, ("failed", "preempted", True)),
    (None, "CANCELLED", False, "0:15", ("failed", "cancelled_ext", False)),
    (None, "REVOKED", False, None, ("failed", "cancelled_ext", False)),
    (None, "CANCELLED", True, "0:15", ("cancelled", "cancelled", False)),
    (None, None, True, None, ("cancelled", "cancelled", False)),
    (None, "TIMEOUT", True, None, ("cancelled", "cancelled", False)),       # forgeflow asked; no ec
    (0, "COMPLETED", True, "0:0", ("succeeded", None, False)),              # finished before the cancel landed
    (None, "COMPLETED", False, "0:0", ("failed", "missing_ec", True)),
    (None, None, False, None, ("failed", "unknown", True)),
]


@pytest.mark.parametrize("ec,final,marker,exit_,want", VERDICTS)
def test_verdict_table(ec, final, marker, exit_, want):
    obs = {"ec": ec, "final": final, "cancel_marker": marker, "exit": exit_}
    status, cls, msg, retry = slurm.verdict(obs)
    assert (status, cls, retry) == want, msg
    assert isinstance(msg, str) and msg


def test_verdict_cancelled_by_uid_end_to_end_parse():
    p = slurm.parse_poll("@@SQUEUE\n@@SACCT\n55|CANCELLED by 4242|0:15|00:00:01\n@@EV 55\nec=\nstarted\n")
    obs = slurm.observe("55", p)
    assert obs["final"] == "CANCELLED"
    assert slurm.verdict(obs)[1] == "cancelled_ext"


def test_verdict_duplicate_guard_code_from_evidence():
    # the guard exits before writing .forgeflow/ec, so a duplicate shows up as ec=None + sacct ExitCode 97 (P2-4)
    status, cls, _, retry = slurm.verdict({"ec": None, "final": "FAILED", "exit": "97:0"})
    assert (status, cls, retry) == ("failed", "duplicate", False)


def test_verdict_duplicate_guard_seen_via_sacct_exitcode():
    p = slurm.parse_poll("@@SQUEUE\n@@SACCT\n56|FAILED|97:0|00:00:00\n@@EV 56\nec=0\nstarted\n")
    # owner job (another id) wrote ec=0 in the shared dir; the tracked id is the duplicate that exited 97
    p["evidence"]["56"]["ec"] = None
    obs = slurm.observe("56", p)
    assert slurm.verdict(obs)[1] == "duplicate"


def test_verdict_payload_exit_97_is_exit_nonzero():
    status, cls, _, _ = slurm.verdict({"ec": 97, "final": "FAILED", "exit": "97:0", "cancel_marker": False})
    assert cls == "exit_nonzero"


# ====================================================================== sbatch directives / script

def test_sbatch_directives_mapping():
    res = {"nodes": 2, "ntasks": 8, "ntasks_per_node": 4, "cpus_per_task": 2, "mem": "4G", "mem_per_cpu": "1G",
           "time": "1:00:00", "partition": "debug", "account": "proj", "qos": "normal", "gpus": 1,
           "gres": "gpu:a100:1", "constraint": "cpu", "reservation": "r1", "exclude": "n[1-3]",
           "exclusive": True, "extra": ["--signal=B:USR1@300", "--requeue"], "unknown_key": "ignored"}
    lines = slurm.sbatch_directives(res)
    assert lines == [
        "#SBATCH --nodes=2", "#SBATCH --ntasks=8", "#SBATCH --ntasks-per-node=4", "#SBATCH --cpus-per-task=2",
        "#SBATCH --mem=4G", "#SBATCH --mem-per-cpu=1G", "#SBATCH --time=1:00:00", "#SBATCH --partition=debug",
        "#SBATCH --account=proj", "#SBATCH --qos=normal", "#SBATCH --gpus=1", "#SBATCH --gres=gpu:a100:1",
        "#SBATCH --constraint=cpu", "#SBATCH --reservation=r1", "#SBATCH --exclude=n[1-3]", "#SBATCH --exclusive",
        "#SBATCH --signal=B:USR1@300", "#SBATCH --requeue"]


@pytest.mark.parametrize("v,want", [(90, "1:30:00"), (5, "0:05:00"), (60, "1:00:00"), (1440, "24:00:00"),
                                    ("00:00:02", "00:00:02"), ("2-00:00:00", "2-00:00:00"), ("30", "30")])
def test_sbatch_time_minutes(v, want):
    assert slurm.sbatch_directives({"time": v}) == [f"#SBATCH --time={want}"]


def test_sbatch_time_fractional_minutes_not_truncated_to_unlimited():
    assert slurm.sbatch_directives({"time": 0.5}) == ["#SBATCH --time=0:00:30"]


@pytest.mark.parametrize("v", [None, False, ""])
def test_sbatch_directives_skip_empty(v):
    assert slurm.sbatch_directives({"partition": v, "exclusive": False}) == []


def test_sbatch_directives_reject_newline_injection():
    try:
        lines = slurm.sbatch_directives({"partition": "debug\ntouch /tmp/pwned", "account": "a"})
    except ValueError:
        return
    body = "\n".join(lines)
    assert all(l.startswith("#SBATCH ") for l in body.splitlines()), body


def _render(job_dir, env=None, resources=None, prelude=None, modules=None):
    return slurm.render_job_script(key="ff-abc-n-a1", job_dir=str(job_dir), resources=resources or {"time": 5},
                                   env=env or {}, prelude=prelude, modules=modules)


def test_render_job_script_shape(tmp_path):
    s = _render(tmp_path, env={"FF_RUN_ID": "r1", "bad key": "x", "X": "a b'c$(touch PWN)"},
                prelude=["echo pre"], modules=["gcc/12"])
    lines = s.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert "#SBATCH --job-name=ff-abc-n-a1" in lines
    assert "#SBATCH --time=0:05:00" in lines
    # every #SBATCH line comes before the first command (Slurm stops parsing at the first command)
    first_cmd = next(i for i, l in enumerate(lines) if l and not l.startswith("#"))
    assert all(i < first_cmd for i, l in enumerate(lines) if l.startswith("#SBATCH"))
    assert "module load gcc/12" in lines and "echo pre" in lines
    assert "bad key" not in s
    assert subprocess.run(["bash", "-n", "-c", s]).returncode == 0


def _run_script(job_dir: Path, jid: str, user: str = "echo ok") -> subprocess.CompletedProcess:
    (job_dir / "user.sh").write_text("set -eo pipefail\n" + user + "\n")
    (job_dir / "job.sh").write_text(_render(job_dir, env={"X": "a b'c$(touch PWN)", "FF_ATTEMPT": 1}))
    env = {**os.environ, "SLURM_JOB_ID": jid}
    return subprocess.run(["bash", str(job_dir / "job.sh")], cwd=job_dir, env=env, capture_output=True, text=True)


def test_job_script_writes_evidence_and_guards_duplicates(tmp_path):
    d = tmp_path / "jd"
    d.mkdir()
    r = _run_script(d, "100", user='printf "%s" "$X" > x.txt; echo "$FF_OUTPUTS" > where.txt')
    assert r.returncode == 0, r.stderr
    assert (d / ".forgeflow" / "ec").read_text().strip() == "0"
    assert (d / ".forgeflow" / "owner" / "id").read_text().strip() == "100"
    assert (d / ".forgeflow" / "started").exists() and (d / ".forgeflow" / "ended").exists()
    assert (d / "x.txt").read_text() == "a b'c$(touch PWN)"          # env value exported verbatim
    assert not (d / "PWN").exists()
    assert (d / "where.txt").read_text().strip() == f"{d}/outputs.json"
    # a second copy of the same attempt (different Slurm id) is refused with 97 and does not touch ec
    (d / ".forgeflow" / "ec").write_text("0\n")
    r2 = _run_script(d, "101", user="echo SECOND > second.txt; exit 5")
    assert r2.returncode == 97
    assert "another job already owns this attempt" in r2.stderr
    assert not (d / "second.txt").exists()
    assert (d / ".forgeflow" / "ec").read_text().strip() == "0"
    # a Slurm requeue keeps the same id and is allowed to run again
    r3 = _run_script(d, "100", user="exit 6")
    assert r3.returncode == 6
    assert (d / ".forgeflow" / "ec").read_text().strip() == "6"


def test_job_script_pipefail_in_user_payload(tmp_path):
    d = tmp_path / "jd"
    d.mkdir()
    r = _run_script(d, "1", user="false | true\necho never > never.txt")
    assert r.returncode == 1
    assert not (d / "never.txt").exists()
    assert (d / ".forgeflow" / "ec").read_text().strip() == "1"


def test_job_script_missing_dir_exits_96(tmp_path):
    s = _render(tmp_path / "does-not-exist")
    p = tmp_path / "job.sh"
    p.write_text(s)
    r = subprocess.run(["bash", str(p)], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 96


# ====================================================================== submit key

@pytest.mark.parametrize("node", ["a", "relax", "x[3]", "n-with-dash_and_underscore", "Z" * 80,
                                  "very_long_node_identifier_that_goes_on_and_on_for_a_while[123]"])
@pytest.mark.parametrize("attempt", [1, 12, 999])
def test_submit_key_charset_length(node, attempt):
    k = slurm.submit_key("myplan-20261003-040528-4e92", node, attempt)
    assert len(k) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_.\-]+", k), k
    assert k.startswith("ff-") and k.endswith(f"-a{attempt}")
    assert slurm.submit_key("myplan-20261003-040528-4e92", node, attempt) == k   # deterministic


def test_submit_key_distinct():
    rid = "p-20261003-000000-aaaa"
    keys = {slurm.submit_key(rid, n, a) for n in ("a", "b", "x[1]", "x[2]", "L" * 70, "L" * 71) for a in (1, 2)}
    assert len(keys) == 12
    assert slurm.submit_key(rid, "a", 1) != slurm.submit_key("p-20261003-000000-aaab", "a", 1)


# ====================================================================== parse_submit

@pytest.mark.parametrize("out,want", [
    ("SUBMITTED 1234\n", ("1234", "submitted")),
    ("noise\nEXISTING 99\n", ("99", "existing")),
    ("SUBMITTED 1234_7", ("1234_7", "submitted")),
    ("motd line\nSUBMITTED 5\ntrailing junk", ("5", "submitted")),
    ("SUBMITTED \n", None), ("SUBMITTED abc", None), ("", None), ("Submitted batch job 12", None),
])
def test_parse_submit(out, want):
    assert slurm.parse_submit(out) == want


def test_submit_command_reuses_jobid_file(tmp_path):
    d = tmp_path / "jd"
    (d / ".forgeflow").mkdir(parents=True)
    (d / ".forgeflow" / "jobid").write_text("4242;cluster\n")
    cmd = slurm.submit_command(str(d), "k", "fp", {"sbatch": "false", "squeue": "false", "sacct": "false"})
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert r.returncode == 0
    assert slurm.parse_submit(r.stdout) == ("4242", "existing")


def test_submit_command_sbatch_failure_is_nonzero_and_leaves_no_jobid(tmp_path):
    d = tmp_path / "jd"
    d.mkdir()
    sb = tmp_path / "sbatch"
    sb.write_text("#!/bin/sh\necho 'sbatch: error: Batch job submission failed' >&2\nexit 1\n")
    sb.chmod(0o755)
    cmd = slurm.submit_command(str(d), "k", "fp", {"sbatch": str(sb), "squeue": "true", "sacct": "true"})
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert r.returncode != 0
    assert slurm.parse_submit(r.stdout) is None
    assert not (d / ".forgeflow" / "jobid").exists()


# ====================================================================== transport

@pytest.mark.parametrize("res,transient", [
    (CmdResult(255, "", "ssh: connect to host x port 22: Connection timed out"), True),
    (CmdResult(255, "", ""), True),
    (CmdResult(124, "", "", timed_out=True), True),
    (CmdResult(1, "", "slurm_load_jobs error: Unable to contact slurm controller (connect failure)"), True),
    (CmdResult(1, "", "sbatch: error: Batch job submission failed: Socket timed out on send/recv operation"), True),
    (CmdResult(1, "", "sbatch: error: Batch job submission failed: Invalid account or account/partition"), False),
    (CmdResult(2, "", "cp: cannot stat 'x': No such file or directory"), False),
])
def test_cmdresult_transient(res, transient):
    assert res.transient is transient


def test_local_transport_run_put_get(tmp_path):
    tr = make_transport({"transport": "local", "env": {"FOO": "b a'r"}})
    assert isinstance(tr, LocalTransport) and tr.is_local
    r = tr.run('printf "%s" "$FOO"; echo err >&2; exit 3')
    assert (r.rc, r.out, r.err.strip()) == (3, "b a'r", "err")
    r = tr.run("sleep 5", timeout=0.3)
    assert r.timed_out and r.transient
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A")
    (src / "sub" / "b.dat").write_text("B")
    assert tr.put(src, str(tmp_path / "dst" / "deep")).rc == 0
    assert (tmp_path / "dst" / "deep" / "sub" / "b.dat").read_text() == "B"
    assert tr.put(src / "a.txt", str(tmp_path / "dst2" / "x" / "a2.txt")).rc == 0
    assert (tmp_path / "dst2" / "x" / "a2.txt").read_text() == "A"
    got = tmp_path / "got"
    assert tr.get(str(src), got, ["*.txt", "sub/*.dat"]).rc == 0
    assert sorted(str(p.relative_to(got)) for p in got.rglob("*") if p.is_file()) == ["a.txt", "sub/b.dat"]


def test_ssh_transport_options():
    tr = make_transport({"transport": "ssh", "host": "myhpc", "connect_timeout": 7, "ssh_options": ["-p", "2222"]})
    assert isinstance(tr, SSHTransport) and not tr.is_local
    assert "BatchMode=yes" in tr.opts and "ConnectTimeout=7" in tr.opts and "ControlMaster=no" in tr.opts
    assert tr.opts[-2:] == ["-p", "2222"]
