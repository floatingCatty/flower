"""End-to-end ``job`` nodes on the fake Slurm: happy path, staging, failure classes, retries, cancel, limits, CLI."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from flower.engine import Engine
from flower.hpc import slurm
from flower.rundir import RunPaths

OUT_X = """printf '{"x": 41, "summary": "made x"}' > "$FLOWER_OUTPUTS"
echo payload-done"""


# ====================================================================== happy path

def test_happy_path_outputs_files_env(ff):
    script = r"""
echo "hello from $FLOWER_NODE_ID"
mkdir -p res
printf 'data-%s' "$FLOWER_IN_N" > res/data.txt
python3 - <<'PY'
import json, os
json.dump({"x": int(os.environ["FLOWER_IN_N"]) + 1, "run": os.environ["FLOWER_RUN_ID"], "node": os.environ["FLOWER_NODE_ID"],
           "att": os.environ["FLOWER_ATTEMPT"], "extra": os.environ["EXTRA"], "inside": os.environ["FLOWER_INSIDE_RUN"],
           "jid": os.environ["SLURM_JOB_ID"], "jobdir": os.environ["FLOWER_JOB_DIR"]}, open(os.environ["FLOWER_OUTPUTS"], "w"))
PY
"""
    plan = ff.plan([ff.job("a", script, outputs={"x": "integer", "run": "string"}, files={"data": "res/data.txt"},
                           env={"EXTRA": {"k": [1, 2]}}, inputs={"n": "${inputs.n}"})],
                   inputs={"n": {"type": "integer", "default": 7}})
    eng = ff.run(plan)
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded", ff.why(eng)
    r = st.nodes["a"].result
    out = r.outputs
    assert out["x"] == 8 and out["run"] == st.run_id and out["node"] == "a" and out["att"] == "1"
    assert json.loads(out["extra"]) == {"k": [1, 2]}
    assert out["inside"] == "1"
    jobs = ff.jobs()
    assert len(jobs) == 1
    assert out["job_id"] == jobs[0]["id"] == out["jid"]
    assert out["job_dir"] == out["jobdir"]
    assert out["job_dir"] == str(RunPaths(ff.home, st.run_id).attempt_dir("a", 1) / "job")
    assert jobs[0]["name"] == slurm.submit_key(st.run_id, "a", 1)
    assert jobs[0]["comment"]  # fingerprint passed through --comment
    f = r.files["data"]
    p = Path(f["path"])
    assert p.read_text() == "data-7"
    assert f["sha256"] == hashlib.sha256(b"data-7").hexdigest() and f["bytes"] == 6
    assert r.summary and "completed" in r.summary.lower()
    # journal: write-ahead intent precedes submission; observed only on change; exactly one exited
    types = [e["eventType"] for e in ff.events(eng, "job.")]
    assert types.index("job.submit_intent") < types.index("job.submitted")
    assert types.count("job.exited") == 1 and types.count("job.submitted") == 1
    obs = [e["payload"]["state"] for e in ff.events(eng, "job.observed")]
    assert all(a != b for a, b in zip(obs, obs[1:])), obs
    assert obs[-1] == "EXITED"
    # remote evidence files
    jd = Path(out["job_dir"])
    assert (jd / ".flower" / "jobid").read_text().strip() == jobs[0]["id"]
    sub = json.loads((jd / ".flower" / "submit.json").read_text())
    assert sub["submit_key"] == jobs[0]["name"] and sub["attempt"] == 1
    assert (jd / f"slurm-{jobs[0]['id']}.out").read_text().startswith("hello from a")


def test_summary_from_outputs(ff):
    eng = ff.run(ff.plan([ff.job("a", OUT_X)]))
    st = ff.drive(eng)
    assert st.status == "succeeded"
    assert st.nodes["a"].result.summary == "made x"


def test_job_without_outputs_json_succeeds_with_ids(ff):
    eng = ff.run(ff.plan([ff.job("a", "echo just-stdout")]))
    st = ff.drive(eng)
    assert st.status == "succeeded"
    out = st.nodes["a"].result.outputs
    assert set(out) == {"job_id", "job_dir", "local_dir"}
    assert "just-stdout" in st.nodes["a"].result.summary


def test_resources_reach_sbatch_and_cluster_defaults_merge(ff):
    clusters = {"c": ff.cluster(resources={"partition": "batch", "time": 10, "account": "acct"},
                                prelude=["export FROM_CLUSTER=1"], env={"CLUSTER_ENV": "ce"})}
    script = 'printf \'{"p": "%s", "c": "%s"}\' "$FROM_CLUSTER" "$CLUSTER_ENV" > "$FLOWER_OUTPUTS"'
    eng = ff.run(ff.plan([ff.job("a", script, resources={"time": "0:01:00", "nodes": 1},
                                 prelude="export FROM_NODE=1")], clusters=clusters))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    jd = Path(st.nodes["a"].result.outputs["job_dir"])
    body = (jd / "job.sh").read_text()
    assert "#SBATCH --partition=batch" in body and "#SBATCH --account=acct" in body
    assert "#SBATCH --time=0:01:00" in body and "#SBATCH --nodes=1" in body
    assert "--time=0:10:00" not in body  # node overrides cluster default
    assert "export FROM_CLUSTER=1" in body and "export FROM_NODE=1" in body
    assert st.nodes["a"].result.outputs["p"] == "1"
    # cluster `env` is exported by the transport around every remote command (sbatch inherits it)
    assert st.nodes["a"].result.outputs["c"] == "ce"
    assert ff.jobs()[0]["time_limit"] == 60


# ====================================================================== staging

def test_stage_in_local_copy_dir_and_link(ff, tmp_path):
    src = tmp_path / "inputs"
    (src / "pseudo").mkdir(parents=True)
    (src / "pseudo" / "Si.upf").write_text("PSEUDO")
    (src / "POSCAR").write_text("STRUCT")
    (src / "big.bin").write_text("BIG")
    script = r"""
test -f POSCAR && test -f pp/Si.upf && test -L big.bin
cat POSCAR pp/Si.upf big.bin > all.txt
printf '{"ok": true}' > "$FLOWER_OUTPUTS"
"""
    stage = [str(src / "POSCAR"), {"from": str(src / "pseudo"), "to": "pp"},
             {"from": str(src / "big.bin"), "mode": "link"}]
    eng = ff.run(ff.plan([ff.job("a", script, stage_in=stage, files={"all": "all.txt"})]))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    jd = Path(st.nodes["a"].result.outputs["job_dir"])
    assert (jd / "all.txt").read_text() == "STRUCTPSEUDOBIG"
    assert not (jd / "POSCAR").is_symlink()
    assert os.path.realpath(jd / "big.bin") == str(src / "big.bin")


def test_stage_in_missing_local_source_fails_cleanly(ff, tmp_path):
    eng = ff.run(ff.plan([ff.job("a", "true", stage_in=[str(tmp_path / "nope.txt")])]))
    st = ff.drive(eng, timeout=15)
    assert st.status == "failed"
    assert ff.error_class(st, "a") == "stage_in"
    assert ff.jobs() == []


@pytest.mark.parametrize("mode", ["link", "copy"])
def test_remote_handoff_between_jobs(ff, mode):
    first = ff.job("first", "mkdir -p data && echo CHARGE-DENSITY > data/rho.dat\n" + OUT_X)
    second = ff.job("second", r"""
test -f indata/rho.dat
printf '{"rho": "%s"}' "$(cat indata/rho.dat)" > "$FLOWER_OUTPUTS"
""", stage_in=[{"from": "remote:${first.outputs.job_dir}/data", "to": "indata", "mode": mode}])
    eng = ff.run(ff.plan([first, second]))
    st = ff.drive(eng, timeout=40)
    assert st.status == "succeeded", ff.why(eng)
    assert st.nodes["second"].result.outputs["rho"] == "CHARGE-DENSITY"
    j1 = Path(st.nodes["first"].result.outputs["job_dir"])
    j2 = Path(st.nodes["second"].result.outputs["job_dir"])
    assert j1 != j2
    staged = j2 / "indata"
    if mode == "link":
        assert staged.is_symlink() and os.path.realpath(staged) == str(j1 / "data")
    else:
        assert staged.is_dir() and not staged.is_symlink()
        assert (staged / "rho.dat").read_text().strip() == "CHARGE-DENSITY"
    # the hand-off never round-tripped through the flower host: second depends on first implicitly
    starts = {e["nodeId"]: e["seq"] for e in ff.events(eng, "node.started")}
    done = {e["nodeId"]: e["seq"] for e in ff.events(eng, "node.succeeded")}
    assert done["first"] < starts["second"]


def test_remote_dir_layout_with_remote_root_and_odd_chars(ff, tmp_path):
    root = tmp_path / "remote root with 'quote'"
    eng = ff.run(ff.plan([ff.job("a", OUT_X)], clusters={"c": ff.cluster(remote_root=str(root))}))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    jd = st.nodes["a"].result.outputs["job_dir"]
    assert jd == f"{root}/{st.run_id}/a/a1"
    assert (Path(jd) / ".flower" / "ec").read_text().strip() == "0"


# ====================================================================== contract

def test_declared_output_missing_is_contract_and_not_retried(ff):
    eng = ff.run(ff.plan([ff.job("a", OUT_X, outputs={"y": "string"}, retry={"max_attempts": 3, "backoff": "0s"})]))
    st = ff.drive(eng)
    assert st.status == "failed"
    assert ff.error_class(st, "a") == "contract"
    assert len(st.nodes["a"].attempts) == 1


def test_declared_file_missing_is_contract(ff):
    eng = ff.run(ff.plan([ff.job("a", OUT_X, files={"traj": "out/traj.xyz"})]))
    st = ff.drive(eng)
    assert ff.error_class(st, "a") == "contract"
    assert "traj" in st.nodes["a"].last.error["message"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_invalid_outputs_json_is_contract(ff, content):
    eng = ff.run(ff.plan([ff.job("a", f"printf '%s' '{content}' > \"$FLOWER_OUTPUTS\"")]))
    st = ff.drive(eng)
    assert st.status == "failed"
    assert ff.error_class(st, "a") == "contract"


# ====================================================================== failure classes

def test_exit_nonzero(ff):
    eng = ff.run(ff.plan([ff.job("a", "echo 'boom: SCF did not converge' >&2\nexit 3",
                                 retry={"max_attempts": 2, "backoff": "0s"})]))
    st = ff.drive(eng)
    assert st.status == "failed"
    err = st.nodes["a"].last.error
    assert err["error_class"] == "exit_nonzero"
    assert err["retryable"] is False and len(st.nodes["a"].attempts) == 1
    assert "SCF did not converge" in err["message"]
    assert err["details"]["ec"] == 3 and err["details"]["sched"] == "FAILED"
    assert ff.jobs()[0]["state"] == "FAILED"


def test_timeout_from_short_time_limit(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 30\n" + OUT_X, resources={"time": "0:00:02"},
                                 retry={"max_attempts": 2, "backoff": "0s"})]))
    st = ff.drive(eng, timeout=30)
    assert st.status == "failed"
    err = st.nodes["a"].last.error
    assert err["error_class"] == "timeout", err
    assert err["retryable"] is False and len(st.nodes["a"].attempts) == 1
    assert ff.jobs()[0]["state"] == "TIMEOUT"


@pytest.mark.parametrize("fault,cls", [("NODE_FAIL", "node_fail"), ("PREEMPTED", "preempted")])
def test_infra_failure_is_retried_with_new_submit_key(ff, fault, cls):
    ff.faults([{"match": "-a1$", "state": fault, "after_s": 0.3, "times": 1}])
    eng = ff.run(ff.plan([ff.job("a", "sleep 1.5\n" + OUT_X, retry={"max_attempts": 2, "backoff": "0s"})]))
    st = ff.drive(eng, timeout=40)
    assert st.status == "succeeded", ff.why(eng)
    a1, a2 = st.nodes["a"].attempts
    assert a1.status == "failed" and a1.error["error_class"] == cls and a1.error["retryable"] is True
    assert a2.status == "succeeded"
    k1, k2 = a1.job["submit_key"], a2.job["submit_key"]
    assert k1 != k2 and k1.endswith("-a1") and k2.endswith("-a2")
    assert a1.job["job_dir"] != a2.job["job_dir"]
    jobs = ff.jobs()
    assert [j["name"] for j in jobs] == [k1, k2]
    assert [j["state"] for j in jobs] == [fault, "COMPLETED"]
    assert len(ff.events(eng, "node.retry_scheduled")) == 1


def test_node_fail_without_retry_policy_fails(ff):
    ff.faults([{"match": ".", "state": "NODE_FAIL", "after_s": 0.2, "times": 5}])
    eng = ff.run(ff.plan([ff.job("a", "sleep 5")]))
    st = ff.drive(eng, timeout=30)
    assert st.status == "failed" and ff.error_class(st, "a") == "node_fail"
    assert len(ff.jobs()) == 1


def test_oom_is_not_retried(ff):
    ff.faults([{"match": ".", "state": "OUT_OF_MEMORY", "after_s": 0.2, "times": 1}])
    eng = ff.run(ff.plan([ff.job("a", "sleep 3\n" + OUT_X, retry={"max_attempts": 3, "backoff": "0s"})]))
    st = ff.drive(eng, timeout=30)
    assert st.status == "failed"
    err = st.nodes["a"].last.error
    assert err["error_class"] == "oom" and err["retryable"] is False
    assert len(st.nodes["a"].attempts) == 1 and len(ff.jobs()) == 1


def test_external_scancel_is_cancelled_ext(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 30")]))
    ff.drive(eng, until=lambda st: st.nodes["a"].last and st.nodes["a"].last.job.get("state") == "RUNNING")
    jid = ff.jobs()[0]["id"]
    subprocess.run([str(ff.bin / "scancel"), jid], check=True)
    st = ff.drive(eng, timeout=20)
    assert st.status == "failed"
    assert ff.error_class(st, "a") == "cancelled_ext"


# ====================================================================== cancel

def _wait_running(ff, eng, nid="a"):
    return ff.drive(eng, until=lambda st: st.nodes.get(nid) and st.nodes[nid].last
                    and st.nodes[nid].last.job.get("state") == "RUNNING", timeout=20)


def test_cancel_queued_job_node(ff, monkeypatch):
    monkeypatch.setenv("FAKESLURM_PEND_S", "60")
    eng = ff.run(ff.plan([ff.job("a", OUT_X)]))
    ff.drive(eng, until=lambda st: st.nodes.get("a") and st.nodes["a"].last
             and st.nodes["a"].last.job.get("state") == "QUEUED")
    st_jid = eng.state().nodes["a"].last.job["job_id"]
    eng.cancel(node="a", reason="test")
    st = ff.drive(eng, timeout=20)
    assert st.nodes["a"].status == "cancelled"
    assert st.status in ("failed", "cancelled")
    assert len(ff.events(eng, "job.cancel_requested")) == 1
    j = ff.jobs()[0]
    assert j["id"] == st_jid and j["state"] == "CANCELLED" and j["start"] is None  # never ran
    jd = Path(st.nodes["a"].last.job["job_dir"])
    assert (jd / ".flower" / "cancelled").exists()
    assert not (jd / "outputs.json").exists()


def test_cancel_running_job_node(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 60\n" + OUT_X)]))
    _wait_running(ff, eng)
    eng.cancel(node="a")
    st = ff.drive(eng, timeout=20)
    assert st.nodes["a"].status == "cancelled"
    j = ff.wait_job_state(ff.jobs()[0]["id"], "CANCELLED")
    assert j["start"] is not None


def test_cancel_run_with_running_job_scancels(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 60\n" + OUT_X), ff.job("b", OUT_X, needs=["a"])]))
    _wait_running(ff, eng)
    eng.cancel()
    st = ff.drive(eng, timeout=10)
    assert st.status == "cancelled"
    assert st.nodes["a"].status == "cancelled" and st.nodes["b"].status == "skipped"
    ff.wait_job_state(ff.jobs()[0]["id"], "CANCELLED")
    assert len(ff.jobs()) == 1


def test_cancel_before_submission(ff, clock):
    ff.replace_cmd("sbatch", 'echo "ssh: connect to host hpc port 22: Connection timed out" >&2; exit 255')
    eng = ff.run(ff.plan([ff.job("a", OUT_X)]))
    ff.drive(eng, until=lambda st: ff.events(eng, "job.remote_error"), timeout=10)
    assert not eng.state().nodes["a"].last.job.get("job_id")
    eng.cancel(node="a")
    clock.advance(10_000)  # even when a resubmit would be due, a cancelled attempt is never submitted
    ff.restore_cmd("sbatch")
    st = ff.drive(eng, timeout=10)
    assert st.nodes["a"].status == "cancelled"
    assert ff.jobs() == []


def test_cancel_run_before_submission(ff):
    ff.replace_cmd("sbatch", 'echo "Connection refused" >&2; exit 255')
    eng = ff.run(ff.plan([ff.job("a", OUT_X)]))
    ff.drive(eng, until=lambda st: ff.events(eng, "job.remote_error"), timeout=10)
    eng.cancel()
    st = ff.drive(eng, timeout=10)
    assert st.status == "cancelled" and st.nodes["a"].status == "cancelled"
    assert ff.jobs() == []


# ====================================================================== cluster limits / batching

def test_max_jobs_per_cluster(ff):
    nodes = [ff.job(f"j{i}", "sleep 0.6\n" + OUT_X) for i in range(3)]
    eng = ff.run(ff.plan(nodes, clusters={"c": ff.cluster(max_jobs=1)}))
    peak = 0

    def until(st):
        nonlocal peak
        peak = max(peak, sum(1 for ns in st.nodes.values() if ns.status == "running"))
        return st.status in ("succeeded", "failed")

    st = ff.drive(eng, until=until, timeout=60)
    assert st.status == "succeeded"
    assert peak == 1
    jobs = sorted(ff.jobs(), key=lambda j: j["submit"])
    assert len(jobs) == 3
    for prev, nxt in zip(jobs, jobs[1:]):
        assert nxt["submit"] >= prev["end"], "a job was submitted while another one still occupied the slot"


def test_poll_is_batched_per_cluster(ff):
    sq = ff.log_cmd("squeue")
    sa = ff.log_cmd("sacct")
    nodes = [ff.job(f"j{i}", "sleep 1.5\n" + OUT_X) for i in range(3)]
    eng = ff.run(ff.plan(nodes, clusters={"c": ff.cluster(min_poll="1s")}))
    t0 = time.time()
    st = ff.drive(eng, timeout=40, interval=0.05)
    elapsed = time.time() - t0
    assert st.status == "succeeded"
    polls = [l for l in sq.read_text().splitlines() if "%T" in l]
    lookups = [l for l in sq.read_text().splitlines() if "%j" in l]
    assert len(lookups) == 3  # one dedupe lookup per submission
    # rate-limited by min_poll even though we ticked every ~50 ms
    assert 1 <= len(polls) <= elapsed / 1.0 + 2, (len(polls), elapsed)
    ids = sorted(j["id"] for j in ff.jobs())
    sacct_polls = [l for l in sa.read_text().splitlines() if " -j " in l]
    assert sacct_polls and any(",".join(ids) in l for l in sacct_polls), sacct_polls


def test_two_clusters_polled_independently(ff, tmp_path):
    from flower.testing.fakeslurm import install
    bin2 = install(tmp_path / "fs2")
    clusters = {"c": ff.cluster(), "d": ff.cluster(bin_dir=str(bin2))}
    eng = ff.run(ff.plan([ff.job("a", OUT_X), {**ff.job("b", OUT_X), "cluster": "d"}], clusters=clusters))
    st = ff.drive(eng, timeout=30)
    assert st.status == "succeeded"
    assert len(ff.jobs()) == 1
    assert len(list((tmp_path / "fs2" / "state" / "jobs").glob("*.json"))) == 1


def test_foreach_job_children_have_valid_distinct_keys(ff):
    node = ff.job("sweep", 'printf \'{"v": %s}\' "$V" > "$FLOWER_OUTPUTS"', foreach=[1, 2, 3], env={"V": "${item}"})
    eng = ff.run(ff.plan([node]))
    st = ff.drive(eng, timeout=60)
    assert st.status == "succeeded", ff.why(eng)
    names = [j["name"] for j in ff.jobs()]
    assert len(set(names)) == 3
    assert sorted(o["v"] for o in st.nodes["sweep"].result.outputs["items"]) == [1, 2, 3]


# ====================================================================== CLI

def _cli(ff, *args, timeout=90, cwd=None):
    env = {**os.environ}
    r = subprocess.run([sys.executable, "-m", "flower", *args], capture_output=True, text=True, env=env,
                       timeout=timeout, cwd=cwd or ff.tmp)
    return r


def test_cli_fake_slurm_run_follow_status(ff, tmp_path):
    from flower.testing.fakeslurm import install
    bindir = str(install(tmp_path / "cli-fs"))
    assert Path(bindir, "sbatch").exists()
    plan = {"flower": 1, "id": "cli-job",
            "clusters": {"c": {"transport": "local", "bin_dir": bindir, "min_poll": "0.5s"}},
            "nodes": [{"id": "a", "kind": "shell", "cluster": "c", "run": OUT_X, "outputs": {"x": "integer"}}]}
    pf = tmp_path / "plan.yaml"
    pf.write_text(yaml.safe_dump(plan))
    r = _cli(ff, "run", str(pf), "-y", "--no-prompt", "--detach", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    rid = next(e for e in Path(ff.home, ".flower", "runs").iterdir()).name
    eng = Engine(RunPaths(ff.home, rid))
    r = _cli(ff, "status", rid, "--follow", "--timeout", "40", "--json")
    st = eng.state()
    assert st.status == "succeeded", (r.stdout, r.stderr)
    assert st.nodes["a"].result.outputs["x"] == 41
    r = _cli(ff, "status", rid, "--json", "--no-tick")
    assert r.returncode == 0 and rid in r.stdout


def test_cli_cancel_node(ff, tmp_path):
    plan = ff.plan([ff.job("a", "sleep 60")])
    eng = ff.run(plan)
    _wait_running(ff, eng)
    r = _cli(ff, "cancel", eng.paths.run_id, "--node", "a", "--json")
    assert r.returncode == 0, r.stderr
    st = eng.state()
    assert st.nodes["a"].status == "cancelled"
    ff.wait_job_state(ff.jobs()[0]["id"], "CANCELLED")
