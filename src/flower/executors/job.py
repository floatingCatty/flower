"""Shell steps that name a ``cluster:``.

The cluster's ``scheduler`` decides how the payload is started: ``slurm`` (sbatch, the default) or ``none``
(a detached process straight on the host, see :mod:`flower.hpc.direct`). Its ``transport`` decides where:
``local`` or ``ssh``. The step's ``run`` becomes the payload.

Lifecycle per attempt (all transitions journalled as ``job.*`` events):

  submit_intent (write-ahead) -> resolve job dir -> stage files (job.staged) -> dedupe+sbatch in ONE
  remote call (job.submitted) -> observed QUEUED/RUNNING (only on change) -> EXITED (job.exited)
  -> retrieve (job.retrieved) -> verdict

Nothing waits on the job: every ``tick`` polls all of a cluster's jobs in one round-trip (rate-limited by
``min_poll``, backed off on errors). Safety rules (context/notes/hpc-execution.md §8, tests/hpc/BUGS.md):

* nothing remote happens before ``job.submit_intent`` is in the journal; a crash anywhere re-attaches
  via the job-id file, the un-renamed sbatch output, the in-job owner record or the job name;
* we never submit while ``squeue`` cannot answer (a duplicate is worse than a delay);
* a poll that failed or came back truncated never changes a verdict and never counts as a "miss";
* infrastructure trouble (ssh drops, slurmctld hiccups, a failed download) is retried with backoff and
  never consumes a job attempt; permanent rejections (bad partition, missing input) fail at once.
"""
from __future__ import annotations

import json
import os
import posixpath
import shlex
import time
from pathlib import Path

from ..hpc import scheduler_for, slurm
from ..hpc.transport import CmdResult, make_transport
from ..rundir import fs_name
from ..util import (FlowerError, atomic_write_json, atomic_write_text, digest, first_line,
                    fmt_bytes, parse_duration, read_json, tail_text)
from .base import (scratch_env, RETRYABLE_DEFAULT, Executor, NodeCtx, Outcome, check_outputs, collect_files, mem_mb,
                   printed_outputs)

MAX_REMOTE_ERRORS = 8
RETRIEVE_LIMIT = "5G"   # what one job may bring back unless its step says `retrieve_limit:`
POLL_BACKOFF = (30.0, 300.0, 1200.0)


def _cluster(ctx: NodeCtx) -> dict:
    name = ctx.node["cluster"]
    c = dict((ctx.plan.get("clusters") or {}).get(name) or {})
    c["_name"] = name
    return c


def _cmds(cluster: dict) -> dict:
    cmds = {k: k for k in ("sbatch", "squeue", "sacct", "scancel")}
    bindir = cluster.get("bin_dir")
    for k in cmds:
        v = (cluster.get("commands") or {}).get(k)
        if v:
            cmds[k] = v
        elif bindir:
            cmds[k] = os.path.join(bindir, k)
    return cmds


def _backoff(n: int) -> float:
    return min(1200.0, 30.0 * 2 ** max(0, n - 1))


def _payload(node: dict) -> str:
    """The user payload (``user.sh``): the step's shell command."""
    return "set -euo pipefail\n" + str(node["run"]) + "\n"


def _resources(cluster: dict, node: dict) -> dict:
    res = dict(cluster.get("resources") or {})
    res.update(node.get("resources") or {})
    if not res.get("time"):   # the job's time limit is the step's own (minutes: a format every scheduler takes)
        total = parse_duration((node.get("timeout") or {}).get("total"))
        if total:
            res["time"] = total / 60
    return res


class _Remote(Exception):
    def __init__(self, result: CmdResult, op: str = "submit"):
        super().__init__(result.err)
        self.result = result
        self.op = op


class JobExecutor(Executor):
    kind = "job"

    # ------------------------------------------------------------ start / submit
    def start(self, ctx: NodeCtx) -> dict:
        cluster = _cluster(ctx)
        key = slurm.submit_key(ctx.run_id, ctx.node["id"], ctx.attempt)
        resources = _resources(cluster, ctx.node)
        scheduler_for(cluster).validate_resources(resources)  # e.g. newline injection, bad extra flags
        user = _payload(ctx.node)
        fingerprint = digest({"user": user, "inputs": ctx.inputs, "resources": resources,
                              "attempt": ctx.attempt})[7:23]
        tr = make_transport(cluster)
        try:
            job_dir = self._job_dir(ctx, cluster, tr)
        except _Remote:
            job_dir = None  # cluster unreachable right now: resolved later inside the retried submit step
        ctx.emit("job.submit_intent", {"submit_key": key, "fingerprint": fingerprint, "cluster": cluster["_name"],
                                       "scheduler": scheduler_for(cluster).NAME,
                                       "resources": resources, "job_dir": job_dir})
        ctx.job.update({"submit_key": key, "fingerprint": fingerprint, "cluster": cluster["_name"],
                        "resources": resources, "job_dir": job_dir})
        failure = self._submit(ctx, cluster, tr)
        if failure is not None:
            # surfaced through poll on the next tick (keeps start() side-effect free of verdicts)
            atomic_write_json(ctx.attempt_dir / "submit_failure.json", failure.__dict__)
        return {"cluster": cluster["_name"], "submit_key": key}

    def _job_dir(self, ctx: NodeCtx, cluster: dict, tr) -> str:
        if ctx.job.get("job_dir"):
            return ctx.job["job_dir"]
        root = cluster.get("remote_root")
        if cluster.get("transport", "local") == "local" and not root:
            return str(ctx.attempt_dir / "job")
        root = root or "~/flower-runs"
        if root.startswith("~"):
            if tr.is_local:
                home = os.path.expanduser("~")
            else:  # asked once per run and cluster (one ssh round trip saved on every later attempt)
                cache = ctx.run_dir / "cache" / f"home-{fs_name(cluster['_name'])}.json"
                home = (read_json(cache, {}) or {}).get("home")
                if not home:
                    r = tr.run("echo $HOME", timeout=60)
                    if r.rc != 0 or not r.out.strip():
                        raise _Remote(r, "connect")
                    home = r.out.strip().splitlines()[-1]
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(cache, {"home": home})
            root = home + root[1:]
        return f"{root.rstrip('/')}/{ctx.run_id}/{fs_name(ctx.node['id'])}/a{ctx.attempt}"

    @staticmethod
    def _state_dir(ctx: NodeCtx, job_dir: str) -> str:
        """FLOWER_STATE_DIR on the cluster: next to the attempt directories, one per series of retries."""
        if job_dir == str(ctx.attempt_dir / "job"):   # local cluster without remote_root: in the run directory
            return str(ctx.state_dir)
        return f"{posixpath.dirname(job_dir.rstrip('/'))}/state-{ctx.series}"

    def _render(self, ctx: NodeCtx, cluster: dict, job_dir: str) -> None:
        stage = ctx.attempt_dir / "stage"
        stage.mkdir(parents=True, exist_ok=True)
        env = {"FLOWER_RUN_ID": ctx.run_id, "FLOWER_NODE_ID": ctx.node["id"], "FLOWER_ATTEMPT": ctx.attempt}
        for k, v in (ctx.inputs or {}).items():
            if isinstance(v, (str, int, float, bool)) and k.replace("_", "").isalnum():
                env[f"FLOWER_IN_{k.upper()}"] = v
        for k, v in (ctx.node.get("env") or {}).items():
            env[str(k)] = v if isinstance(v, str) else json.dumps(v)
        res = {**(ctx.node.get("resources") or {}), **(ctx.job.get("resources") or {})}
        for k, v in scratch_env({**ctx.node, "resources": res, "tmpdir": None}).items():
            env.setdefault(k, v)
        np = ctx.node.get("prelude")
        prelude = list(cluster.get("prelude") or []) + (list(np) if isinstance(np, list) else ([np] if np else []))
        modules = list(cluster.get("modules") or [])
        script = scheduler_for(cluster).render_job_script(key=ctx.job["submit_key"], job_dir=job_dir,
                                                          resources=ctx.job.get("resources") or {}, env=env,
                                                          prelude=prelude, modules=modules,
                                                          state_dir=self._state_dir(ctx, job_dir),
                                                          tmpdir=ctx.node.get("tmpdir"))
        atomic_write_text(stage / "job.sh", script)
        atomic_write_text(stage / "user.sh", _payload(ctx.node))
        atomic_write_json(stage / "inputs.json", ctx.inputs)
        atomic_write_json(stage / "submit.json", {"run_id": ctx.run_id, "node": ctx.node["id"], "attempt": ctx.attempt,
                                                  "submit_key": ctx.job["submit_key"],
                                                  "fingerprint": ctx.job["fingerprint"]})

    def _stage(self, ctx: NodeCtx, cluster: dict, tr, job_dir: str) -> None:
        """Create the job directory with everything the payload needs, in as few round trips as possible.

        The directory's initial content (generated scripts, local
        ``stage_in`` files as symlinks to their sources) is assembled under ``upload/`` and sent in ONE
        transfer that also creates the directory; cluster-side ``stage_in`` (``remote:`` sources, links on
        a local cluster) runs as ONE command after it. Over ssh that is 2 connections instead of one per file.
        """
        import shutil
        self._render(ctx, cluster, job_dir)
        stage = ctx.attempt_dir / "stage"
        up = ctx.attempt_dir / "upload"
        if up.exists():
            shutil.rmtree(up)
        (up / ".flower").mkdir(parents=True)
        for name in ("job.sh", "user.sh", "inputs.json"):
            shutil.copy2(stage / name, up / name)
        shutil.copy2(stage / "submit.json", up / ".flower" / "submit.json")
        q = shlex.quote
        on_cluster: list[str] = []
        for item in ctx.node.get("stage_in") or []:
            item = {"from": item} if isinstance(item, str) else item
            src = str(item.get("from"))
            to = str(item.get("to") or os.path.basename(src.rstrip("/")))
            mode = item.get("mode", "copy")
            if src.startswith("remote:"):  # cluster-side hand-off between jobs (no download/upload round trip)
                rsrc = src[len("remote:"):]
                op = "ln -sfn" if mode == "link" else "cp -r"
                on_cluster.append(f"test -e {q(rsrc)} || {{ echo 'stage_in source {rsrc} does not exist' >&2; exit 66; }}; "
                                  f"{op} {q(rsrc)} {q(job_dir + '/' + to)}")
                continue
            if not Path(src).exists():
                raise FlowerError("stage_in", f"stage_in source {src} does not exist")
            if tr.is_local and mode == "link":
                on_cluster.append(f"ln -sfn {q(os.path.abspath(src))} {q(job_dir + '/' + to)}")
                continue
            dst = up / to
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            os.symlink(os.path.abspath(src), dst)  # the transfer follows it: no local copy of the data
        try:
            r = tr.put_tree(up, job_dir)
        except OSError as exc:
            raise FlowerError("stage_in", f"could not stage files: {exc}") from None
        if r.rc != 0:
            raise _Remote(r, "stage")
        if on_cluster:
            r = tr.run("set -e; mkdir -p " + q(job_dir) + "; " + "; ".join(on_cluster), timeout=600)
            if r.rc == 66:
                raise FlowerError("stage_in", (r.err or "a stage_in source does not exist on the cluster").strip()[-300:])
            if r.rc != 0:
                raise _Remote(r, "stage")

    def _submit(self, ctx: NodeCtx, cluster: dict, tr=None) -> Outcome | None:
        """Try to get this attempt submitted. Returns a failure Outcome only for permanent problems."""
        tr = tr or make_transport(cluster)
        try:
            job_dir = self._job_dir(ctx, cluster, tr)
            self._stage(ctx, cluster, tr, job_dir)
            if ctx.job.get("job_dir") != job_dir:
                ctx.emit("job.staged", {"job_dir": job_dir})
                ctx.job["job_dir"] = job_dir
            sched = scheduler_for(cluster)
            r = tr.run(sched.submit_command(job_dir, ctx.job["submit_key"], ctx.job["fingerprint"], _cmds(cluster)),
                       timeout=180)
            got = sched.parse_submit(r.out) if r.rc == 0 else None
            if not got:
                raise _Remote(r, "submit")
        except _Remote as exc:
            res = exc.result
            msg = (res.err or res.out or f"exit {res.rc}").strip()[-400:]
            if res.rc == 127:   # a command is missing (rsync, ssh, sbatch): waiting will not bring it
                ctx.emit("job.remote_error", {"op": exc.op, "error": msg, "transient": False})
                return Outcome.fail(exc.op, f"command not found ({exc.op}): {first_line(msg, 300)}", retryable=False,
                                    details={"stderr": msg, "hint": "install rsync/ssh here, or the scheduler's "
                                                                    "commands on the cluster"})
            if not res.transient and exc.op == "submit":
                ctx.emit("job.remote_error", {"op": "submit", "error": msg, "transient": False})
                what = "sbatch rejected the job" if scheduler_for(cluster).NAME == "slurm" else "could not start the process"
                return Outcome.fail("submit", f"{what}: {first_line(msg, 300)}", retryable=False,
                                    details={"stderr": msg})
            n = int(ctx.job.get("remote_errors") or 0) + 1
            retry_at = time.time() + _backoff(n)
            ctx.emit("job.remote_error", {"op": exc.op, "error": msg, "transient": res.transient, "retry_at": retry_at})
            ctx.job["remote_errors"] = n
            ctx.job["last_remote_error"] = {"op": exc.op, "error": msg, "retry_at": retry_at}
            return None
        except FlowerError as exc:
            return Outcome.fail(exc.code, exc.message, retryable=exc.code in RETRYABLE_DEFAULT)
        except (OSError, ValueError) as exc:
            return Outcome.fail("stage", f"{type(exc).__name__}: {exc}", retryable=False)
        job_id, via = got
        ctx.emit("job.submitted", {"job_id": job_id, "via": via, "scheduler": scheduler_for(cluster).NAME})
        ctx.job.update({"job_id": job_id, "state": "QUEUED"})
        return None

    # ------------------------------------------------------------ polling
    def poll(self, ctx: NodeCtx) -> Outcome | None:
        return self.poll_many([ctx])[0]

    def poll_many(self, ctxs: list[NodeCtx]) -> list[Outcome | None]:
        results: list[Outcome | None] = [None] * len(ctxs)
        by_cluster: dict[str, list[int]] = {}
        for i, ctx in enumerate(ctxs):
            by_cluster.setdefault(ctx.node["cluster"], []).append(i)
        for name, idxs in by_cluster.items():
            try:
                self._poll_cluster(name, [ctxs[i] for i in idxs], results, idxs)
            except Exception as exc:  # noqa: BLE001 - never let one cluster wedge the tick
                for i in idxs:
                    if results[i] is None:
                        ctxs[i].emit("job.remote_error", {"op": "internal", "error": f"{type(exc).__name__}: {exc}",
                                                          "transient": True})
        return results

    def _poll_cluster(self, name: str, ctxs: list[NodeCtx], results: list, idxs: list[int]) -> None:
        cluster = _cluster(ctxs[0])
        tr = make_transport(cluster)
        live: list[tuple[int, NodeCtx]] = []
        for i, ctx in zip(idxs, ctxs):
            try:
                out = self._pre_submit(ctx, cluster, tr)
            except Exception as exc:  # noqa: BLE001
                out = Outcome.fail("internal", f"{type(exc).__name__}: {exc}", retryable=True)
            if out is not None:
                results[i] = out
            elif ctx.job.get("job_id"):
                live.append((i, ctx))
        if not live:
            return
        cache_path = ctxs[0].run_dir / "cache" / f"cluster-{fs_name(name)}.json"
        cache = read_json(cache_path, {}) or {}
        sched = scheduler_for(cluster)
        min_poll = parse_duration(cluster.get("min_poll")) or sched.DEFAULT_MIN_POLL
        fdir = ctxs[0].run_dir / "follow"
        if fdir.is_dir() and any(fdir.glob("*.json")):  # someone is watching (flower rerun --follow)
            min_poll = min(min_poll, 0.5)
        nowt = time.time()
        if nowt - float(cache.get("last_poll", 0)) < min_poll or nowt < float(cache.get("retry_at", 0)):
            return
        jobs = [(ctx.job["job_id"], ctx.job["job_dir"]) for _, ctx in live]
        r = tr.run(sched.poll_command(jobs, _cmds(cluster)), timeout=120)
        cache["last_poll"] = time.time()
        poll = sched.parse_poll(r.out) if r.out else None
        if r.rc != 0 or poll is None or not poll.get("complete"):
            n = int(cache.get("poll_errors", 0)) + 1
            delay = min(POLL_BACKOFF[-1], min_poll * 10 ** (n - 1))  # 30 s -> 300 s -> 1200 s at min_poll 30 s
            cache.update({"poll_errors": n, "retry_at": time.time() + delay,
                          "last_error": (r.err or r.out or f"exit {r.rc}").strip()[-300:]})
            atomic_write_json(cache_path, cache)
            if n == 1 or n % 10 == 0:  # don't flood the journal during a long outage
                for _, ctx in live:
                    ctx.emit("job.remote_error", {"op": "poll", "error": cache["last_error"], "transient": True,
                                                  "consecutive": n, "next_poll_in_s": delay})
            return  # a failed poll never changes a verdict
        cache.update({"poll_errors": 0, "retry_at": 0})
        cache.pop("last_error", None)
        atomic_write_json(cache_path, cache)
        for i, ctx in live:
            tail = (poll.get("evidence") or {}).get(str(ctx.job["job_id"]), {}).get("tail")
            if tail:   # what the job printed last: shown by `flower status` while it runs (a file, not the journal)
                try:
                    (ctx.attempt_dir / "live.txt").write_text(tail)
                except OSError:
                    pass
            try:
                results[i] = self._advance(ctx, cluster, tr, poll)
            except Exception as exc:  # noqa: BLE001
                ctx.emit("job.remote_error", {"op": "advance", "error": f"{type(exc).__name__}: {exc}", "transient": True})

    def _pre_submit(self, ctx: NodeCtx, cluster: dict, tr) -> Outcome | None:
        """Handle attempts that are not (yet) known to Slurm: crash recovery, resubmission, cancel."""
        if ctx.job.get("job_id"):
            return None
        if not ctx.job.get("submit_key"):
            # crashed between node.started and job.submit_intent: nothing remote can exist yet (write-ahead)
            self.start(ctx)
            return self._stored_failure(ctx)
        stored = self._stored_failure(ctx)
        if stored is not None:
            return stored
        if (ctx.attempt_dir / "stage" / "cancel").exists():
            self._cancel_remote(ctx, cluster, tr)
            return Outcome(status="cancelled", error_class="cancelled", message="cancelled before the job started")
        err = ctx.job.get("last_remote_error") or {}
        if err.get("retry_at") and time.time() < float(err["retry_at"]):
            return None
        n = int(ctx.job.get("remote_errors") or 0)
        if n >= MAX_REMOTE_ERRORS:
            return Outcome.fail("remote", f"could not submit after {n} tries: {first_line(err.get('error'), 200)}",
                                retryable=True)
        return self._submit(ctx, cluster, tr)

    def _stored_failure(self, ctx: NodeCtx) -> Outcome | None:
        p = ctx.attempt_dir / "submit_failure.json"
        data = read_json(p)
        if not data:
            return None
        p.unlink()
        return Outcome(**data)

    def _advance(self, ctx: NodeCtx, cluster: dict, tr, poll: dict) -> Outcome | None:
        jid = ctx.job["job_id"]
        sched = scheduler_for(cluster)
        obs = sched.observe(jid, poll)
        prev = ctx.job.get("state")
        # duplicate guard / orphan: the attempt's owner is another job id -> follow the owner
        owner = obs.get("owner")
        if owner and owner != jid:
            ctx.emit("job.orphan_detected", {"tracked": jid, "owner": owner,
                                             "action": "following the job that owns the attempt directory"})
            ctx.emit("job.submitted", {"job_id": owner, "via": "owner", "scheduler": sched.NAME})
            ctx.job.update({"job_id": owner, "state": "QUEUED"})
            return None
        if obs["state"] == "UNKNOWN":
            if not obs.get("poll_ok") or (not poll.get("sacct_ok") and cluster.get("accounting", True)):
                return None  # the scheduler could not answer: not evidence of anything
            track = read_json(ctx.attempt_dir / "job_poll.json", {}) or {}
            track["misses"] = int(track.get("misses", 0)) + 1
            track.setdefault("first_miss", time.time())
            atomic_write_json(ctx.attempt_dir / "job_poll.json", track)
            grace = parse_duration(cluster.get("lost_after")) or sched.DEFAULT_LOST_AFTER
            if track["misses"] >= 5 and time.time() - track["first_miss"] > grace:
                if sched.NAME == "none":
                    why = "process died without writing its exit code (killed hard or the machine rebooted)"
                    if obs.get("missing_dir"):
                        why = "attempt directory is gone"
                else:
                    why = "job vanished from squeue and sacct without an exit record"
                    if obs.get("missing_dir"):
                        why = "job directory is gone and Slurm has no record"
                    elif obs.get("started"):
                        why = "job started but died without writing its exit code (node failure or hard kill)"
                tr.run(sched.kill_command(jid, _cmds(cluster)), timeout=60)
                ctx.emit("job.lost", {"why": why, "misses": track["misses"]})
                return Outcome.fail("lost", f"job {jid} lost: {why}", retryable=True)
            return None
        if (ctx.attempt_dir / "job_poll.json").exists():
            (ctx.attempt_dir / "job_poll.json").unlink()
        if obs["state"] != prev:
            ctx.emit("job.observed", {"state": obs["state"], "raw": obs.get("sched"), "source": obs.get("source"),
                                      "scheduler": sched.NAME})
            ctx.job["state"] = obs["state"]
        if obs["state"] != "EXITED":
            return None
        ctx.emit("job.exited", {"ec": obs.get("ec"), "final": obs.get("final"), "exit": obs.get("exit"),
                                "scheduler": sched.NAME,
                                "elapsed": obs.get("elapsed")}, key=f"job.exited:{ctx.node['id']}#a{ctx.attempt}")
        status, cls, msg, retry = sched.verdict(obs)
        return self._collect(ctx, cluster, tr, obs, status, cls, msg, retry)

    def _collect(self, ctx: NodeCtx, cluster: dict, tr, obs: dict, status: str, cls, msg, retry) -> Outcome | None:
        job_dir = ctx.job["job_dir"]
        if tr.is_local:
            local = Path(job_dir)
        else:
            local = ctx.attempt_dir / "job"
            pats = ["outputs.json", *scheduler_for(cluster).retrieve_patterns(), ".flower/*"] \
                + list(ctx.node.get("retrieve") or []) \
                + [str(v) for v in (ctx.node.get("files") or {}).values()]
            if not ctx.job.get("retrieve_sized"):   # a broad `retrieve:` must not pull gigabytes unannounced
                limit_mb = mem_mb(ctx.node.get("retrieve_limit") or cluster.get("retrieve_limit") or RETRIEVE_LIMIT)
                size = tr.size(job_dir, pats)
                if size is not None and limit_mb and size > limit_mb * 1024 * 1024:
                    return Outcome.fail("retrieve_limit", f"the files to fetch come to {fmt_bytes(size)}, over the "
                                        f"limit of {fmt_bytes(limit_mb * 2 ** 20)}: nothing was fetched; they are on "
                                        f"{cluster.get('_name') or ctx.node.get('cluster')} in {job_dir}",
                                        retryable=False, details={"job_dir": job_dir, "bytes": size,
                                        "hint": "narrow `retrieve:`, or raise it for this step: retrieve_limit: 20G"})
                ctx.job["retrieve_sized"] = True
            r = tr.get(job_dir, local, pats)
            if r.rc != 0:
                n = int(ctx.job.get("retrieve_errors") or 0) + 1
                ctx.emit("job.remote_error", {"op": "retrieve", "error": (r.err or "")[-300:], "transient": r.transient,
                                              "consecutive": n})
                ctx.job["retrieve_errors"] = n
                if n >= MAX_REMOTE_ERRORS:
                    return Outcome.fail("remote", f"could not retrieve outputs after {n} tries: "
                                        f"{(r.err or '').strip()[-200:]}", retryable=False,
                                        details={"job_dir": job_dir, "note": "results are still on the cluster"})
                return None  # the job's results are safe on the cluster; try the download again later
            ctx.emit("job.retrieved", {"to": str(local)}, key=f"job.retrieved:{ctx.node['id']}#a{ctx.attempt}")
        jid = ctx.job["job_id"]
        sched = scheduler_for(cluster)
        out_name, err_name = sched.log_names(jid)
        out_tail = tail_text(local / out_name, 1500).strip()
        err_tail = tail_text(local / err_name, 1500).strip()
        outputs = {}
        if not (local / "outputs.json").is_file() or not (local / "outputs.json").stat().st_size:
            outputs = printed_outputs(local / out_name) or {}
        else:
            try:
                outputs = json.loads((local / "outputs.json").read_text() or "{}")
            except ValueError as exc:
                return Outcome.fail("contract", f"job wrote invalid outputs.json: {exc}", retryable=False)
            except OSError as exc:
                return Outcome.fail("stage", f"cannot read outputs.json: {exc}", retryable=False)
        if not isinstance(outputs, dict):
            return Outcome.fail("contract", "outputs.json must contain a JSON object", retryable=False)
        outputs.setdefault("job_id", jid)
        outputs.setdefault("job_dir", job_dir)
        outputs.setdefault("local_dir", str(local))  # where this machine sees its files (= job_dir when local)
        details = {"job_id": jid, "sched": obs.get("final"), "ec": obs.get("ec"), "elapsed": obs.get("elapsed"),
                   "stdout_tail": out_tail[-600:], "stderr_tail": err_tail[-600:]}
        if status == "cancelled":
            return Outcome(status="cancelled", error_class="cancelled", message=msg, details=details)
        if status != "succeeded":
            last = (err_tail or out_tail).splitlines()[-1:] if (err_tail or out_tail) else []
            return Outcome.fail(cls or "job_failed", f"{sched.LABEL} {jid}: {msg}" + (f" — {last[0][:200]}" if last else ""),
                                outputs=outputs, retryable=retry, details=details)
        files, missing = collect_files(ctx.node.get("files") or {}, local)
        problems = check_outputs({k: v for k, v in outputs.items() if k not in ("job_id", "job_dir", "local_dir")},
                                 ctx.node.get("outputs") or {}) + [f"missing declared file {m}" for m in missing]
        if problems:
            return Outcome.fail("contract", "job finished but violates its output contract: " + "; ".join(problems[:5]),
                                outputs=outputs, files=files, retryable=False, details=details)
        summary = outputs.get("summary") if isinstance(outputs.get("summary"), str) else None
        shown = {k: v for k, v in outputs.items() if k not in ("job_id", "job_dir", "local_dir")}
        if not summary and (ctx.node.get("outputs") or {}) and shown:   # declared outputs are the contract
            summary = f"{sched.LABEL} {jid} completed · outputs: " + ", ".join(
                f"{k}={json.dumps(v, default=str)[:40]}" for k, v in list(shown.items())[:3])
        if not summary:
            lines = [l for l in out_tail.splitlines() if l.strip()]
            summary = f"{sched.LABEL} {jid} completed" + (f": {lines[-1].strip()}" if lines else "")
        return Outcome(status="succeeded", outputs=outputs, files=files, summary=first_line(summary, 240),
                       rationale=outputs.get("rationale") if isinstance(outputs.get("rationale"), str) else None,
                       details=details)

    # ------------------------------------------------------------ cancel
    def _cancel_remote(self, ctx: NodeCtx, cluster: dict, tr) -> None:
        """Mark the attempt cancelled on the cluster and stop whatever may be running for it."""
        parts = scheduler_for(cluster).cancel_command(ctx.job.get("job_dir"), ctx.job.get("job_id"),
                                                      ctx.job.get("submit_key"), _cmds(cluster))
        if parts:
            tr.run("; ".join(parts), timeout=60)

    def cancel(self, ctx: NodeCtx) -> None:
        cluster = _cluster(ctx)
        tr = make_transport(cluster)
        ctx.emit("job.cancel_requested", {"job_id": ctx.job.get("job_id")})
        if not ctx.job.get("job_id"):
            (ctx.attempt_dir / "stage").mkdir(parents=True, exist_ok=True)
            (ctx.attempt_dir / "stage" / "cancel").write_text("cancel\n")
        self._cancel_remote(ctx, cluster, tr)
        cache = ctx.run_dir / "cache" / f"cluster-{fs_name(ctx.node['cluster'])}.json"
        info = read_json(cache, {}) or {}
        info["last_poll"] = 0
        info["retry_at"] = 0
        atomic_write_json(cache, info)
