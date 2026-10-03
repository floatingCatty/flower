#!/usr/bin/env python3
"""A small, faithful-enough fake Slurm for trying and testing flower ``job`` nodes locally.

``install(dir)`` writes ``dir/bin/{sbatch,squeue,sacct,scancel}`` wrappers; state lives in ``dir/state``.
Behaviour modelled on real Slurm where it matters to a workflow engine:

* ``sbatch --parsable`` prints the job id; ``#SBATCH`` lines and CLI flags (job-name, chdir, output,
  error, time, comment) are honoured; jobs start after ``FAKESLURM_PEND_S`` (default 1 s).
* ``squeue -h -u USER -t all -o '%i|%T|%r'`` lists jobs; finished jobs disappear after
  ``FAKESLURM_MINJOBAGE`` seconds (default 5) like MinJobAge.
* ``sacct -X -n -P -j IDS -o JobIDRaw,State,ExitCode,Elapsed`` (and ``--name``); new jobs are invisible to
  sacct for ``FAKESLURM_SACCT_LAG`` seconds (default 0) like slurmdbd lag.
* ``--time`` limits are enforced (TIMEOUT), ``scancel`` kills the process group (CANCELLED by uid).
* Fault injection: ``state/faults.json`` = [{"match": "<regex on job name>", "state": "NODE_FAIL",
  "after_s": 1, "times": 1}] makes matching jobs die with that state (times = how many jobs).

Standalone on purpose: no flower imports.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("FAKESLURM_HOME", Path(__file__).resolve().parent / "_state"))


def _jobs() -> Path:
    d = HOME / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(jid: str) -> dict | None:
    try:
        return json.loads((_jobs() / f"{jid}.json").read_text())
    except (FileNotFoundError, ValueError):
        return None


def _save(job: dict) -> None:
    p = _jobs() / f"{job['id']}.json"
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(job))
    os.replace(tmp, p)


def _locked_update(jid: str, fn) -> dict | None:
    lock = HOME / "lock"
    HOME.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        job = _load(jid)
        if job is None:
            return None
        fn(job)
        _save(job)
        return job


def _next_id() -> str:
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        p = HOME / "next_id"
        n = int(p.read_text()) if p.exists() else 1000
        p.write_text(str(n + 1))
        return str(n)


def _parse_time(v: str | None) -> float | None:
    if not v:
        return None
    v = v.strip()
    days = 0
    if "-" in v:
        d, v = v.split("-", 1)
        days = int(d)
    parts = [float(x) for x in v.split(":")]
    if len(parts) == 1:
        secs = parts[0] * 60  # minutes
    elif len(parts) == 2:
        secs = parts[0] * 60 + parts[1]
    else:
        secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
    return days * 86400 + secs


def _all() -> list[dict]:
    out = []
    for p in sorted(_jobs().glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------- commands

def sbatch(argv: list[str]) -> int:
    opts: dict = {}
    script = None
    it = iter(argv)
    for a in it:
        if a == "--parsable":
            opts["parsable"] = True
        elif a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k] = v
        elif a in ("-J", "--job-name", "-D", "--chdir", "-o", "--output", "-e", "--error", "-t", "--time",
                   "--comment", "-p", "--partition", "-N", "--nodes", "-n", "--ntasks"):
            key = {"-J": "job-name", "-D": "chdir", "-o": "output", "-e": "error", "-t": "time", "-p": "partition",
                   "-N": "nodes", "-n": "ntasks"}.get(a, a.lstrip("-"))
            opts[key] = next(it)
        elif a.startswith("-"):
            continue
        else:
            script = a
            break
    if not script:
        print("sbatch: error: no batch script given", file=sys.stderr)
        return 1
    cwd = opts.get("chdir") or os.getcwd()
    spath = Path(script) if os.path.isabs(script) else Path(cwd) / script
    if not spath.exists():
        print(f"sbatch: error: Unable to open file {script}", file=sys.stderr)
        return 1
    for line in spath.read_text().splitlines():
        m = re.match(r"#SBATCH\s+(--[\w-]+)(?:[= ](.*))?", line)
        if m:
            k = m.group(1)[2:]
            opts.setdefault(k, (m.group(2) or "").strip())
    jid = _next_id()
    job = {"id": jid, "name": opts.get("job-name") or spath.name, "comment": opts.get("comment", ""),
           "user": os.environ.get("USER", "user"), "dir": cwd, "script": str(spath), "state": "PENDING",
           "reason": "Priority", "submit": time.time(), "start": None, "end": None, "exit_code": None,
           "signal": 0, "time_limit": _parse_time(opts.get("time")),
           "out": (opts.get("output") or "slurm-%j.out").replace("%j", jid),
           "err": (opts.get("error") or opts.get("output") or "slurm-%j.out").replace("%j", jid),
           "cancel": False}
    _save(job)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_run", jid], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env={**os.environ, "FAKESLURM_HOME": str(HOME)})
    print(jid if opts.get("parsable") else f"Submitted batch job {jid}")
    return 0


def _fault_for(name: str) -> dict | None:
    p = HOME / "faults.json"
    lock = HOME / "lock"
    with open(lock, "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            rules = json.loads(p.read_text())
        except (FileNotFoundError, ValueError):
            return None
        for r in rules:
            if r.get("times", 1) > 0 and re.search(r["match"], name):
                r["times"] = r.get("times", 1) - 1
                p.write_text(json.dumps(rules))
                return r
    return None


def _run(jid: str) -> int:
    job = _load(jid)
    time.sleep(float(os.environ.get("FAKESLURM_PEND_S", "1")))
    job = _load(jid)
    if job["cancel"]:
        _locked_update(jid, lambda j: j.update(state="CANCELLED", end=time.time(), exit_code=0, signal=15))
        return 0
    fault = _fault_for(job["name"])
    _locked_update(jid, lambda j: j.update(state="RUNNING", reason="None", start=time.time()))
    env = {**os.environ, "SLURM_JOB_ID": jid, "SLURM_JOB_NAME": job["name"], "SLURM_SUBMIT_DIR": job["dir"]}
    env.pop("FAKESLURM_HOME", None)
    with open(Path(job["dir"]) / job["out"], "ab") as out, open(Path(job["dir"]) / job["err"], "ab") as err:
        p = subprocess.Popen(["bash", job["script"]], cwd=job["dir"], env=env, stdout=out, stderr=err,
                             start_new_session=True)
        t0 = time.time()
        final = None
        while p.poll() is None:
            time.sleep(0.2)
            cur = _load(jid) or {}
            if cur.get("cancel"):
                final = "CANCELLED"
            elif job["time_limit"] and time.time() - t0 > job["time_limit"]:
                final = "TIMEOUT"
            elif fault and time.time() - t0 > float(fault.get("after_s", 0)):
                final = fault["state"]
            if final:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    time.sleep(0.5)
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
                break
        rc = p.returncode
    if final is None:
        final = "COMPLETED" if rc == 0 else "FAILED"
    sig = 9 if final in ("CANCELLED", "TIMEOUT", "NODE_FAIL", "PREEMPTED", "OUT_OF_MEMORY") else 0
    _locked_update(jid, lambda j: j.update(state=final, end=time.time(),
                                           exit_code=(rc if rc is not None and rc >= 0 else 0) if not sig else 0,
                                           signal=sig))
    return 0


def _visible_squeue(job: dict) -> bool:
    if job["state"] in ("PENDING", "RUNNING"):
        return True
    age = float(os.environ.get("FAKESLURM_MINJOBAGE", "5"))
    return job["end"] is not None and time.time() - job["end"] < age


def _fmt_elapsed(job: dict) -> str:
    if not job.get("start"):
        return "00:00:00"
    s = int((job.get("end") or time.time()) - job["start"])
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def squeue(argv: list[str]) -> int:
    fmt = "%.18i %.9P %.8j %.8u %.2t %.10M %.6D %R"
    header = True
    ids = None
    states_all = False
    it = iter(argv)
    for a in it:
        if a in ("-h", "--noheader"):
            header = False
        elif a in ("-o", "--format"):
            fmt = next(it)
        elif a.startswith("--format="):
            fmt = a.split("=", 1)[1]
        elif a in ("-j", "--jobs"):
            ids = set(next(it).split(","))
        elif a.startswith("--jobs="):
            ids = set(a.split("=", 1)[1].split(","))
        elif a in ("-t", "--states"):
            states_all = next(it).lower() == "all"
        elif a in ("-u", "--user"):
            next(it)
    rows = []
    for job in _all():
        if ids is not None and job["id"] not in ids:
            continue
        if not _visible_squeue(job):
            continue
        if not states_all and job["state"] not in ("PENDING", "RUNNING"):
            continue
        state = job["state"]
        if state == "RUNNING" and job.get("cancel"):
            state = "COMPLETING"

        def tok(m):
            c = m.group(2)
            return {"i": job["id"], "T": state, "t": state[:2], "j": job["name"], "r": job.get("reason") or "None",
                    "u": job["user"], "M": _fmt_elapsed(job), "P": "fake", "D": "1", "R": "localhost"}.get(c, "")

        rows.append(re.sub(r"%(\.?\d*)([A-Za-z])", tok, fmt))
    if ids is not None and not rows and not any(_load(i) for i in ids):
        print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
        return 1
    if header:
        print("JOBID STATE NAME")
    print("\n".join(rows))
    return 0


def sacct(argv: list[str]) -> int:
    fields = ["JobID", "JobName", "State", "ExitCode"]
    ids = None
    name = None
    parsable = False
    it = iter(argv)
    for a in it:
        if a in ("-o", "--format"):
            fields = next(it).split(",")
        elif a.startswith("--format="):
            fields = a.split("=", 1)[1].split(",")
        elif a in ("-j", "--jobs"):
            ids = set(next(it).split(","))
        elif a.startswith("--jobs="):
            ids = set(a.split("=", 1)[1].split(","))
        elif a.startswith("--name="):
            name = a.split("=", 1)[1]
        elif a == "--name":
            name = next(it)
        elif a in ("-P", "--parsable2"):
            parsable = True
        elif a in ("-S", "--starttime", "-E", "--endtime", "-u", "--user"):
            next(it)
    lag = float(os.environ.get("FAKESLURM_SACCT_LAG", "0"))
    out = []
    for job in _all():
        if ids is not None and job["id"] not in ids:
            continue
        if name is not None and job["name"] != name:
            continue
        if time.time() - job["submit"] < lag:
            continue
        state = job["state"]
        if state == "CANCELLED":
            state = f"CANCELLED by {os.getuid()}"
        vals = {"JobID": job["id"], "JobIDRaw": job["id"], "JobName": job["name"], "State": state,
                "ExitCode": f"{job.get('exit_code') or 0}:{job.get('signal') or 0}", "Elapsed": _fmt_elapsed(job),
                "Comment": job.get("comment", "")}
        out.append(("|" if parsable else "  ").join(str(vals.get(f, "")) for f in fields))
    print("\n".join(out))
    return 0


def scancel(argv: list[str]) -> int:
    rc = 0
    for jid in [a for a in argv if not a.startswith("-")]:
        jid = jid.split("_")[0]

        def mark(j):
            j["cancel"] = True
            if j["state"] == "PENDING":
                j.update(state="CANCELLED", end=time.time(), exit_code=0, signal=15)

        if _locked_update(jid, mark) is None:
            print(f"scancel: error: Kill job error on job id {jid}: Invalid job id specified", file=sys.stderr)
            rc = 1
    return rc


def install(directory: Path) -> Path:
    directory = Path(directory).resolve()
    bindir = directory / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    state = directory / "state"
    state.mkdir(parents=True, exist_ok=True)
    me = Path(__file__).resolve()
    for cmd in ("sbatch", "squeue", "sacct", "scancel"):
        p = bindir / cmd
        p.write_text(f"#!/bin/sh\nFAKESLURM_HOME={shlex.quote(str(state))} exec {shlex.quote(sys.executable)} "
                     f"{shlex.quote(str(me))} {cmd} \"$@\"\n")
        p.chmod(0o755)
    return bindir


def main() -> int:
    cmd, argv = sys.argv[1], sys.argv[2:]
    return {"sbatch": sbatch, "squeue": squeue, "sacct": sacct, "scancel": scancel,
            "_run": lambda a: _run(a[0])}[cmd](argv)


if __name__ == "__main__":
    sys.exit(main())
