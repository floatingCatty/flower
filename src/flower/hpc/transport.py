"""Command transport for a cluster: run on this machine (``local``) or over OpenSSH (``ssh``).

OpenSSH lessons ported from Eleforge (`apps/backend/compute/remote/ssh/openssh.py`, commits dbb0fd2d,
e372517d, 0d06b919): BatchMode (never prompt), bounded connect/keepalive, a hard subprocess timeout,
and automation never *starts* a ControlMaster (a half-dead master hangs every command). If the user
has one open for MFA sites we reuse it when ``ssh -O check`` succeeds.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TRANSIENT = ("connection timed out", "connection refused", "connection reset", "socket timed out",
             "unable to contact slurm controller", "slurm_load_jobs error", "temporarily unavailable",
             "broken pipe", "no route to host", "could not resolve hostname", "network is unreachable",
             "invalid user for slurmuser", "kex_exchange_identification")


@dataclass
class CmdResult:
    rc: int
    out: str
    err: str
    timed_out: bool = False

    @property
    def transient(self) -> bool:
        if self.timed_out or self.rc in (255, 75):
            return True
        low = (self.err + self.out).lower()
        return any(t in low for t in TRANSIENT)


class Transport:
    is_local = True

    def __init__(self, cluster: dict):
        self.cluster = cluster
        env = {str(k): str(v) for k, v in (cluster.get("env") or {}).items()}
        self.env_prefix = "".join(f"export {k}={shlex.quote(v)}; " for k, v in env.items())

    def run(self, script: str, timeout: float = 120.0) -> CmdResult:
        raise NotImplementedError

    def put(self, local: Path, remote: str) -> CmdResult:
        raise NotImplementedError

    def get(self, remote: str, local: Path, patterns: list[str] | None = None) -> CmdResult:
        raise NotImplementedError


class LocalTransport(Transport):
    is_local = True

    def run(self, script: str, timeout: float = 120.0) -> CmdResult:
        try:
            env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
            p = subprocess.run(["bash", "-c", self.env_prefix + script], capture_output=True, text=True,
                               timeout=timeout, env=env)
            return CmdResult(p.returncode, p.stdout, p.stderr)
        except subprocess.TimeoutExpired as exc:
            return CmdResult(124, exc.stdout or "", exc.stderr or "", timed_out=True)

    def put(self, local: Path, remote: str) -> CmdResult:
        dst = Path(os.path.expanduser(remote))
        dst.parent.mkdir(parents=True, exist_ok=True)
        if Path(local).is_dir():
            shutil.copytree(local, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(local, dst)
        return CmdResult(0, "", "")

    def get(self, remote: str, local: Path, patterns: list[str] | None = None) -> CmdResult:
        src = Path(os.path.expanduser(remote))
        local = Path(local)
        local.mkdir(parents=True, exist_ok=True)
        for pat in patterns or ["*"]:
            for f in src.glob(pat):
                if f.is_file():
                    rel = f.relative_to(src)
                    (local / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, local / rel)
        return CmdResult(0, "", "")


class SSHTransport(Transport):
    is_local = False

    def __init__(self, cluster: dict):
        super().__init__(cluster)
        self.host = cluster["host"]
        self.opts = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(cluster.get('connect_timeout', 30))}",
                     "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=4",
                     "-o", "ControlMaster=no"] + list(cluster.get("ssh_options") or [])
        self._master_checked = None

    def _ssh_base(self) -> list[str]:
        if self._master_checked is None:
            try:
                ok = subprocess.run(["ssh", "-O", "check", self.host], capture_output=True, timeout=5).returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                ok = False
            self._master_checked = ok
        opts = list(self.opts)
        if not self._master_checked:
            opts += ["-o", "ControlPath=none"]
        return ["ssh", *opts, self.host]

    def run(self, script: str, timeout: float = 120.0) -> CmdResult:
        cmd = self._ssh_base() + ["bash -lc " + shlex.quote(self.env_prefix + script)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            return CmdResult(p.returncode, p.stdout, p.stderr)
        except subprocess.TimeoutExpired as exc:
            return CmdResult(124, exc.stdout or "", exc.stderr or "", timed_out=True)

    def _rsync(self, args: list[str], timeout: float = 3600) -> CmdResult:
        ssh = " ".join(shlex.quote(x) for x in ["ssh", *self.opts, "-o", "ControlPath=none"])
        cmd = ["rsync", "-a", "--partial", "--timeout=60", "-e", ssh, *args]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return CmdResult(p.returncode, p.stdout, p.stderr)
        except subprocess.TimeoutExpired as exc:
            return CmdResult(124, exc.stdout or "", exc.stderr or "", timed_out=True)
        except FileNotFoundError:
            return CmdResult(127, "", "rsync not installed")

    def put(self, local: Path, remote: str) -> CmdResult:
        src = str(local) + ("/" if Path(local).is_dir() else "")
        return self._rsync([src, f"{self.host}:{remote}"])

    def get(self, remote: str, local: Path, patterns: list[str] | None = None) -> CmdResult:
        Path(local).mkdir(parents=True, exist_ok=True)
        args = []
        if patterns:
            args += ["--include=*/"] + [f"--include={p}" for p in patterns] + ["--exclude=*", "--prune-empty-dirs"]
        return self._rsync(args + [f"{self.host}:{remote.rstrip('/')}/", str(local) + "/"])


def make_transport(cluster: dict) -> Transport:
    return SSHTransport(cluster) if cluster.get("transport", "local") == "ssh" else LocalTransport(cluster)
