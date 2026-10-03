"""``scheduler: none`` (run the payload directly on the cluster host) and shell / function nodes on a cluster.

The cluster here uses the ``local`` transport with a ``remote_root``, so the "remote" host is this machine; the
same code paths over ``ssh`` are covered in ``test_ssh.py`` with the fake OpenSSH client.
"""
from __future__ import annotations

import os
import signal
import sys
import textwrap
import time
from pathlib import Path

from flower.hpc import direct
from flower.hpc.transport import LocalTransport
from flower.plan import normalize, validate

OUT = 'printf \'{"x": 1}\' > "$FLOWER_OUTPUTS"\necho result > result.txt'


def _direct(ff, **kw) -> dict:
    c = {"transport": "local", "scheduler": "none", "remote_root": str(ff.tmp / "remote"),
         "min_poll": "0.2s", "lost_after": "1s"}
    c.update(kw)
    return {"c": c}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _wait(pred, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def _job_id(st, nid):
    ns = st.nodes.get(nid)
    return (ns.last.job or {}).get("job_id") if ns and ns.last else None


# ------------------------------------------------------------------ job nodes, scheduler: none

def test_direct_job_runs_detached_and_succeeds(ff):
    eng = ff.run(ff.plan([ff.job("a", "echo hello\n" + OUT, files={"res": "result.txt"}, outputs={"x": "integer"})],
                         clusters=_direct(ff)))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    r = st.nodes["a"].result
    jd = Path(r.outputs["job_dir"])
    assert jd == ff.tmp / "remote" / st.run_id / "a" / "a1"
    assert r.outputs["local_dir"] == str(jd)  # local transport: the files are already here
    assert (jd / "job.out").read_text().strip() == "hello"
    assert (jd / ".flower" / "ec").read_text().strip() == "0"
    assert r.outputs["job_id"] == (jd / ".flower" / "owner" / "id").read_text().strip()
    # declared outputs are the contract: the summary shows them rather than stray stdout
    assert r.summary.startswith(f"process {r.outputs['job_id']} completed · outputs: x=1")
    assert Path(r.files["res"]["path"]) == (jd / "result.txt").resolve()
    assert ff.jobs() == []  # no Slurm anywhere
    from flower.render import describe_event
    lines = [d for d in (describe_event(e) for e in eng.journal.read()) if d]
    assert f"a#1: process {r.outputs['job_id']} started" in lines
    assert any(l.startswith("a#1: process exited (exit code 0)") for l in lines)
    assert not [l for l in lines if "slurm" in l.lower()], "timeline talks about Slurm for a plain process"


def test_direct_nonzero_exit_reports_stderr(ff):
    eng = ff.run(ff.plan([ff.job("a", "echo boom >&2\nexit 3")], clusters=_direct(ff)))
    st = ff.drive(eng)
    assert st.status == "failed"
    err = st.nodes["a"].last.error
    assert err["error_class"] == "exit_nonzero" and "code 3" in err["message"] and "boom" in err["message"]


def test_direct_time_limit(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 60", resources={"time": "1s"})], clusters=_direct(ff)))
    st = ff.drive(eng, timeout=30)
    assert ff.error_class(st, "a") == "timeout", ff.why(eng)


def test_direct_cancel_kills_the_whole_process_tree(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 300 &\necho $! > child.pid\nwait")], clusters=_direct(ff)))
    st = ff.drive(eng, until=lambda s: _job_id(s, "a"))
    jd = Path(st.nodes["a"].last.job["job_dir"])
    assert _wait(lambda: (jd / "child.pid").exists() and (jd / "child.pid").read_text().strip())
    child = int((jd / "child.pid").read_text())
    leader = int(_job_id(st, "a"))
    assert _alive(child) and _alive(leader)
    eng.cancel(node="a", by="test:x")
    st = ff.drive(eng, timeout=30)
    assert st.nodes["a"].status == "cancelled", ff.why(eng)
    assert _wait(lambda: not _alive(child) and not _alive(leader), 15), "payload survived the cancel"


def test_direct_cancel_reaches_a_payload_under_a_time_limit(ff):
    """BUGS #24: with a time limit the payload runs under coreutils `timeout`, which puts it in a process group of
    its own; cancel used to signal only the leader's group and left the computation running."""
    eng = ff.run(ff.plan([ff.job("a", "sleep 300 &\necho $! > child.pid\nwait", timeout={"total": "10m"})],
                         clusters=_direct(ff)))
    st = ff.drive(eng, until=lambda s: _job_id(s, "a"))
    jd = Path(st.nodes["a"].last.job["job_dir"])
    assert _wait(lambda: (jd / "child.pid").exists() and (jd / "child.pid").read_text().strip())
    child = int((jd / "child.pid").read_text())
    assert os.getpgid(child) != int(_job_id(st, "a"))   # really in another process group
    eng.cancel(node="a", by="test:x")
    st = ff.drive(eng, timeout=30)
    assert st.nodes["a"].status == "cancelled", ff.why(eng)
    assert _wait(lambda: not _alive(child), 15), "the payload under `timeout` survived the cancel"


def test_direct_hard_killed_process_is_lost(ff):
    eng = ff.run(ff.plan([ff.job("a", "sleep 300", retry={"max_attempts": 1})], clusters=_direct(ff)))
    st = ff.drive(eng, until=lambda s: _job_id(s, "a"))
    pid = int(_job_id(st, "a"))
    os.killpg(pid, signal.SIGKILL)  # like a reboot: nothing gets to write an exit code
    st = ff.drive(eng, timeout=30)
    assert ff.error_class(st, "a") == "lost", ff.why(eng)
    assert ff.events(eng, "job.lost")


def test_direct_launch_is_idempotent_and_guarded(tmp_path):
    jd = tmp_path / "jd"
    jd.mkdir()
    (jd / "job.sh").write_text(direct.render_job_script(key="k", job_dir=str(jd), resources={}, env={},
                                                        prelude=None, modules=None))
    (jd / "user.sh").write_text("sleep 1\necho run >> ran.txt\n")
    tr = LocalTransport({})
    cmd = direct.submit_command(str(jd), "k", "fp", {})
    first = direct.parse_submit(tr.run(cmd).out)
    assert first and first[1] == "submitted"
    assert direct.parse_submit(tr.run(cmd).out) == (first[0], "existing")  # a retried launch re-attaches
    (jd / ".flower" / "jobid").unlink()  # crash after launch, before the id was recorded
    assert direct.parse_submit(tr.run(cmd).out) == (first[0], "existing")
    dup = tr.run(f"cd {jd} && bash job.sh")  # a second copy of the same attempt refuses to run
    assert dup.rc == 97
    assert _wait(lambda: (jd / ".flower" / "ec").exists(), 15)
    assert (jd / "ran.txt").read_text() == "run\n"  # the payload ran exactly once


# ------------------------------------------------------------------ shell / function nodes on a cluster

def test_shell_node_on_cluster_runs_in_the_remote_job_dir(ff):
    eng = ff.run(ff.plan([
        {"id": "s", "kind": "shell", "cluster": "c", "env": {"MYV": "7"}, "prelude": ["export EXTRA=5"],
         "run": 'echo "{\\"where\\": \\"$PWD\\", \\"v\\": $((MYV + EXTRA))}" > "$FLOWER_OUTPUTS"',
         "outputs": {"where": "string", "v": "integer"}},
        {"id": "after", "kind": "shell", "run": 'echo "got ${s.outputs.v}"'},
    ], clusters=_direct(ff)))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    out = st.nodes["s"].result.outputs
    assert out["v"] == 12 and out["where"] == out["job_dir"]
    assert st.nodes["after"].result.summary == "got 12"
    from flower.render import kind_label
    assert kind_label(st.graph().nodes["s"]) == "shell@c"


def test_shell_node_on_cluster_uses_strict_shell_semantics(ff):
    eng = ff.run(ff.plan([{"id": "s", "kind": "shell", "cluster": "c", "run": 'echo "$UNSET_VAR_XYZ"'}],
                         clusters=_direct(ff)))
    st = ff.drive(eng)
    assert ff.error_class(st, "s") == "exit_nonzero"  # set -u, as for a local shell node


def test_shell_node_on_slurm_cluster(ff):
    eng = ff.run(ff.plan([{"id": "s", "kind": "shell", "cluster": "c", "run": OUT, "outputs": {"x": "integer"}}]))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    assert len(ff.jobs()) == 1


def test_function_node_on_cluster_ships_its_package(ff, tmp_path):
    src = tmp_path / "plansrc"
    (src / "sci").mkdir(parents=True)
    (src / "sci" / "__init__.py").write_text("")
    (src / "sci" / "helpers.py").write_text("K = 3\n")
    (src / "sci" / "api.py").write_text(textwrap.dedent("""
        import os
        from sci.helpers import K
        def f(x, ctx):
            with open(os.path.join(ctx["workdir"], "plot.txt"), "w") as fh:
                fh.write("p")
            return {"v": x * K, "cwd": os.getcwd(), "workdir": ctx["workdir"], "summary": f"v={x * K}"}
    """))
    eng = ff.run(ff.plan([{"id": "f", "kind": "function", "cluster": "c", "call": "sci.api:f", "args": {"x": 5},
                           "python": sys.executable, "outputs": {"v": "integer"}, "files": {"plot": "plot.txt"}}],
                         clusters=_direct(ff), _source={"dir": str(src)}))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    r = st.nodes["f"].result
    assert r.outputs["v"] == 15 and r.summary == "v=15"
    assert r.outputs["cwd"] == r.outputs["workdir"] == r.outputs["job_dir"]
    assert (Path(r.outputs["job_dir"]) / ".flower" / "code" / "sci" / "helpers.py").exists()
    assert Path(r.files["plot"]["path"]).read_text() == "p"


def test_function_node_on_cluster_can_call_an_installed_module(ff):
    eng = ff.run(ff.plan([{"id": "f", "kind": "function", "cluster": "c", "call": "json:dumps",
                           "args": {"obj": [1, 2]}, "python": sys.executable}], clusters=_direct(ff)))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    assert st.nodes["f"].result.outputs["result"] == "[1, 2]"


def test_function_node_on_cluster_reports_the_traceback(ff, tmp_path):
    src = tmp_path / "plansrc"
    src.mkdir()
    (src / "bad.py").write_text("def f():\n    raise ValueError('nope 42')\n")
    eng = ff.run(ff.plan([{"id": "f", "kind": "function", "cluster": "c", "call": "bad:f", "python": sys.executable}],
                         clusters=_direct(ff), _source={"dir": str(src)}))
    st = ff.drive(eng)
    err = st.nodes["f"].last.error
    assert err["error_class"] == "exit_nonzero" and "nope 42" in err["message"]


def test_cluster_nodes_do_not_count_against_local_concurrency(ff):
    eng = ff.run(ff.plan([{"id": f"s{i}", "kind": "shell", "cluster": "c", "run": "sleep 2"} for i in range(3)],
                         clusters=_direct(ff), defaults={"concurrency": 1}))
    eng.tick()
    st = eng.state()
    assert [st.nodes[f"s{i}"].status for i in range(3)] == ["running"] * 3
    assert ff.drive(eng, timeout=30).status == "succeeded"


# ------------------------------------------------------------------ validation

def _issues(nodes, clusters=None):
    raw = {"flower": 1, "id": "v", "clusters": clusters if clusters is not None else
           {"c": {"transport": "local", "scheduler": "none"}}, "nodes": nodes}
    return [(i["path"], i["message"]) for i in validate(normalize(raw))]


def test_cluster_node_validation():
    assert _issues([{"id": "s", "kind": "shell", "cluster": "c", "run": "true"},
                    {"id": "f", "kind": "function", "cluster": "c", "call": "m:f"}]) == []
    assert any("unknown scheduler" in m for _, m in _issues([{"id": "s", "kind": "shell", "run": "true"}],
                                                            {"c": {"scheduler": "pbs"}}))
    assert any(p.endswith(".cluster") and "unknown cluster" in m
               for p, m in _issues([{"id": "s", "kind": "shell", "cluster": "nope", "run": "true"}]))
    assert any(p.endswith(".stage_in") for p, _ in _issues([{"id": "s", "kind": "shell", "run": "true",
                                                              "stage_in": ["x"]}]))
    assert any(p.endswith(".cwd") for p, _ in _issues([{"id": "s", "kind": "shell", "cluster": "c", "run": "true",
                                                         "cwd": "elsewhere"}]))


RESUME = """\
if [ -f "$FLOWER_STATE_DIR/ckpt" ]; then
  echo "{\\"resumed_from\\": $(cat "$FLOWER_STATE_DIR/ckpt")}" > "$FLOWER_OUTPUTS"
else
  echo 41 > "$FLOWER_STATE_DIR/ckpt"; exit 7     # a crash after writing a checkpoint
fi
"""


def _resume_plan(ff, kind):
    retry = {"max_attempts": 3, "on": ["exit_nonzero"], "backoff": "0s"}
    if kind == "cluster":
        node = ff.job("a", RESUME, retry=retry, outputs={"resumed_from": "integer"})
        return ff.plan([node], clusters=_direct(ff))
    return ff.plan([{"id": "a", "kind": "shell", "run": RESUME, "retry": retry, "outputs": {"resumed_from": "integer"}}])


def test_state_dir_survives_retries_and_a_rerun_starts_fresh(ff):
    """FLOWER_STATE_DIR: a retried attempt resumes from its predecessor's checkpoint (cluster and local steps);
    a deliberate rerun starts a new, empty state directory."""
    for kind in ("cluster", "local"):
        eng = ff.run(_resume_plan(ff, kind))
        st = ff.drive(eng, timeout=60)
        a = st.nodes["a"]
        assert a.status == "succeeded" and a.result.outputs["resumed_from"] == 41, (kind, ff.why(eng))
        assert len(a.attempts) == 2, kind
        eng.rerun("a", by="test:x")
        st = ff.drive(eng, timeout=60)
        a = st.nodes["a"]
        assert a.status == "succeeded" and len(a.attempts) == 4, (kind, ff.why(eng))   # fresh: failed once again


def test_a_retry_keeps_its_slot_and_goes_first(ff):
    """With max_jobs = 1, a lost job's retry must run before the next fresh item (it resumes work under way)."""
    nodes = [ff.job("a", 'if [ ! -f "$FLOWER_STATE_DIR/once" ]; then touch "$FLOWER_STATE_DIR/once"; sleep 300; fi; '
                         'date +%s%N > "$FLOWER_STATE_DIR/../done_at"',
                    retry={"max_attempts": 2, "backoff": "1s"}),
             ff.job("b", "sleep 0.2")]
    eng = ff.run(ff.plan(nodes, clusters=_direct(ff, max_jobs=1)))
    st = ff.drive(eng, until=lambda s: _job_id(s, "a"))
    os.killpg(int(_job_id(st, "a")), signal.SIGKILL)          # a crash: lost, then retried
    st = ff.drive(eng, timeout=60)
    a, b = st.nodes["a"], st.nodes["b"]
    assert a.status == "succeeded" and b.status == "succeeded", ff.why(eng)
    assert a.attempts[-1].started_at <= b.attempts[-1].started_at, "fresh work took the retry's slot"


def test_tmpdir_and_resources_reach_the_payload(ff):
    node = ff.job("a", 'echo "{\\"tmp\\": \\"$TMPDIR\\", \\"mb\\": \\"$FLOWER_MEM_MB\\", \\"cpus\\": \\"$FLOWER_CPUS\\"}" '
                       '> "$FLOWER_OUTPUTS"', tmpdir="job", resources={"mem": "2G", "cpus_per_task": 4},
                  outputs={"tmp": "string", "mb": "string", "cpus": "string"})
    eng = ff.run(ff.plan([node], clusters=_direct(ff)))
    st = ff.drive(eng, timeout=60)
    o = st.nodes["a"].result.outputs
    assert o["tmp"].endswith("/a1/tmp") and Path(o["tmp"]).is_dir(), o
    assert o["mb"] == "2048" and o["cpus"] == "4"


def test_cpu_budget_is_shared_by_runs_on_the_same_machine(ff):
    """Two studies on one workstation (no batch system): `cpus` on the cluster is a budget for the machine, shared by
    all runs of the project. The second run's job waits until the first's frees its cores."""
    cl = _direct(ff, cpus=2)
    job = ff.job("a", "sleep 2", resources={"cpus_per_task": 2})
    e1 = ff.run(ff.plan([job], clusters=cl))
    e2 = ff.run(ff.plan([job], clusters=cl))
    ff.drive(e1, until=lambda s: _job_id(s, "a"))
    rep = e2.tick()
    assert e2.state().nodes["a"].status == "pending", "started although the machine's cpus were in use"
    assert any("cpus in use" in w for w in rep.waiting_on), rep.waiting_on
    assert ff.drive(e1, timeout=60).status == "succeeded"
    assert ff.drive(e2, timeout=60).status == "succeeded", ff.why(e2)
    left = list((e1.paths.root / ".flower" / "usage").rglob("*.json"))
    assert left == [], f"usage files left after the runs ended: {left}"
