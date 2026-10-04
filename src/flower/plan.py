"""Plan model: load, validate, normalise, digest, diff and amend a workflow plan.

A plan is the *contract* a user approves. It is a YAML document::

    flower: 1
    id: si-bands
    title: Silicon band structure
    inputs:   {structure: {type: path, required: true}}
    defaults: {harness: {name: claude, model: sonnet}, timeout: 2h, retry: {max_attempts: 2}}
    clusters: {hpc: {transport: ssh, host: myhpc, remote_root: ~/flower-runs}}
    nodes:
      - id: relax
        kind: job
        ...

Validation never stops at the first problem: it returns every issue as
``{code, path, message, suggestion}`` so an agent can fix the plan in one pass (LabFlow PlanLoadError).
"""
from __future__ import annotations

import copy
import difflib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from . import template as tpl
from .util import FlowerError, digest, parse_duration

PLAN_VERSION = 1
NODE_KINDS = ("shell", "function", "agent", "job", "gate", "wait")
TRIGGERS = ("all_success", "all_done", "any_success")
OUTPUT_TYPES = ("string", "number", "integer", "boolean", "object", "array", "path", "any")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*(\[\d+\])?$")
KNOWN_HARNESSES = ("claude", "codex", "pi", "script")
TOP_KEYS = {"flower", "id", "title", "description", "inputs", "defaults", "clusters", "policies", "results", "nodes"}

COMMON_KEYS = {"id", "kind", "title", "description", "needs", "when", "trigger", "inputs", "outputs",
               "files", "retry", "timeout", "cache", "foreach", "tags", "env", "cwd", "on_failure",
               "bind", "expanded_from", "generated"}
KIND_KEYS = {
    "shell": {"run", "shell", "cluster", "stage_in", "retrieve", "resources", "modules", "prelude", "environment",
              "tmpdir"},
    "function": {"call", "python", "pythonpath", "args", "cluster", "stage_in", "retrieve", "resources", "modules",
                 "prelude", "environment", "tmpdir"},
    "agent": {"prompt", "prompt_file", "harness", "system", "repair_attempts", "effects", "context"},
    "job": {"cluster", "script", "resources", "stage_in", "retrieve", "poll", "deadline", "modules", "prelude",
            "environment", "tmpdir"},
    "gate": {"message", "decisions", "on_reject", "approve_value"},
    "wait": {"signal", "deadline", "timer", "token"},
}


class Issue(dict):
    def __init__(self, code: str, path: str, message: str, suggestion: str | None = None):
        super().__init__(code=code, path=path, message=message)
        if suggestion:
            self["suggestion"] = suggestion


class PlanInvalid(FlowerError):
    def __init__(self, issues: list[Issue]):
        lines = "; ".join(f"{i['path']}: {i['message']}" for i in issues[:5])
        more = f" (+{len(issues) - 5} more)" if len(issues) > 5 else ""
        super().__init__("plan_invalid", f"plan has {len(issues)} problem(s): {lines}{more}",
                         "Fix every issue listed under details, then run `flower plan validate` again.",
                         details=issues)
        self.issues = issues


# ====================================================================== loading

class _Yaml12Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans: only true/false. In YAML 1.1 `on`, `off`, `yes`, `no` are booleans,
    which silently turns `retry: {on: [...]}` into `{True: [...]}`."""


_Yaml12Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
import re as _re  # noqa: E402
_Yaml12Loader.add_implicit_resolver("tag:yaml.org,2002:bool", _re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                                    list("tTfF"))


def yaml_load(text: str):
    return yaml.load(text, Loader=_Yaml12Loader)  # noqa: S506 - SafeLoader subclass

def load_plan_file(path: str | Path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FlowerError("plan_not_found", f"plan file {path} does not exist") from None
    try:
        raw = yaml_load(text)
    except yaml.YAMLError as exc:
        raise FlowerError("plan_yaml", f"{path}: invalid YAML: {exc}",
                             "Check indentation; quote strings that contain ': ' or start with '${'.") from None
    if not isinstance(raw, dict):
        raise FlowerError("plan_yaml", f"{path}: top level must be a mapping")
    base = path.parent.resolve()
    # inline prompt files so the approved contract is self-contained and hashable
    for i, node in enumerate(raw.get("nodes") or []):
        if isinstance(node, dict) and node.get("prompt_file") and not node.get("prompt"):
            pf = (base / str(node["prompt_file"])).resolve()
            try:
                node["prompt"] = pf.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise FlowerError("plan_prompt_file", f"nodes[{i}].prompt_file: {pf} not found") from None
    raw.setdefault("_source", {})["dir"] = str(base)
    raw["_source"]["file"] = str(path.resolve())
    return raw


# ====================================================================== normalisation

def _as_list(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _merged(base: dict, val: Any) -> Any:
    """Merge a node-level mapping over defaults; non-mappings are left for `validate` to report."""
    if val is None:
        return dict(base)
    if isinstance(val, dict):
        out = dict(base)
        out.update(val)
        return out
    return val


def normalize_node(node: dict, defaults: dict) -> dict:
    """Fill defaults. Type-tolerant: a malformed field is kept as-is so validation can report it."""
    n = copy.deepcopy(node)
    defaults = defaults if isinstance(defaults, dict) else {}
    n["needs"] = [str(x) for x in _as_list(n.get("needs"))]
    n.setdefault("title", n.get("id"))
    n.setdefault("trigger", "all_success")
    n.setdefault("inputs", {})
    n.setdefault("outputs", {})
    n.setdefault("files", {})
    if isinstance(n.get("timeout"), (str, int, float)):
        n["timeout"] = {"total": n["timeout"]}
    dt = defaults.get("timeout")
    base_t = dict(dt) if isinstance(dt, dict) else ({"total": dt} if dt else {})
    n["timeout"] = _merged(base_t, n.get("timeout"))
    base_r = {"max_attempts": 1, "backoff": "10s"}
    if isinstance(defaults.get("retry"), dict):
        base_r.update(defaults["retry"])
    n["retry"] = _merged(base_r, n.get("retry"))
    kind = n.get("kind")
    if kind == "agent":
        dh = defaults.get("harness") if isinstance(defaults.get("harness"), dict) else {}
        hv = n.get("harness")
        h = _merged(dh, hv) if not isinstance(hv, str) else {**dh, "name": hv}
        if isinstance(h, dict):
            h.setdefault("name", "claude")
        n["harness"] = h
        n.setdefault("repair_attempts", 2)
        n.setdefault("cache", False)
    elif kind in ("gate", "wait"):
        n["cache"] = False  # a decision or an external event is never "reused"
    else:
        n.setdefault("cache", True)
    if kind == "gate":
        n.setdefault("decisions", ["approve", "reject"])
        dec = n["decisions"]
        n.setdefault("approve_value", dec[0] if isinstance(dec, list) and dec else "approve")
    if on_cluster(n):
        n.setdefault("resources", {})
        n.setdefault("stage_in", [])
        n.setdefault("retrieve", [])
    if isinstance(n.get("outputs"), dict):  # shorthand "key: string" -> {type: string}
        outs = {}
        for k, v in n["outputs"].items():
            if isinstance(v, str):
                v = {"type": v}
            if isinstance(v, dict) or v is None:
                v = dict(v or {})
                v.setdefault("type", "any")
                v.setdefault("required", True)
            outs[k] = v
        n["outputs"] = outs
    return n


def normalize(raw: dict) -> dict:
    """Return a normalised copy of a raw plan (no validation)."""
    plan = copy.deepcopy(raw)
    plan.setdefault("title", plan.get("id"))
    plan.setdefault("description", "")
    plan.setdefault("inputs", {})
    plan.setdefault("defaults", {})
    plan.setdefault("clusters", {})
    plan.setdefault("policies", {})
    plan.setdefault("results", [])
    nodes = plan.get("nodes")
    if isinstance(nodes, list):
        plan["nodes"] = [normalize_node(n, plan["defaults"]) if isinstance(n, dict) else n for n in nodes]
        _implicit_local_cluster(plan)
        _expand_environments(plan)
    return plan


LOCAL_CLUSTER = "local"


def _implicit_local_cluster(plan: dict) -> None:
    """A shell/function step that names an `environment` but no `cluster` runs on this machine with it: on the
    cluster `local` (transport local, no scheduler), defined here unless the plan has its own. So a recipe gives
    every step a reproducible software environment, local analysis included (no hard-coded interpreters)."""
    for n in plan["nodes"]:
        if isinstance(n, dict) and n.get("environment") and not n.get("cluster") \
                and n.get("kind") in ("shell", "function"):
            n["cluster"] = LOCAL_CLUSTER
    # defined whenever a step uses it, also when the step arrives already resolved (an amendment of a running plan)
    if any(isinstance(n, dict) and n.get("cluster") == LOCAL_CLUSTER for n in plan["nodes"]):
        plan["clusters"].setdefault(LOCAL_CLUSTER, {"transport": "local", "scheduler": "none"})


ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _expand_environments(plan: dict) -> None:
    """For each (environment, cluster) the nodes use, add one generated step that makes it ready (check, else
    setup + check), make the users of it depend on that step, and activate it in their prelude. Idempotent:
    a stored plan normalised again keeps the step and the recipe version it was approved with."""
    from . import envs as envmod
    src = (plan.get("_source") or {}).get("dir")
    nodes = plan["nodes"]
    by_id = {n.get("id"): n for n in nodes if isinstance(n, dict)}
    added = []
    for n in nodes:
        if not (isinstance(n, dict) and n.get("environment") and on_cluster(n) and n.get("cluster")):
            continue
        name, cl = str(n["environment"]), str(n["cluster"])
        if not ENV_NAME_RE.match(name):
            continue
        eid = envmod.node_id(name, cl)
        existing = by_id.get(eid)
        if existing is not None and str(existing.get("generated", "")).startswith(f"env:{name}:"):
            h = str(existing["generated"]).split(":", 2)[2]
        else:
            d = envmod.find(name, src)
            state, fz = envmod.status(d) if d is not None else ("missing", None)
            if state != "frozen":
                continue  # validation reports it
            h = fz["hash"]
            c = (plan.get("clusters") or {}).get(cl) or {}
            en = normalize_node(envmod.env_node(name, cl, d, h, c.get("install", "auto") != "never"),
                                plan.get("defaults") or {})
            en["generated"] = f"env:{name}:{h}"
            added.append(en)
            by_id[eid] = en
        needs = n.get("needs") if isinstance(n.get("needs"), list) else []
        if eid not in needs:
            n["needs"] = [*needs, eid]
        pre = n.get("prelude")
        pre = list(pre) if isinstance(pre, list) else ([pre] if pre else [])
        pre = [x for x in pre if not str(x).startswith(('export FLOWER_ENV_PREFIX=', 'set +u; source "$FLOWER_ENV_DIR'))]
        n["prelude"] = envmod.activation(name, h) + pre
    if added:
        plan["nodes"] = added + nodes


# ====================================================================== validation

def _environment_issues(plan: dict, n: dict, p: str) -> list:
    from . import envs as envmod
    name = n.get("environment")
    if not isinstance(name, str) or not ENV_NAME_RE.match(name):
        return [Issue("environment", f"{p}.environment", f"invalid environment name {name!r}",
                      "letters, digits, '.', '-', '_'; the recipe lives in envs/<name>/")]
    if not on_cluster(n) or not n.get("cluster"):
        return [Issue("environment", f"{p}.environment", "`environment` applies to shell, function and job steps",
                      "a shell/function step without `cluster:` uses it on this machine")]
    if any(isinstance(x, dict) and str(x.get("generated", "")).startswith(f"env:{name}:") for x in plan.get("nodes") or []):
        return []  # the step was generated from a frozen recipe
    src = (plan.get("_source") or {}).get("dir")
    d = envmod.find(name, src)
    if d is None:
        return [Issue("environment", f"{p}.environment", f"no recipe envs/{name}/ next to the plan or above it",
                      f"create it: flower env new {name}  (then explore, write the scripts, flower env freeze {name})")]
    state, _ = envmod.status(d)
    if state == "draft":
        return [Issue("environment", f"{p}.environment", f"recipe {d} is not frozen", f"flower env freeze {name}")]
    if state == "changed":
        return [Issue("environment", f"{p}.environment", f"recipe {d} was edited after it was frozen",
                      f"freeze the new version: flower env freeze {name}")]
    return []


def on_cluster(node: dict) -> bool:
    """True for nodes that run through a cluster (and the job executor): ``job`` nodes, and ``shell`` /
    ``function`` nodes that name a ``cluster:``."""
    kind = node.get("kind")
    return kind == "job" or (kind in ("shell", "function") and bool(node.get("cluster")))


def effective_needs(node: dict) -> list[str]:
    """Explicit `needs` plus every node referenced from templates/expressions (refs imply edges)."""
    refs = tpl.node_refs({k: v for k, v in node.items() if k not in ("needs", "id", "title", "description")})
    out = list(node.get("needs") or [])
    for r in sorted(refs):
        if r not in out and r != node.get("id"):
            out.append(r)
    return out


def validate(plan: dict) -> list[Issue]:
    issues: list[Issue] = []
    if plan.get("flower") != PLAN_VERSION:
        issues.append(Issue("version", "flower", f"expected `flower: {PLAN_VERSION}`"
                            + (" (missing)" if "flower" not in plan else f", got {plan.get('flower')!r}"),
                            "Add `flower: 1` at the top of the plan."))
    pid = plan.get("id")
    if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-.]*", pid or ""):
        issues.append(Issue("plan_id", "id", "plan `id` is required: letters, digits, '-', '_', '.'",
                            "e.g. `id: si-band-structure`"))
    for name, spec in (plan.get("inputs") or {}).items():
        if not isinstance(spec, dict):
            issues.append(Issue("input", f"inputs.{name}", "input spec must be a mapping",
                                "e.g. `structure: {type: path, required: true}`"))
    pol = plan.get("policies") or {}
    if isinstance(pol, dict) and pol.get("edits", "ask") not in ("ask", "unfinished", "all"):
        issues.append(Issue("policies", "policies.edits", f"unknown edits policy {pol.get('edits')!r}",
                            "`ask` (default), `unfinished` (auto-approve plan-file edits to nodes that have not "
                            "succeeded), or `all`"))
    for name, c in (plan.get("clusters") or {}).items():
        if not isinstance(c, dict):
            issues.append(Issue("cluster", f"clusters.{name}", "cluster spec must be a mapping"))
            continue
        t = c.get("transport", "local")
        if t not in ("local", "ssh"):
            issues.append(Issue("cluster", f"clusters.{name}.transport", f"unknown transport {t!r}",
                                "use `local` (flower runs on the login node) or `ssh` (with `host:`)"))
        if t == "ssh" and not c.get("host"):
            issues.append(Issue("cluster", f"clusters.{name}.host", "ssh transport needs `host` (an ~/.ssh/config alias)"))
        if c.get("install", "auto") not in ("auto", "never"):
            issues.append(Issue("cluster", f"clusters.{name}.install", "install must be `auto` or `never`",
                                "`never`: environment steps only check, they never run setup.sh there"))
        if c.get("cpus") is not None and not (isinstance(c.get("cpus"), int) and c["cpus"] > 0):
            issues.append(Issue("cluster", f"clusters.{name}.cpus", "cpus must be a positive integer",
                                "the cores flower may use on that machine, shared by all runs of this project"))
        if c.get("scheduler", "slurm") not in ("slurm", "none"):
            issues.append(Issue("cluster", f"clusters.{name}.scheduler", f"unknown scheduler {c.get('scheduler')!r}",
                                "`slurm` (sbatch) or `none` (run the payload directly on the host)"))

    nodes = plan.get("nodes")
    if not isinstance(nodes, list):
        issues.append(Issue("nodes", "nodes", "a plan needs a `nodes` list",
                            "`nodes: []` for a draft that grows step by step (`flower start`, `flower add`)"))
        return issues
    ids: dict[str, int] = {}
    for i, n in enumerate(nodes):
        p = f"nodes[{i}]"
        if not isinstance(n, dict):
            issues.append(Issue("node", p, "node must be a mapping"))
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not ID_RE.match(nid):
            issues.append(Issue("node_id", f"{p}.id", f"invalid node id {nid!r}",
                                "ids start with a letter; use letters, digits, '-' or '_'"))
        elif nid in ids:
            issues.append(Issue("duplicate_id", f"{p}.id", f"duplicate node id {nid!r} (also nodes[{ids[nid]}])"))
        else:
            ids[nid] = i
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            continue
        p = f"nodes[{i}]({n.get('id')})"
        kind = n.get("kind")
        if kind not in NODE_KINDS:
            issues.append(Issue("kind", f"{p}.kind", f"unknown kind {kind!r}", f"one of: {', '.join(NODE_KINDS)}"))
            continue
        for fld, typ, hint in (("retry", dict, "{max_attempts: 3, backoff: 30s}"), ("timeout", dict, "{total: 2h, idle: 30m}"),
                               ("outputs", dict, "{energy: number, converged: boolean}"), ("files", dict, "{report: report.md}"),
                               ("inputs", dict, "{x: ${a.outputs.x}}"), ("env", dict, "{OMP_NUM_THREADS: '1'}"),
                               ("harness", dict, "{name: claude, model: sonnet}"), ("resources", dict, "{nodes: 1, time: '01:00:00'}"),
                               ("effects", dict, "{amend: {auto_approve: true}}")):
            if fld in n and n[fld] is not None and not isinstance(n[fld], typ):
                issues.append(Issue("type", f"{p}.{fld}", f"`{fld}` must be a mapping, got {type(n[fld]).__name__}",
                                    f"e.g. {fld}: {hint}"))
        for key, spec in (n.get("outputs") or {}).items() if isinstance(n.get("outputs"), dict) else []:
            if not isinstance(spec, dict):
                issues.append(Issue("output_type", f"{p}.outputs.{key}", "output spec must be a type name or mapping",
                                    "e.g. energy: number"))
        if isinstance(n.get("resources"), dict):
            for rk, rv in n["resources"].items():
                vals = rv if isinstance(rv, list) else [rv]
                if any(isinstance(x, str) and ("\n" in x or "\r" in x) for x in vals):
                    issues.append(Issue("resources", f"{p}.resources.{rk}", "resource values may not contain newlines"))
            extra = n["resources"].get("extra") or []
            if any(not (isinstance(x, str) and x.startswith("--")) for x in (extra if isinstance(extra, list) else [extra])):
                issues.append(Issue("resources", f"{p}.resources.extra", "extra sbatch flags must be strings starting with --"))
            tv = n["resources"].get("time")
            if isinstance(tv, (int, float)) and tv <= 0:
                issues.append(Issue("resources", f"{p}.resources.time", "time must be positive (minutes or 'H:MM:SS')"))
        if any(i["path"].startswith(p + ".") and i["code"] == "type" for i in issues):
            continue
        allowed = COMMON_KEYS | KIND_KEYS[kind]
        for k in n:
            if k not in allowed:
                close = difflib.get_close_matches(str(k), sorted(allowed), n=1, cutoff=0.6) \
                    or [a for a in sorted(allowed) if a[:3] == str(k)[:3]]
                issues.append(Issue("unknown_field", f"{p}.{k}", f"field {k!r} is not valid for kind {kind}",
                                    f"did you mean {close[0]!r}?" if close else f"valid fields: {', '.join(sorted(KIND_KEYS[kind]))}"))
        soft = tpl.soft_refs({k: v for k, v in n.items() if k not in ("needs", "id", "title", "description")})
        for dep in effective_needs(n) + sorted(soft):
            if dep not in ids:
                issues.append(Issue("unknown_ref", p, f"refers to unknown node {dep!r}",
                                    f"known nodes: {', '.join(sorted(ids)) or '(none)'}"))
        if n.get("trigger") not in TRIGGERS:
            issues.append(Issue("trigger", f"{p}.trigger", f"unknown trigger {n.get('trigger')!r}",
                                f"one of {', '.join(TRIGGERS)}"))
        for field in ("when",):
            if n.get(field):
                try:
                    tpl.check_expr(str(n[field]))
                except tpl.TemplateError as exc:
                    issues.append(Issue("expr", f"{p}.{field}", str(exc)))
        for key, spec in (n.get("outputs") or {}).items():
            if isinstance(spec, dict) and spec.get("type") not in OUTPUT_TYPES:
                issues.append(Issue("output_type", f"{p}.outputs.{key}", f"unknown type {spec.get('type')!r}",
                                    f"one of {', '.join(OUTPUT_TYPES)}"))
        for fld in ("total", "idle"):
            try:
                parse_duration((n.get("timeout") or {}).get(fld))
            except ValueError as exc:
                issues.append(Issue("duration", f"{p}.timeout.{fld}", str(exc), "e.g. 90s, 30m, 2h, 7d"))
        try:
            ma = int(n.get("retry", {}).get("max_attempts", 1))
            if ma < 1:
                raise ValueError
        except (TypeError, ValueError):
            issues.append(Issue("retry", f"{p}.retry.max_attempts", "must be an integer >= 1"))
        if n.get("foreach") is not None and not isinstance(n["foreach"], (str, list)):
            issues.append(Issue("foreach", f"{p}.foreach", "foreach must be a reference like ${a.outputs.items} or a list"))
        # kind specific
        if kind == "shell" and not n.get("run"):
            issues.append(Issue("required", f"{p}.run", "shell node needs `run` (a bash script)"))
        if kind == "function":
            call = n.get("call")
            if not isinstance(call, str) or ":" not in call:
                issues.append(Issue("required", f"{p}.call", "function node needs `call: package.module:function`"))
        if kind == "agent":
            if not n.get("prompt"):
                issues.append(Issue("required", f"{p}.prompt", "agent node needs `prompt` (or `prompt_file`)"))
            h = n.get("harness") or {}
            if h.get("name") not in KNOWN_HARNESSES:
                issues.append(Issue("harness", f"{p}.harness.name", f"unknown harness {h.get('name')!r}",
                                    f"one of {', '.join(KNOWN_HARNESSES)}"))
            eff = n.get("effects") or {}
            if eff and not isinstance(eff, dict):
                issues.append(Issue("effects", f"{p}.effects", "effects must be a mapping"))
            am = eff.get("amend") if isinstance(eff, dict) else None
            if am is not None and not isinstance(am, (bool, dict)):
                issues.append(Issue("effects", f"{p}.effects.amend", "amend must be true or a policy mapping"))
            if isinstance(am, dict):
                if "auto_approve" in am and not isinstance(am["auto_approve"], bool):
                    issues.append(Issue("effects", f"{p}.effects.amend.auto_approve", "must be true or false (not a string)"))
                if "max_nodes" in am and not (isinstance(am["max_nodes"], int) and am["max_nodes"] >= 0):
                    issues.append(Issue("effects", f"{p}.effects.amend.max_nodes", "must be a non-negative integer"))
                for fld in ("kinds", "ops"):
                    if fld in am and not (isinstance(am[fld], list) and all(isinstance(k, str) for k in am[fld])):
                        issues.append(Issue("effects", f"{p}.effects.amend.{fld}", "must be a list of strings"))
        if n.get("environment") is not None:
            issues.extend(_environment_issues(plan, n, p))
        if kind in ("shell", "function"):
            if n.get("cluster") is not None and n["cluster"] not in (plan.get("clusters") or {}):
                issues.append(Issue("cluster", f"{p}.cluster", f"unknown cluster {n['cluster']!r}",
                                    f"declare it under top-level `clusters:` (known: {', '.join(plan.get('clusters') or {}) or 'none'})"))
            if n.get("cluster") is None:
                stray = [k for k in ("stage_in", "retrieve", "resources", "modules", "prelude") if n.get(k)]
                if stray:
                    issues.append(Issue("cluster", f"{p}.{stray[0]}", f"`{stray[0]}` only applies to a node that runs on a cluster",
                                        "add `cluster: <name>`, or drop it"))
            elif n.get("cwd") or n.get("shell"):
                k = "cwd" if n.get("cwd") else "shell"
                issues.append(Issue("cluster", f"{p}.{k}", f"`{k}` is not supported on a cluster node",
                                    "the payload runs in its own attempt directory on the cluster, under bash"))
        if kind == "job":
            cl = n.get("cluster")
            if not cl or cl not in (plan.get("clusters") or {}):
                issues.append(Issue("cluster", f"{p}.cluster", f"unknown cluster {cl!r}",
                                    f"declare it under top-level `clusters:` (known: {', '.join(plan.get('clusters') or {}) or 'none'})"))
            if not n.get("script"):
                issues.append(Issue("required", f"{p}.script", "job node needs `script` (body of the batch script)"))
            for fld in ("poll", "deadline"):
                try:
                    parse_duration(n.get(fld))
                except ValueError as exc:
                    issues.append(Issue("duration", f"{p}.{fld}", str(exc)))
        if kind == "gate":
            dec = n.get("decisions")
            if not isinstance(dec, list) or not dec or not all(isinstance(d, str) for d in dec):
                issues.append(Issue("decisions", f"{p}.decisions", "decisions must be a non-empty list of strings"))
            orj = n.get("on_reject")
            if orj is not None and not (isinstance(orj, dict) and isinstance(orj.get("rerun", []), list)):
                issues.append(Issue("on_reject", f"{p}.on_reject", "on_reject must be {rerun: [node ids], max_attempts: N}"))
            elif orj:
                for r in orj.get("rerun", []):
                    if r not in ids:
                        issues.append(Issue("unknown_ref", f"{p}.on_reject.rerun", f"unknown node {r!r}"))
        if kind == "wait":
            if not (n.get("signal") or n.get("timer")):
                issues.append(Issue("required", p, "wait node needs `signal` and/or `timer`"))
            for fld in ("deadline", "timer"):
                try:
                    parse_duration(n.get(fld))
                except ValueError as exc:
                    issues.append(Issue("duration", f"{p}.{fld}", str(exc)))
    for k in plan:
        if k not in TOP_KEYS and not k.startswith("_"):
            issues.append(Issue("unknown_field", k, f"unknown top-level field {k!r}",
                                f"valid fields: {', '.join(sorted(TOP_KEYS))}"))
    for r in plan.get("results") or []:
        if r not in ids:
            issues.append(Issue("unknown_ref", "results", f"results lists unknown node {r!r}"))
    if not any(i["code"] in ("unknown_ref", "node_id", "duplicate_id") for i in issues):
        cyc = find_cycle(plan)
        if cyc:
            issues.append(Issue("cycle", "nodes", "dependency cycle: " + " -> ".join(cyc),
                                "Break the cycle; use a gate with on_reject.rerun for iterative loops."))
    return issues


def find_cycle(plan: dict) -> list[str] | None:
    graph = {n["id"]: effective_needs(n) for n in plan["nodes"] if isinstance(n, dict) and "id" in n}
    color: dict[str, int] = {}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = 1
        stack.append(u)
        for v in graph.get(u, []):
            if color.get(v) == 1:
                return stack[stack.index(v):] + [v]
            if color.get(v, 0) == 0 and v in graph:
                r = dfs(v)
                if r:
                    return r
        stack.pop()
        color[u] = 2
        return None

    for u in graph:
        if color.get(u, 0) == 0:
            r = dfs(u)
            if r:
                return r
    return None


def check(raw: dict) -> dict:
    """Normalise + validate; raise PlanInvalid with all issues. Returns the normalised plan."""
    plan = normalize(raw)
    issues = validate(plan)
    if issues:
        raise PlanInvalid(issues)
    return plan


# ====================================================================== identity

def contract_view(plan: dict) -> dict:
    """The part of a plan an approval binds to (drops loader metadata)."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def plan_digest(plan: dict) -> str:
    return digest(contract_view(plan))


# keys that do not change what a step computes: editing them keeps cached results
DECL_EXCLUDE = {"title", "description", "tags", "retry", "timeout", "resources", "poll", "deadline", "cache", "tmpdir"}


def decl_hash(node: dict) -> str:
    """Identity of *what a node does* (scheduler resources and cosmetics excluded, as AiiDA does)."""
    return digest({k: v for k, v in node.items() if k not in DECL_EXCLUDE})


# ====================================================================== graph helpers

class Graph:
    def __init__(self, plan: dict):
        self.plan = plan
        self.nodes: dict[str, dict] = {n["id"]: n for n in plan["nodes"]}
        self.order = [n["id"] for n in plan["nodes"]]
        self.needs = {nid: effective_needs(n) for nid, n in self.nodes.items()}
        self.children: dict[str, list[str]] = defaultdict(list)
        for nid, deps in self.needs.items():
            for d in deps:
                self.children[d].append(nid)

    def topo(self) -> list[str]:
        indeg = {n: len([d for d in self.needs[n] if d in self.nodes]) for n in self.order}
        ready = [n for n in self.order if indeg[n] == 0]
        out: list[str] = []
        while ready:
            u = ready.pop(0)
            out.append(u)
            for c in self.children.get(u, []):
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        out += [n for n in self.order if n not in out]
        return out

    def descendants(self, nid: str) -> list[str]:
        seen: list[str] = []
        todo = list(self.children.get(nid, []))
        while todo:
            c = todo.pop(0)
            if c not in seen:
                seen.append(c)
                todo.extend(self.children.get(c, []))
        return [n for n in self.topo() if n in seen]

    def ancestors(self, nid: str) -> set[str]:
        seen: set[str] = set()
        todo = list(self.needs.get(nid, []))
        while todo:
            c = todo.pop()
            if c not in seen:
                seen.add(c)
                todo.extend(self.needs.get(c, []))
        return seen

    def depth(self) -> dict[str, int]:
        d: dict[str, int] = {}
        for n in self.topo():
            d[n] = 1 + max((d.get(p, 0) for p in self.needs[n] if p in self.nodes), default=-1)
        return d


# ====================================================================== amendments

AMEND_OPS = ("add", "replace", "drop", "detour", "set_needs", "stop", "add_clusters", "tune_clusters", "add_inputs")
# cluster settings that only pace the work (how much of the machine, how often to poll): a running plan may change
# them (op tune_clusters); where and how a step runs (host, paths, prelude, scheduler) stays fixed
CLUSTER_TUNABLE = ("cpus", "max_jobs", "min_poll")


def apply_amendment(plan: dict, ops: list[dict], node_status: dict[str, str]) -> tuple[dict, dict]:
    """Apply amendment ops to a (normalised) plan; return (new_plan, effects).

    ``node_status`` maps node id -> status, used to forbid silent edits of history:
    only *pending* nodes may be replaced/dropped/re-wired; a finished node can only be replaced with
    ``supersede: true``, which marks it and its downstream cone stale (``effects['stale']``).
    """
    new = copy.deepcopy(contract_view(plan))
    if plan.get("_source"):
        new["_source"] = plan["_source"]   # new environment steps find their recipe next to the plan file
    defaults = new.get("defaults") or {}
    effects: dict[str, list] = {"stale": [], "stop": [], "added": [], "removed": [], "changed": []}
    issues: list[Issue] = []
    by_id = {n["id"]: n for n in new["nodes"]}

    def is_pending(nid: str) -> bool:
        return node_status.get(nid, "pending") in ("pending", "ready")

    for i, op in enumerate(ops or []):
        p = f"ops[{i}]"
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in AMEND_OPS:
            issues.append(Issue("amend_op", p, f"unknown op {kind!r}", f"one of {', '.join(AMEND_OPS)}"))
            continue
        if kind == "add":
            for spec in _as_list(op.get("nodes") or op.get("node")):
                if not isinstance(spec, dict) or "id" not in spec:
                    issues.append(Issue("amend_add", p, "each added node needs an `id`"))
                    continue
                if spec["id"] in by_id:
                    issues.append(Issue("amend_add", p, f"node {spec['id']!r} already exists",
                                        "use `replace` to change a pending node"))
                    continue
                nn = normalize_node(spec, defaults)
                new["nodes"].append(nn)
                by_id[nn["id"]] = nn
                effects["added"].append(nn["id"])
        elif kind == "replace":
            nid = op.get("node")
            spec = op.get("with")
            if nid not in by_id or not isinstance(spec, dict):
                issues.append(Issue("amend_replace", p, f"replace needs an existing `node` and a `with:` spec (got {nid!r})"))
                continue
            if not is_pending(nid) and not op.get("supersede"):
                issues.append(Issue("amend_history", p, f"node {nid!r} is {node_status.get(nid)}; history is immutable",
                                    "add `supersede: true` to re-run it (it and its downstream become stale)"))
                continue
            if op.get("supersede") and node_status.get(nid) in ("running", "waiting", "retrying"):
                issues.append(Issue("amend_active", p, f"node {nid!r} is {node_status.get(nid)}; cannot supersede active work",
                                    f"cancel it first (flower cancel RUN --node {nid}), then amend"))
                continue
            spec = dict(spec)
            spec["id"] = nid
            nn = normalize_node(spec, defaults)
            idx = next(j for j, n in enumerate(new["nodes"]) if n["id"] == nid)
            new["nodes"][idx] = nn
            by_id[nid] = nn
            effects["changed"].append(nid)
            if not is_pending(nid):
                effects["stale"].append(nid)
        elif kind == "drop":
            for nid in _as_list(op.get("nodes") or op.get("node")):
                if nid not in by_id:
                    issues.append(Issue("amend_drop", p, f"unknown node {nid!r}"))
                    continue
                if not is_pending(nid):
                    issues.append(Issue("amend_history", p, f"cannot drop {nid!r}: it is {node_status.get(nid)}",
                                        "use `stop` for pending work; finished history stays in the record"))
                    continue
                new["nodes"] = [n for n in new["nodes"] if n["id"] != nid]
                by_id.pop(nid)
                effects["removed"].append(nid)
        elif kind == "detour":
            after = op.get("after")
            specs = _as_list(op.get("nodes"))
            if after not in by_id or not specs:
                issues.append(Issue("amend_detour", p, "detour needs `after: <existing node>` and `nodes: [...]`"))
                continue
            added = []
            for spec in specs:
                if not isinstance(spec, dict) or "id" not in spec or spec["id"] in by_id:
                    issues.append(Issue("amend_detour", p, f"bad or duplicate detour node {spec!r:.60}"))
                    continue
                spec = dict(spec)
                spec.setdefault("needs", [after] if not added else [added[-1]])
                nn = normalize_node(spec, defaults)
                new["nodes"].append(nn)
                by_id[nn["id"]] = nn
                added.append(nn["id"])
            if not added:
                continue
            effects["added"].extend(added)
            leaf = added[-1]
            for n in new["nodes"]:
                if n["id"] in added:
                    continue
                if after in effective_needs(n) and is_pending(n["id"]):
                    n["needs"] = list(dict.fromkeys(n["needs"] + [leaf]))
                    effects["changed"].append(n["id"])
        elif kind == "set_needs":
            nid = op.get("node")
            if nid not in by_id:
                issues.append(Issue("amend_needs", p, f"unknown node {nid!r}"))
                continue
            if not is_pending(nid):
                issues.append(Issue("amend_history", p, f"cannot re-wire {nid!r}: it is {node_status.get(nid)}"))
                continue
            by_id[nid]["needs"] = [str(x) for x in _as_list(op.get("needs"))]
            effects["changed"].append(nid)
        elif kind == "add_clusters":
            cl = op.get("clusters")
            if not isinstance(cl, dict) or not cl:
                issues.append(Issue("amend_clusters", p, "add_clusters needs `clusters: {name: {...}}`"))
                continue
            for name, spec in cl.items():
                if name in (new.get("clusters") or {}):
                    issues.append(Issue("amend_clusters", p, f"cluster {name!r} already exists; a run's clusters "
                                        "are fixed once defined", "use a new cluster name, or `flower fork` the run"))
                    continue
                new.setdefault("clusters", {})[name] = copy.deepcopy(spec)
        elif kind == "tune_clusters":
            cl = op.get("clusters")
            if not isinstance(cl, dict) or not cl:
                issues.append(Issue("amend_clusters", p, "tune_clusters needs `clusters: {name: {cpus: N, ...}}`"))
                continue
            for name, spec in cl.items():
                cur = (new.get("clusters") or {}).get(name)
                bad = sorted(k for k in (spec or {}) if k not in CLUSTER_TUNABLE)
                if cur is None or not isinstance(spec, dict) or bad:
                    issues.append(Issue("amend_clusters", p, f"cannot tune cluster {name!r}" + (
                        f": only {', '.join(CLUSTER_TUNABLE)} may change in a running plan, not {', '.join(bad)}"
                        if bad else ": no such cluster"), "use a new cluster name, or `flower fork` the run"))
                    continue
                for k, v in spec.items():   # null removes the setting
                    if v is None:
                        cur.pop(k, None)
                    else:
                        cur[k] = copy.deepcopy(v)
        elif kind == "add_inputs":
            ins = op.get("inputs")
            if not isinstance(ins, dict) or not ins:
                issues.append(Issue("amend_inputs", p, "add_inputs needs `inputs: {name: {type, default}}`"))
                continue
            for name, decl in ins.items():
                if name in (new.get("inputs") or {}):
                    issues.append(Issue("amend_inputs", p, f"input {name!r} already exists; a run's inputs are fixed"))
                    continue
                if not isinstance(decl, dict) or "default" not in decl:
                    issues.append(Issue("amend_inputs", p, f"new input {name!r} needs a value",
                                        f"give it `default:` in the plan file, or pass `-i {name}=VALUE`"))
                    continue
                new.setdefault("inputs", {})[name] = copy.deepcopy(decl)
        elif kind == "stop":
            for nid in _as_list(op.get("nodes") or op.get("node")):
                if nid in by_id and is_pending(nid):
                    effects["stop"].append(nid)
                else:
                    issues.append(Issue("amend_stop", p, f"can only stop pending nodes ({nid!r} is {node_status.get(nid, 'unknown')})"))
    if issues:
        raise PlanInvalid(issues)
    new = normalize(new)
    vissues = validate(new)
    if vissues:
        raise PlanInvalid(vissues)
    if effects["stale"]:
        g = Graph(new)
        cone: list[str] = []
        for nid in effects["stale"]:
            for x in [nid] + g.descendants(nid):
                if x not in cone:
                    cone.append(x)
        effects["stale"] = cone
    return new, effects


def diff_plans(old: dict, new: dict) -> dict:
    """Human-oriented diff: added / removed / changed (with changed fields) / unchanged node ids."""
    a = {n["id"]: n for n in old.get("nodes", [])}
    b = {n["id"]: n for n in new.get("nodes", [])}
    changed = []
    for nid in a.keys() & b.keys():
        if a[nid] != b[nid]:
            fields = sorted(k for k in set(a[nid]) | set(b[nid]) if a[nid].get(k) != b[nid].get(k))
            changed.append({"id": nid, "fields": fields})
    return {
        "added": [n for n in b if n not in a],
        "removed": [n for n in a if n not in b],
        "changed": sorted(changed, key=lambda c: c["id"]),
        "unchanged": sorted(n for n in a.keys() & b.keys() if a[n] == b[n]),
    }


def dump_yaml(plan: dict) -> str:
    return yaml.safe_dump(contract_view(plan), sort_keys=False, allow_unicode=True, width=100)
