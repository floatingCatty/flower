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


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def probe(name: str, entry: dict, timeout: float = 90) -> dict:
    from .hpc.transport import make_transport
    spec = cluster_spec(name, {**entry, "probed": {}})
    r = make_transport(spec).run(PROBE, timeout=timeout)
    if r.rc != 0 and not r.out.strip():
        hint = ("open a shared connection first: flower remote login " + name) if "denied" in (r.err or "").lower() \
            or "password" in (r.err or "").lower() else "check that `ssh " + shlex.quote(str(entry.get("ssh"))) + \
            " true` works from this machine"
        raise FlowerError("unreachable", f"cannot reach {name}: {(r.err or '').strip()[-300:] or 'exit %d' % r.rc}", hint)
    return parse_probe(r.out)


def summary(name: str, entry: dict) -> str:
    m = effective(name, entry)
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
