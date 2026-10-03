"""Export a run as a Workflow Run RO-Crate (Provenance Run Crate profile 0.5).

https://www.researchobject.org/workflow-run-crate/profiles/provenance_run_crate/
Mapping: plan.yaml -> ComputationalWorkflow; run -> CreateAction (instrument: workflow);
node -> HowToStep + SoftwareApplication; attempt -> CreateAction (instrument: step tool) wrapped by a
ControlAction; forgeflow itself -> OrganizeAction; humans/agents -> Person/SoftwareApplication agents;
files -> File with sha256. Gate decisions and amendments are kept as additional CreateActions.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import __version__
from .util import atomic_write_text

PROFILES = [
    {"@id": "https://w3id.org/ro/wfrun/process/0.5", "@type": "CreativeWork", "name": "Process Run Crate", "version": "0.5"},
    {"@id": "https://w3id.org/ro/wfrun/workflow/0.5", "@type": "CreativeWork", "name": "Workflow Run Crate", "version": "0.5"},
    {"@id": "https://w3id.org/ro/wfrun/provenance/0.5", "@type": "CreativeWork", "name": "Provenance Run Crate", "version": "0.5"},
    {"@id": "https://w3id.org/workflowhub/workflow-ro-crate/1.0", "@type": "CreativeWork", "name": "Workflow RO-Crate", "version": "1.0"},
]
STATUS = {"succeeded": "http://schema.org/CompletedActionStatus", "failed": "http://schema.org/FailedActionStatus",
          "cancelled": "http://schema.org/FailedActionStatus", "running": "http://schema.org/ActiveActionStatus"}


def _rel(path: str | None, base: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except ValueError:
        return "file://" + str(Path(path).resolve())


def build_crate(eng) -> dict:
    st = eng.state()
    base = eng.paths.dir
    g = st.graph()
    graph: list[dict] = []
    agents: dict[str, dict] = {}

    def agent(actor: str | None) -> dict:
        actor = actor or "system"
        aid = "#agent-" + actor.replace(":", "-").replace("/", "-")
        if aid not in agents:
            kind = "Person" if actor.startswith("human:") else "SoftwareApplication"
            agents[aid] = {"@id": aid, "@type": kind, "name": actor}
        return {"@id": aid}

    has_part: list[dict] = [{"@id": "plan.yaml"}, {"@id": "events.jsonl"}]
    steps, tools, actions, control = [], [], [], []
    for i, nid in enumerate(g.topo()):
        spec = g.nodes[nid]
        step_id, tool_id = f"#step-{nid}", f"#tool-{nid}"
        steps.append({"@id": step_id, "@type": "HowToStep", "name": spec.get("title") or nid, "position": str(i),
                      "workExample": {"@id": tool_id}})
        tools.append({"@id": tool_id, "@type": "SoftwareApplication", "name": nid,
                      "description": f"forgeflow {spec.get('kind')} node"
                      + (f" ({(spec.get('harness') or {}).get('name')})" if spec.get("kind") == "agent" else "")})
        ns = st.nodes.get(nid)
        for a in (ns.attempts if ns else []):
            act_id = f"#run-{nid}-a{a.n}"
            objs, results = [], []
            for k, f in (a.files or {}).items():
                fid = _rel(f.get("path"), base)
                if fid:
                    graph.append({"@id": fid, "@type": "File", "name": k, "sha256": f.get("sha256"),
                                  "contentSize": f.get("bytes"), "encodingFormat": Path(fid).suffix.lstrip(".") or None})
                    results.append({"@id": fid})
                    has_part.append({"@id": fid})
            pv_id = f"#outputs-{nid}-a{a.n}"
            if a.outputs:
                graph.append({"@id": pv_id, "@type": "PropertyValue", "name": f"{nid} outputs",
                              "value": json.dumps(a.outputs, default=str, ensure_ascii=False)})
                results.append({"@id": pv_id})
            if a.inputs:
                in_id = f"#inputs-{nid}-a{a.n}"
                graph.append({"@id": in_id, "@type": "PropertyValue", "name": f"{nid} inputs",
                              "value": json.dumps(a.inputs, default=str, ensure_ascii=False)})
                objs.append({"@id": in_id})
            act = {"@id": act_id, "@type": "CreateAction", "name": f"{nid} attempt {a.n}", "instrument": {"@id": tool_id},
                   "startTime": a.started_at, "endTime": a.ended_at, "actionStatus": STATUS.get(a.status),
                   "agent": agent(a.actor), "object": objs, "result": results}
            if a.summary:
                act["description"] = a.summary
            if a.rationale:
                act["disambiguatingDescription"] = a.rationale
            if a.error:
                act["error"] = f"{a.error.get('error_class')}: {a.error.get('message')}"
            if a.reused_from:
                act["sameAs"] = a.reused_from
            if a.session:
                act["identifier"] = a.session
            actions.append(act)
            control.append({"@id": f"#control-{nid}-a{a.n}", "@type": "ControlAction", "instrument": {"@id": step_id},
                            "object": {"@id": act_id}})
    for gt in st.gates.values():
        if gt.status == "answered":
            actions.append({"@id": f"#decision-{gt.id}", "@type": "CreateAction", "name": f"decision on {gt.node or gt.id}",
                            "agent": agent(gt.by), "endTime": gt.answered_at, "description": gt.message[:500],
                            "result": {"@type": "PropertyValue", "name": "decision", "value": gt.decision},
                            "disambiguatingDescription": gt.text})
    for am in st.amendments.values():
        actions.append({"@id": f"#amendment-{am.id}", "@type": "UpdateAction", "name": f"plan amendment {am.id}",
                        "agent": agent(am.proposed_by), "startTime": am.proposed_at, "description": am.rationale,
                        "actionStatus": "http://schema.org/CompletedActionStatus" if am.status == "approved"
                        else "http://schema.org/FailedActionStatus", "targetCollection": {"@id": "plan.yaml"}})
    run_action = {"@id": "#run", "@type": "CreateAction", "name": f"forgeflow run {st.run_id}",
                  "instrument": {"@id": "plan.yaml"}, "startTime": st.created_at, "endTime": st.completed_at,
                  "actionStatus": STATUS.get(st.status, "http://schema.org/ActiveActionStatus"),
                  "agent": agent(st.approved_by),
                  "object": [{"@id": "#run-inputs"}], "result": [r for r in has_part[2:]]}
    engine = {"@id": "#forgeflow", "@type": "SoftwareApplication", "name": "forgeflow", "version": __version__,
              "url": "https://github.com/ (forgeflow)"}
    organize = {"@id": "#organize", "@type": "OrganizeAction", "instrument": {"@id": "#forgeflow"},
                "object": [{"@id": c["@id"]} for c in control], "result": {"@id": "#run"},
                "startTime": st.created_at, "endTime": st.completed_at}
    workflow = {"@id": "plan.yaml", "@type": ["File", "SoftwareSourceCode", "ComputationalWorkflow", "HowTo"],
                "name": st.title, "description": st.plan.get("description"), "programmingLanguage": {"@id": "#forgeflow-lang"},
                "hasPart": [{"@id": t["@id"]} for t in tools], "step": [{"@id": s["@id"]} for s in steps],
                "sha256": None, "version": st.base_digest}
    root = {"@id": "./", "@type": "Dataset", "name": f"{st.title} — run {st.run_id}", "datePublished": st.completed_at or st.created_at,
            "description": (st.plan.get("description") or "").strip(), "license": "https://spdx.org/licenses/CC-BY-4.0",
            "mainEntity": {"@id": "plan.yaml"}, "mentions": [{"@id": "#run"}], "hasPart": has_part,
            "conformsTo": [{"@id": p["@id"]} for p in PROFILES]}
    meta = {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"},
            "conformsTo": [{"@id": "https://w3id.org/ro/crate/1.1"}, {"@id": "https://w3id.org/workflowhub/workflow-ro-crate/1.0"}]}
    lang = {"@id": "#forgeflow-lang", "@type": "ComputerLanguage", "name": "forgeflow plan", "version": "1"}
    inputs = {"@id": "#run-inputs", "@type": "PropertyValue", "name": "run inputs",
              "value": json.dumps(st.inputs, default=str, ensure_ascii=False)}
    journal = {"@id": "events.jsonl", "@type": "File", "name": "forgeflow journal (complete event log)",
               "encodingFormat": "application/x-ndjson"}
    return {"@context": "https://w3id.org/ro/crate/1.1/context",
            "@graph": [meta, root, workflow, lang, journal, inputs, run_action, engine, organize, *PROFILES,
                       *steps, *tools, *control, *actions, *agents.values(), *graph]}


def export_crate(eng) -> Path:
    crate = build_crate(eng)
    path = eng.paths.dir / "ro-crate-metadata.json"
    atomic_write_text(path, json.dumps(crate, indent=2, ensure_ascii=False, default=str))
    return path
