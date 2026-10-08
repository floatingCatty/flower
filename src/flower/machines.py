"""Machines: where work can run, one file per person (``~/.flower/machines.yaml``; ``$FLOWER_MACHINES`` overrides).

A person gives the least: how to reach a machine. Flower probes the rest and keeps it under ``probed:``; any key
written beside ``probed:`` wins over what was probed::

    narval:
      ssh: narval                     # an ~/.ssh/config alias, user@host[:port], or `local` (this machine)
      identity_file: ~/.ssh/id_x      # optional; also ssh_options: [...]
      note: ask before using more than 256 cores           # optional, read by agents and people
      agent_may_use: {cores: 256, hours: 24}               # optional: what an agent may take without asking
      # optional overrides: scheduler, work_dir, env_dir, cores, account, partition, modules, prelude
      probed: {scheduler: slurm, partitions: [...], accounts: [...], work_dir: ..., env_dir: ..., ...}

Plans name a machine as ``cluster: NAME``; the run records the settings it used (no secrets: those stay with ssh).
For a password or a second factor, ``flower remote login NAME`` opens one shared ssh connection that flower reuses.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

import yaml

from .util import FlowerError, atomic_write_text, now_iso

USER_KEYS = ("ssh", "identity_file", "ssh_options", "note", "agent_may_use", "scheduler", "work_dir", "env_dir",
             "cores", "account", "partition", "modules", "prelude")


def path() -> Path:
    return Path(os.environ.get("FLOWER_MACHINES") or Path.home() / ".flower" / "machines.yaml").expanduser()


def load() -> dict:
    p = path()
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise FlowerError("machines", f"{p} must be a mapping of machine names")
    return data


def save(data: dict) -> Path:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    head = ("# Machines flower may run work on (`flower remote add NAME TARGET` probes one; `flower remote list`).\n"
            "# Keys beside `probed:` are yours and win over it; `flower remote check NAME` refreshes `probed:`.\n")
    atomic_write_text(p, head + yaml.safe_dump(data, sort_keys=False, default_flow_style=None, width=110))
    os.chmod(p, 0o600)
    return p


def control_path() -> str:
    """The shared connection `flower remote login` opens and every ssh call of flower reuses (when it is open)."""
    return str(Path.home() / ".flower" / "ssh" / "%C")


def ssh_target(entry: dict) -> tuple[str, list[str]]:
    """(host for ssh/rsync, extra ssh options) from `ssh: alias | user@host[:port]`, identity_file, ssh_options."""
    t = str(entry.get("ssh") or "")
    opts: list[str] = []
    if ":" in t and not t.startswith("["):
        t, port = t.rsplit(":", 1)
        opts += ["-p", port]
    if entry.get("identity_file"):
        opts += ["-i", os.path.expanduser(str(entry["identity_file"])), "-o", "IdentitiesOnly=yes"]
    opts += [str(x) for x in entry.get("ssh_options") or []]
    opts += ["-o", f"ControlPath={control_path()}"]
    return t, opts


def effective(name: str, entry: dict) -> dict:
    """The machine as plans see it: probed facts with the person's keys on top."""
    eff = dict(entry.get("probed") or {})
    eff.update({k: v for k, v in entry.items() if k != "probed"})
    eff["name"] = name
    return eff


def cluster_spec(name: str, entry: dict) -> dict:
    """The machine as a cluster of a plan (what the run records)."""
    m = effective(name, entry)
    local = str(m.get("ssh") or "") == "local"
    c: dict = {"machine": name, "transport": "local" if local else "ssh",
               "scheduler": m.get("scheduler") or "none"}
    if not local:
        c["host"], c["ssh_options"] = ssh_target(m)
    if m.get("work_dir"):
        c["remote_root"] = m["work_dir"]
    if m.get("env_dir"):
        c["env_dir"] = m["env_dir"]
    if c["scheduler"] == "none":   # flower shares the cores out itself: what an agent may use, else the machine
        cores = (m.get("agent_may_use") or {}).get("cores") or m.get("cores")
        if isinstance(cores, int) and cores > 0:
            c["cpus"] = cores
    res = {k: m[k] for k in ("account", "partition") if m.get(k)}
    if res:
        c["resources"] = res
    for k in ("modules", "prelude"):
        if m.get(k):
            c[k] = m[k]
    return c


def map_role(raw: dict, role: str, name: str) -> None:
    """`run --machine ROLE=NAME`: the plan's steps on cluster ROLE run on the person's machine NAME; the plan's own
    entry for ROLE goes, with the inputs only it used (a protocol's host and ssh options). The run records the
    mapping, so its plan file is read the same way later (`flower rerun`)."""
    import json
    if get(name) is None:
        raise FlowerError("usage", f"no machine {name!r}", f"machines: {', '.join(load()) or 'none'} "
                          "(add one: flower remote add NAME user@host)")
    steps = [n for n in raw.get("nodes") or [] if isinstance(n, dict) and n.get("cluster") == role]
    if not steps:
        raise FlowerError("usage", f"no step of the plan runs on {role!r}",
                          "clusters the steps use: " + ", ".join(sorted({str(n.get("cluster")) for n in raw.get("nodes")
                                                                         or [] if isinstance(n, dict) and n.get("cluster")})))
    for n in steps:
        n["cluster"] = name
    gone = (raw.get("clusters") or {}).pop(role, None)
    rest = json.dumps({k: v for k, v in raw.items() if k != "inputs"}, default=str)
    for k in list(raw.get("inputs") or {}):
        if f"inputs.{k}" in json.dumps(gone, default=str) and f"inputs.{k}" not in rest:
            raw["inputs"].pop(k)


def get(name: str) -> dict | None:
    entry = load().get(name)
    return cluster_spec(name, entry) if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------- probing

PROBE = r'''
echo "user=$(id -un)"; echo "home=$HOME"; echo "os=$(uname -sm)"
echo "cores=$( (nproc --all || getconf _NPROCESSORS_ONLN) 2>/dev/null)"
echo "mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null)"
echo "gpus=$(nvidia-smi -L 2>/dev/null | grep -c GPU)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
for d in "${SCRATCH:-}" "/scratch/$(id -un)" "/scratch/$(id -un | cut -c1)/$(id -un)" "/work/$(id -un)"; do
  if [ -n "$d" ] && [ -d "$d" ] && [ -w "$d" ]; then echo "scratch=$d"; break; fi
done
for t in sbatch module conda mamba micromamba apptainer singularity docker rsync python3; do
  if command -v $t >/dev/null 2>&1 || type $t >/dev/null 2>&1; then echo "tool=$t"; fi
done
echo "free_home_gb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1048576}')"
if command -v sinfo >/dev/null 2>&1; then
  sinfo -h -o 'partition=%P|%l|%c|%m|%G|%D' 2>/dev/null | sort -u
  sacctmgr -nP show assoc user="$(id -un)" format=account 2>/dev/null | sort -u | sed 's/^/account=/'
fi
'''


def parse_probe(out: str) -> dict:
    kv: dict = {"tools": [], "partitions": [], "accounts": []}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if k == "tool":
            kv["tools"].append(v)
        elif k == "account" and v:
            kv["accounts"].append(v)
        elif k == "partition":
            p = v.split("|")
            if len(p) >= 6:
                kv["partitions"].append({"name": p[0].rstrip("*"), "default": p[0].endswith("*"), "max_time": p[1],
                                         "cores_per_node": _int(p[2]), "mem_mb_per_node": _int(p[3].rstrip("+")),
                                         "gres": None if p[4] in ("(null)", "") else p[4], "nodes": _int(p[5])})
        else:
            kv[k] = v
    merged: dict = {}
    for p in kv["partitions"]:   # sinfo prints a partition once per node configuration: one entry, the largest
        q = merged.setdefault(p["name"], dict(p, nodes=0))
        q["default"] = q["default"] or p["default"]
        q["nodes"] = (q["nodes"] or 0) + (p["nodes"] or 0)
        for k in ("cores_per_node", "mem_mb_per_node"):
            q[k] = max(x for x in (q[k], p[k], 0) if x is not None) or None
        if p["gres"] and p["gres"] != q["gres"]:
            q["gres"] = ",".join(x for x in (q["gres"], p["gres"]) if x)
    kv["partitions"] = list(merged.values())
    slurm = "sbatch" in kv["tools"] and bool(kv["partitions"])
    scratch = kv.get("scratch")
    base = scratch or "~"
    probed = {"scheduler": "slurm" if slurm else "none", "user": kv.get("user"), "os": kv.get("os"),
              "work_dir": f"{base}/flower-runs" if scratch else "~/flower-runs",
              "env_dir": f"{base}/flower-envs" if scratch else "~/.flower/envs",
              "tools": kv["tools"], "free_home_gb": _int(kv.get("free_home_gb")), "probed_at": now_iso()}
    if scratch:
        probed["scratch"] = scratch
    if slurm:
        probed["partitions"] = kv["partitions"]
        probed["accounts"] = kv["accounts"]
    else:   # a workstation: its own size and how busy it is
        probed.update(cores=_int(kv.get("cores")), memory_gb=round((_int(kv.get("mem_kb")) or 0) / 1048576),
                      gpus=_int(kv.get("gpus")) or 0, load=kv.get("load"))
    return probed


LIVE = r'''
echo "load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
echo "mem_avail_kb=$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null)"
for rd in __DIRS__; do
  r=${rd%%|*}; d=${rd#*|}
  while [ -n "$d" ] && [ ! -d "$d" ]; do d=$(dirname "$d"); done
  df -Pk "$d" 2>/dev/null | awk -v r="$r" 'NR==2 {print "disk=" r "|" $4 "|" $2 "|" $6}'
done
if command -v squeue >/dev/null 2>&1; then
  squeue -h -u "$(id -un)" -o 'job=%i|%P|%j|%T|%M|%l|%C|%r' 2>/dev/null
  sinfo -h -o 'cpus=%P|%C' 2>/dev/null
fi
'''


def live(name: str, entry: dict, timeout: float = 20) -> dict:
    """What the machine looks like now: load and free memory, free space where work and environments go; on Slurm
    your jobs (flower's are named flower-…) and each partition's idle cores. {"error": ...} when unreachable."""
    from .hpc.transport import make_transport
    m = effective(name, entry)
    dirs = []
    for role, d in (("home", "$HOME"), ("work", m.get("work_dir")), ("environments", m.get("env_dir"))):
        if d:
            d = str(d)
            dirs.append(f"{role}|" + ("$HOME" + d[1:] if d.startswith("~") else d))
    script = LIVE.replace("__DIRS__", " ".join(f'"{d}"' for d in dirs))
    r = make_transport(cluster_spec(name, entry)).run(script, timeout=timeout)
    if r.rc != 0 and not r.out.strip():
        return {"error": (r.err or "").strip()[-200:] or f"exit {r.rc}"}
    out: dict = {"jobs": [], "partitions": {}, "disks": []}
    for line in r.out.splitlines():
        k, _, v = line.partition("=")
        if k == "load":
            out["load"] = v.strip()
        elif k == "mem_avail_kb" and v.strip().isdigit():
            out["mem_free_gb"] = round(int(v) / 1048576)
        elif k == "disk":   # one entry per filesystem, with the roles on it (home, work, environments)
            p = v.split("|")
            if len(p) == 4 and p[1].isdigit():
                d = next((x for x in out["disks"] if x["mount"] == p[3]), None)
                if d is None:
                    out["disks"].append({"mount": p[3], "roles": [p[0]], "free_gb": round(int(p[1]) / 1048576),
                                         "size_gb": round(int(p[2]) / 1048576)})
                elif p[0] not in d["roles"]:
                    d["roles"].append(p[0])
        elif k == "job":
            p = v.split("|")
            if len(p) >= 8:
                out["jobs"].append({"id": p[0], "partition": p[1], "name": p[2], "state": p[3], "time": p[4],
                                    "limit": p[5], "cpus": _int(p[6]), "reason": p[7],
                                    "flower": p[2].startswith("flower-")})
        elif k == "cpus":
            p = v.split("|")
            c = p[1].split("/") if len(p) == 2 else []
            if len(c) == 4:
                out["partitions"][p[0].rstrip("*")] = {"allocated": _int(c[0]), "idle": _int(c[1]),
                                                       "total": _int(c[3])}
    return out


def host_key_name(host: str, opts: list[str]) -> str:
    """How known_hosts names the host: `[host]:port` on a port other than 22."""
    bare = host.split("@", 1)[-1]
    port = opts[opts.index("-p") + 1] if "-p" in opts else None
    return f"[{bare}]:{port}" if port and port != "22" else bare


def host_key(entry: dict) -> str | None:
    """The fingerprint ssh saved for the machine (shown when `remote add` accepted it), or None."""
    import subprocess
    host, opts = ssh_target(entry)
    try:
        r = subprocess.run(["ssh-keygen", "-l", "-F", host_key_name(host, opts)], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    keys = [ln.split(None, 1)[1] for ln in r.stdout.splitlines() if ln and not ln.startswith("#") and " " in ln]
    return keys[0] if keys else None


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def probe(name: str, entry: dict, timeout: float = 90, first: bool = False) -> dict:
    """``first`` (`remote add`): a host never seen before has its key accepted, as ssh does when you answer yes;
    a key that changed is still refused."""
    from .hpc.transport import make_transport
    spec = cluster_spec(name, {**entry, "probed": {}})
    if first and spec.get("transport") == "ssh":
        spec["ssh_options"] = ["-o", "StrictHostKeyChecking=accept-new", *spec["ssh_options"]]
    r = make_transport(spec).run(PROBE, timeout=timeout)
    if r.rc != 0 and not r.out.strip():
        err = (r.err or "").strip()
        host, opts = ssh_target(entry)
        shown = [x for i, x in enumerate(opts) if not x.startswith("ControlPath=")
                 and not (x == "-o" and i + 1 < len(opts) and opts[i + 1].startswith("ControlPath="))]
        try_it = " ".join(shlex.quote(x) for x in ["ssh", *shown, host, "true"])
        if "host key verification failed" in err.lower() or "remote host identification has changed" in err.lower():
            hint = ("the machine's host key is not the one ssh saved for it before (a reinstalled machine, or "
                    f"someone in between): check with the centre, then remove the old key: ssh-keygen -R "
                    f"{shlex.quote(host_key_name(host, opts))}")
        elif "denied" in err.lower():
            hint = ("ssh refused the login. Check, in order: the user name (`user@host`"
                    + ("" if "@" in host else f"; ssh used your local name {os.environ.get('USER', '')}") + "); "
                    "the key (`-i KEY`, readable only by you: chmod 600 KEY); and if the machine asks for a password "
                    "or a code, add it with --login (a shared connection, opened here). "
                    f"Plain ssh should log in without asking: {try_it}")
        else:
            hint = f"check that this works from here: {try_it}"
        raise FlowerError("unreachable", f"cannot reach {name} (nothing saved): {err[-300:] or 'exit %d' % r.rc}", hint)
    return parse_probe(r.out)


def details(name: str, entry: dict, now: dict | None = None) -> str:
    """`flower remote list`: one block per machine (``now``: its live state, from `live`)."""
    from .util import fmt_duration, seconds_since
    m = effective(name, entry)
    head = f"{name}  ({m.get('ssh')})"
    if not entry.get("probed"):
        return head + f"\n  not probed yet: flower remote check {name}"
    age = fmt_duration(seconds_since(m["probed_at"])) + " ago" if m.get("probed_at") else ""
    L = [head]
    if m.get("scheduler") == "slurm":
        acct = m.get("account") or ", ".join(m.get("accounts") or []) or "none found"
        L.append(f"  slurm, account {acct}")
        parts = m.get("partitions") or []
        if parts:
            idle = (now or {}).get("partitions") or {}
            rows = [("partition", "time limit", "nodes", "cores/node", "memory/node", "accelerators/node")
                    + (("idle cores now",) if idle else ())]
            for p in parts:
                i = idle.get(p["name"])
                rows.append((p["name"] + ("*" if p.get("default") else ""), _days(p.get("max_time")),
                             str(p.get("nodes") or ""), str(p.get("cores_per_node") or ""),
                             f"{round(p['mem_mb_per_node'] / 1024)} GB" if p.get("mem_mb_per_node") else "",
                             _gres(p.get("gres")))
                            + ((f"{i['idle']} of {i['total']}" if i else "",) if idle else ()))
            w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
            L += ["    " + "  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    else:
        gpus = f"{m.get('gpus')} GPUs" if m.get("gpus") else "no GPU"
        L.append(f"  workstation: {m.get('cores')} cores, {m.get('memory_gb')} GB, {gpus}")
    L.append(f"  work in {m.get('work_dir')}, environments in {m.get('env_dir')}")
    if now is not None:
        L += _now_lines(m, now)
    lim = m.get("agent_may_use")
    L.append("  agents may use: " + (", ".join(f"{v} {k}" for k, v in lim.items()) if lim else
                                     "not set (agent_may_use in " + str(path()).replace(str(Path.home()), "~") + ")"))
    if m.get("note"):
        L.append(f"  note: {m['note']}")
    if age:
        L.append(f"  probed {age}")
    return "\n".join(L)


def _now_lines(m: dict, now: dict) -> list[str]:
    if now.get("error"):
        return [f"  now: unreachable ({now['error']})"]
    L = []
    if m.get("scheduler") != "slurm" and now.get("load"):
        L.append(f"  now: load {now['load']}, {now.get('mem_free_gb')} GB memory free")
    free = "; ".join(f"{d['free_gb']} GB free of {d['size_gb']} GB ({', '.join(d['roles'])})"
                     for d in now.get("disks") or [])
    if free:
        L.append(f"  disk: {free}")
    if m.get("scheduler") == "slurm":
        jobs = now.get("jobs") or []
        if not jobs:
            L.append("  your jobs: none")
        else:
            by = {}
            for j in jobs:
                by[j["state"].lower()] = by.get(j["state"].lower(), 0) + 1
            L.append("  your jobs: " + ", ".join(f"{n} {s}" for s, n in by.items())
                     + f" ({sum(j['flower'] for j in jobs)} by flower)")
            for j in jobs[:6]:
                L.append(f"    {j['id']:<10} {j['partition']:<14} {j['state'].lower():<9} {j['time']:>10} of "
                         f"{j['limit']:<11} {j['cpus'] or ''} cores  {j['name'][:40]}"
                         + (f"  ({j['reason']})" if j["state"] == "PENDING" and j["reason"] else ""))
            if len(jobs) > 6:
                L.append(f"    … {len(jobs) - 6} more")
    return L


def _days(t) -> str:
    """Slurm's D-HH:MM:SS as a person reads it: 7-00:00:00 -> 7 d, 1-12:00:00 -> 1 d 12 h, UNLIMITED -> none."""
    import re
    t = str(t or "")
    if t.lower() in ("unlimited", "infinite"):
        return "none"
    m = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+)(?::(\d+))?", t)
    if not m:
        return t
    d, h, mi = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    parts = [f"{d} d"] * bool(d) + [f"{h} h"] * bool(h) + [f"{mi} min"] * bool(mi)
    return " ".join(parts) or "0"


def _gres(g) -> str:
    """gpu:a100:4,  RTX4090:8(S:0-7),RTX4090:8(S:0-3),  dcu:Hygon:4(S:0-3)  ->  4 × a100, 8 × RTX4090, 4 × Hygon dcu"""
    import re
    out = []
    for item in str(g or "").split(","):
        item = re.sub(r"\(.*?\)", "", item).strip()
        if not item:
            continue
        f = item.split(":")
        count = f[-1] if f[-1].isdigit() else "1"
        names = [x for x in f[:-1] if x] if f[-1].isdigit() else [x for x in f if x]
        kind = names[0] if names else "?"
        model = names[1] if len(names) > 1 else ""
        label = (f"{model} {kind}" if kind not in ("gpu",) else model or "GPU") if model else kind
        s = f"{count} × {label}"
        if s not in out:
            out.append(s)
    return ", ".join(out)


def summary(name: str, entry: dict) -> str:
    m = effective(name, entry)
    if not entry.get("probed"):   # written by hand
        return f"{name:<14} {str(m.get('ssh')):<22} not probed yet: flower remote check {name}"
    if m.get("scheduler") == "slurm":
        parts = m.get("partitions") or []
        size = (f"slurm, {len(parts)} partitions ({', '.join(p['name'] for p in parts[:4])}"
                + (", ..." if len(parts) > 4 else "") + ")" + (f", account {m.get('account') or ', '.join(m.get('accounts') or [])}"
                                                            if m.get("account") or m.get("accounts") else ""))
    else:
        size = f"{m.get('cores')} cores, {m.get('memory_gb')} GB" + (f", {m.get('gpus')} GPUs" if m.get("gpus") else "") \
            + (f", load {m.get('load')}" if m.get("load") else "")
    lim = m.get("agent_may_use")
    return (f"{name:<14} {str(m.get('ssh')):<22} {size}; work in {m.get('work_dir')}"
            + (f"; agents may use {', '.join(f'{v} {k}' for k, v in lim.items())}" if lim else "")
            + (f"; note: {m['note']}" if m.get("note") else ""))
