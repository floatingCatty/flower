"""Cluster backends: transports (local / ssh) and schedulers (slurm / none)."""
from __future__ import annotations

SCHEDULERS = ("slurm", "none")


def scheduler_for(cluster: dict):
    """The scheduler module of a cluster spec: :mod:`.slurm` (default) or :mod:`.direct` (``scheduler: none``)."""
    from . import direct, slurm
    return direct if (cluster or {}).get("scheduler", "slurm") == "none" else slurm


def log_files(cluster: dict, job_id: str | None) -> tuple[str, str]:
    """Names of a job's stdout / stderr files inside its job directory."""
    return scheduler_for(cluster).log_names(job_id)
