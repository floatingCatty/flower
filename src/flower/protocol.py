"""A finished run as a reproducibility protocol: the steps that produced a result, and the values to expect.

`flower export RUN STEP...` writes, next to the run's plan file (so `${plan.dir}` and `envs/` resolve the same):

    protocol.yaml    the plan file's own definitions of STEP and everything it depends on, nothing else
                     (probes, previews and side studies drop out by construction)
    expected.json    those steps' outputs in this run
    PROTOCOL.md      what it does, what it needs, how long it took, how to reproduce it

Reproducing is `flower run protocol.yaml --follow` and then `flower compare NEW_RUN expected.json`.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from . import plan as planmod
from .util import FlowerError, fmt_duration

SKIP = {"job_id", "job_dir", "local_dir", "dir", "prefix", "seconds", "seconds_opt", "summary", "rationale"}


def closure(st, steps: list[str]) -> tuple[set[str], set[str]]:
    """(the run's nodes the steps depend on, the plan-file ids among them): foreach items map to their step,
    generated environment steps are left to be regenerated."""
    g = st.graph()
    nodes: set[str] = set()
    for s in steps:
        if s not in g.nodes:
            raise FlowerError("node_not_found", f"no step {s!r} in run {st.run_id}")
        if (st.nodes.get(s) is None) or st.nodes[s].status != "succeeded":
            raise FlowerError("not_succeeded", f"step {s} has not succeeded; a protocol starts from a result")
        nodes |= {s} | g.ancestors(s)
    ids = {g.nodes[n].get("expanded_from") or n for n in nodes
           if not str(g.nodes[n].get("generated", "")).startswith("env:")}
    return nodes, ids


def export(eng, steps: list[str]) -> dict:
    st = eng.state()
    src = st.meta.get("plan_source")
    if not src or not Path(src).is_file():
        raise FlowerError("no_plan_file", f"run {st.run_id} has no plan file to export from ({src})")
    nodes, ids = closure(st, steps)
    drift = [n for n in eng.plan_edits(None)["changed"] if n in ids]
    if drift:
        raise FlowerError("plan_drift", f"the plan file differs from what ran for {', '.join(drift)}",
                          "export the version that ran: check it out in git (or `flower rerun RUN` first)")
    raw = yaml.safe_load(Path(src).read_text()) or {}
    proto = {k: v for k, v in raw.items() if k not in ("nodes", "environments")}
    g = st.graph()
    pins = {}   # the recipe versions the result was made with: a later `flower run` refuses any other
    for n in nodes:
        gen = str(g.nodes[n].get("generated", ""))
        if gen.startswith("env:"):
            name, h = gen.split(":", 2)[1:]
            pins[name] = h
    if pins:
        proto["environments"] = dict(sorted(pins.items()))
    used = {g.nodes[n].get("cluster") for n in nodes}
    if isinstance(proto.get("clusters"), dict):   # only the machines the protocol's steps run on
        proto["clusters"] = {k: v for k, v in proto["clusters"].items() if k in used}
    proto["nodes"] = [n for n in raw.get("nodes") or [] if isinstance(n, dict) and n.get("id") in ids]
    d = Path(src).parent
    from .devloop import _Dumper
    (d / "protocol.yaml").write_text(
        f"# A reproducibility protocol exported from run {st.run_id} (`flower export`): the steps behind\n"
        f"# {', '.join(steps)}. Run it with `flower run protocol.yaml --follow`, then `flower compare RUN expected.json`.\n"
        + yaml.dump(proto, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110))
    planmod.check(planmod.load_plan_file(str(d / "protocol.yaml")))    # it must stand on its own
    expected = {}
    for n in sorted(nodes):
        spec, ns = g.nodes[n], st.nodes.get(n)
        if str(spec.get("generated", "")).startswith("env:") or spec.get("foreach") is not None or not ns or not ns.result:
            continue
        expected[n] = {k: v for k, v in (ns.result.outputs or {}).items() if k not in SKIP}
    old = d / "expected.json"   # comparison rules recorded with `flower compare --save` stay with the protocol
    rules = (json.loads(old.read_text()).get("compare") if old.is_file() else None) or None
    old.write_text(json.dumps({"from_run": st.run_id, "environments": pins, "steps": expected,
                               **({"compare": rules} if rules else {})}, indent=1, default=str))
    (d / "PROTOCOL.md").write_text(readme(st, proto, steps, nodes))
    return {"dir": str(d), "steps": sorted(ids), "expected": len(expected),
            "files": [str(d / f) for f in ("protocol.yaml", "expected.json", "PROTOCOL.md")]}


def readme(st, proto: dict, steps: list[str], nodes: set[str]) -> str:
    g = st.graph()
    order = [n for n in g.topo() if n in nodes and not g.nodes[n].get("expanded_from")
             and not str(g.nodes[n].get("generated", "")).startswith("env:")]

    def took(n):
        kids = [c for c in nodes if g.nodes[c].get("expanded_from") == n] or [n]
        secs = [st.nodes[c].last.duration_s for c in kids if st.nodes.get(c) and st.nodes[c].last
                and st.nodes[c].last.duration_s is not None]
        return (f"{len(kids)} items, {fmt_duration(sum(secs))} in total" if len(kids) > 1 else fmt_duration(sum(secs))) \
            if secs else ""
    title, desc = (proto.get("title") or proto.get("id") or "").strip(), (proto.get("description") or "").strip()
    L = [f"# {title}", ""] + ([desc, ""] if desc and desc != title else []) + [
         f"Exported from run `{st.run_id}`: the steps behind {', '.join(f'`{s}`' for s in steps)}.", "",
         "## Reproduce", "", "```bash",
         "flower run protocol.yaml --follow" + (" --inputs my-inputs.json" if proto.get("inputs") else ""),
         "flower compare RUN expected.json      # RUN: the id the first command prints", "```", ""]
    ins = proto.get("inputs") or {}
    if ins:
        L += ["## Inputs", ""] + [f"- `{k}`: {(v or {}).get('description') or (v or {}).get('type', '')}"
                                  + (f" (default `{v['default']}`)" if isinstance(v, dict) and "default" in v else "")
                                  for k, v in ins.items()] + [""]
    envs = sorted({g.nodes[n].get("environment") for n in nodes if g.nodes[n].get("environment")})
    cls = sorted({g.nodes[n].get("cluster") for n in nodes if g.nodes[n].get("cluster")} - {"local"})
    if envs or cls:
        L += ["## Needs", ""]
        L += [f"- machines: {', '.join(cls)} (see `clusters:` in protocol.yaml)"] if cls else []
        L += [f"- software: {', '.join(f'envs/{e}' for e in envs)} (frozen recipes, installed by the run; "
              "`environments:` in protocol.yaml pins their versions)"] if envs else []
        L.append("")
    L += ["## Steps", "", "| step | what it establishes | took |", "|---|---|---|"]
    for n in order:
        spec = g.nodes[n]
        L.append(f"| `{n}` | {(spec.get('description') or spec.get('title') or '').strip()} | {took(n)} |")
    return "\n".join(L) + "\n"


def load_expected(path: Path) -> dict:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or "steps" not in data:
        raise FlowerError("bad_expected", f"{path} is not an expected.json written by `flower export`")
    return data
