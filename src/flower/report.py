"""Run report (Markdown + self-contained HTML) and machine-readable audit.

The report answers, for someone who was not there: what was the goal, what ran, what came out,
which decisions were made by whom and why, what changed in the plan along the way, what it cost,
and where every file is. Everything is derived from the journal.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import __version__
from .plan import on_cluster
from .render import ICON, describe_event, kind_label, what
from .state import NodeState, RunState
from .util import (atomic_write_text, fmt_duration, local_clock, local_stamp, local_zone, oneline, parse_iso,
                   seconds_since, short, truncate)


def _elapsed(st: RunState) -> float | None:
    if st.completed_at:
        return (parse_iso(st.completed_at) - parse_iso(st.created_at)).total_seconds()
    return seconds_since(st.created_at)


def rel(path: str | None, base: Path) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")


def mermaid(st: RunState) -> str:
    g = st.graph()
    lines = ["flowchart TD"]
    ids = {n: f"n{i}" for i, n in enumerate(g.order)}
    for nid in g.topo():
        spec = g.nodes[nid]
        ns = st.nodes.get(nid) or NodeState(nid)
        label = f"{ICON.get(ns.status, '')} {nid}<br/><small>{kind_label(spec)}</small>".replace('"', "'")
        shape = ("{{", "}}") if spec.get("kind") == "gate" else ("([", "])") if spec.get("kind") == "agent" else \
            ("[[", "]]") if on_cluster(spec) else ("[", "]")
        lines.append(f'  {ids[nid]}{shape[0]}"{label}"{shape[1]}:::{ns.status}')
    for nid in g.order:
        for d in g.needs[nid]:
            if d in ids:
                lines.append(f"  {ids[d]} --> {ids[nid]}")
    lines += ["  classDef succeeded fill:#d9f2e3,stroke:#1f7a45", "  classDef failed fill:#f9dada,stroke:#b42318",
              "  classDef running fill:#dbeafe,stroke:#1d4ed8", "  classDef waiting fill:#fff4d6,stroke:#b54708",
              "  classDef pending fill:#f2f4f7,stroke:#98a2b3", "  classDef skipped fill:#f2f4f7,stroke:#d0d5dd,color:#98a2b3",
              "  classDef cancelled fill:#f9dada,stroke:#b42318", "  classDef retrying fill:#f4ebff,stroke:#6941c6"]
    return "\n".join(lines)


def _final_outputs(st: RunState) -> list[tuple[str, NodeState]]:
    """Nodes whose results the report leads with: the plan's `results:` list, else the 'real' sinks
    (gates/waits are looked through to the work they guard)."""
    g = st.graph()
    chosen = [n for n in (st.plan.get("results") or []) if n in g.nodes]
    if not chosen:
        frontier = [n for n in g.order if not g.children.get(n)]
        seen: set[str] = set()
        while frontier:
            n = frontier.pop(0)
            if n in seen:
                continue
            seen.add(n)
            if g.nodes[n].get("kind") in ("gate", "wait") or g.nodes[n].get("expanded_from"):
                frontier.extend(g.needs[n])
            else:
                chosen.append(n)
    return [(n, st.nodes[n]) for n in chosen if n in st.nodes and st.nodes[n].result]


def build_markdown(eng) -> str:
    st = eng.state()
    g = st.graph()
    cost = st.cost()
    counts = st.counts()
    md = [f"# {st.title or st.plan_id}", "",
          f"**Run** `{st.run_id}` · **status** `{st.status}` · **elapsed** {fmt_duration(_elapsed(st))} · "
          f"**nodes** {counts.get('succeeded', 0)}/{len(g.order)} succeeded"
          + (f", {counts['failed']} failed" if counts.get("failed") else "")
          + (f" · **agent cost** ${cost['usd']:.2f}" if cost["usd"] else ""), ""]
    if st.status_reason:
        md += [f"> {oneline(st.status_reason, 300)}", ""]
    desc = st.plan.get("description")
    if desc:
        md += ["## Goal", "", desc.strip(), ""]
    if st.inputs:
        md += ["**Inputs:** " + ", ".join(f"`{k}` = `{truncate(json.dumps(v, default=str), 80)}`" for k, v in st.inputs.items()), ""]

    finals = _final_outputs(st)
    if finals:
        md += ["## Results", ""]
        for nid, ns in finals:
            r = ns.result
            md.append(f"- **{nid}** — {oneline(r.summary, 300) or 'done'}")
            for k, v in list(r.outputs.items())[:12]:
                if k in ("summary", "rationale"):
                    continue
                md.append(f"  - `{k}`: {oneline(json.dumps(v, default=str, ensure_ascii=False), 160)}")
            for k, f in r.files.items():
                md.append(f"  - file `{k}`: `{rel(f.get('path'), eng.paths.dir)}`")
                if str(f.get("path", "")).lower().endswith(IMAGE_EXT):
                    md.append(f"\n![{k}]({rel(f.get('path'), eng.paths.dir)})\n")
        md.append("")

    md += ["## Workflow", "", "```mermaid", mermaid(st), "```", ""]
    md += ["| | node | kind | time | result |", "|---|---|---|---|---|"]
    for nid in g.topo():
        ns = st.nodes.get(nid) or NodeState(nid)
        a = ns.last
        res = (ns.result.summary if ns.result else "") or ((a.error or {}).get("message") if a and a.error else "") \
            or (ns.skipped_reason or "")
        t = fmt_duration(a.duration_s) if a and a.duration_s is not None else ""
        md.append(f"| {ICON.get(ns.status, '')} | `{nid}` | {kind_label(g.nodes[nid])} | {t} | "
                  f"{oneline(str(res), 140)} |")
    md.append("")

    # decisions
    md += ["## Decisions and approvals", ""]
    plan_gate = st.gates.get("plan")
    if plan_gate and plan_gate.status == "answered":
        md.append(f"- **Plan** {plan_gate.decision} by `{plan_gate.by}` at {local_stamp(plan_gate.answered_at)}"
                  + (f" — “{oneline(plan_gate.text, 300)}”" if plan_gate.text else "") + f" (digest `{short(st.base_digest)}`)")
    for gate in sorted(st.gates.values(), key=lambda x: x.requested_at):
        if gate.id == "plan" or gate.subject == "amendment":
            continue
        line = f"- **{gate.node or gate.id}**: " + (f"{gate.decision!r} by `{gate.by}`" if gate.status == "answered" else "*still open*")
        if gate.text:
            line += f" — “{oneline(gate.text, 400)}”"
        md.append(line)
    if not st.gates:
        md.append("- (none)")
    md.append("")

    if st.amendments:
        fan = [a for a in st.amendments.values() if a.proposed_by == "flower" and a.id.startswith("fx-")]
        real = [a for a in st.amendments.values() if a not in fan]
        md += ["## How the plan changed", ""]
        if not real:
            md.append("- No amendments: the approved plan ran as written.")
        for am in sorted(real, key=lambda a: a.proposed_at):
            verdict = {"approved": f"approved → generation {am.generation} by `{am.decided_by}`",
                       "rejected": f"rejected by `{am.decided_by}`" + (f": {am.effects.get('reason')}" if am.effects.get("reason") else ""),
                       "proposed": "awaiting decision"}[am.status]
            md.append(f"- **{am.id}** proposed by `{am.proposed_by}` — {verdict}")
            if am.rationale:
                md.append(f"  - why: {oneline(am.rationale, 600)}")
            d = am.diff or {}
            for op in am.ops:
                if not isinstance(op, dict):
                    continue
                for nd in op.get("nodes") or ([op.get("with")] if isinstance(op.get("with"), dict) else []):
                    if isinstance(nd, dict):
                        md.append(f"  - {op.get('op')} `{nd.get('id')}` ({nd.get('kind')}): {truncate(what(nd), 140)}")
            if d.get("changed"):
                md.append("  - re-wired: " + ", ".join(f"`{c['id']}`" for c in d["changed"]))
        if fan:
            md.append("- Fan-out: " + "; ".join(
                f"`{(a.source_node or '?')}` → {len(a.diff.get('added') or [])} items" for a in sorted(fan, key=lambda a: a.proposed_at)))
        md.append("")

    md += ["## Node details", ""]
    for nid in g.topo():
        spec = g.nodes[nid]
        ns = st.nodes.get(nid) or NodeState(nid)
        md += [f"### {ICON.get(ns.status, '')} {nid} — {kind_label(spec)}", ""]
        w = what(spec)
        if w:
            md += [f"*{w}*", ""]
        if ns.skipped_reason and ns.status == "skipped":
            md += [f"Skipped: {ns.skipped_reason}", ""]
        for a in ns.attempts:
            head = f"**attempt {a.n}** — {a.status}"
            if a.duration_s is not None:
                head += f", {fmt_duration(a.duration_s)}"
            if a.reused_from:
                head += f" (reused {a.reused_from})"
            md.append(head)
            if a.summary:
                md.append(f"- summary: {oneline(a.summary, 400)}")
            if a.rationale:
                md.append(f"- rationale: {oneline(a.rationale, 1200)}")
            if a.job:
                md.append(f"- slurm job `{a.job.get('job_id')}` on `{a.job.get('cluster')}` ({a.job.get('state')}), dir `{a.job.get('job_dir')}`")
            if a.session:
                md.append(f"- agent session `{a.session}`" + (f", {a.repairs} repair turn(s)" if a.repairs else ""))
            u = a.usage or {}
            if u.get("cost_usd") is not None or u.get("input_tokens"):
                md.append(f"- usage: {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out tokens"
                          + (f", ${u['cost_usd']:.4f}" if u.get("cost_usd") is not None else ""))
            if a.outputs:
                md.append("- outputs: `" + truncate(json.dumps(a.outputs, default=str, ensure_ascii=False), 600).replace("`", "'") + "`")
            for k, f in (a.files or {}).items():
                md.append(f"- file `{k}`: `{rel(f.get('path'), eng.paths.dir)}`" + (f" ({f.get('bytes')} B, sha256 `{(f.get('sha256') or '')[:12]}`)" if f.get("sha256") else ""))
            if a.error:
                md.append(f"- **error** `{a.error.get('error_class')}`: {oneline(a.error.get('message'), 500)}")
            md.append("")

    md += ["## Timeline", "", f"Times in {local_zone()}.", "", "```"]
    t0 = None
    for ev in eng.journal.read():
        d = describe_event(ev)
        if not d:
            continue
        t0 = t0 or parse_iso(ev["occurredAtIso"])
        since = fmt_duration((parse_iso(ev["occurredAtIso"]) - t0).total_seconds())
        md.append(f"{local_clock(ev['occurredAtIso'])} +{since:>6}  {oneline(d, 400).replace('ʼʼʼ', chr(39) * 3)}")
    md += ["```", ""]
    md += ["## Provenance", "",
           f"- plan `{st.plan_id}`, base digest `{st.base_digest}`, current generation {st.generation} (`{st.digest}`)",
           f"- created {st.created_at} by `{st.meta.get('user')}` on `{st.meta.get('host')}` with flower {st.meta.get('flower_version')}",
           f"- run directory `{eng.paths.dir}` — the journal `events.jsonl` is the complete record ({st.last_seq} events)",
           f"- report generated by flower {__version__}", ""]
    if st.notes:
        md += ["## Notes", ""] + [f"- {local_stamp(n['at'])} `{n['by']}`: {oneline(n['text'], 400)}" for n in st.notes] + [""]
    return "\n".join(md)


HTML_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#ffffff; --fg:#1d2939; --muted:#667085; --line:#e4e7ec; --code:#f6f8fa; --accent:#1d4ed8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --fg:#e4e7ec; --muted:#98a2b3; --line:#2a2f3a; --code:#171a21; --accent:#7aa2ff; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       max-width:1080px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:26px; margin:0 0 6px; }} h2 {{ margin-top:34px; border-bottom:1px solid var(--line); padding-bottom:4px; }}
h3 {{ margin-bottom:4px; }} code,pre {{ background:var(--code); border-radius:6px; font-size:13px; }}
code {{ padding:1px 4px; }} pre {{ padding:12px; overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th,td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }}
blockquote {{ margin:0; padding:8px 12px; border-left:3px solid var(--accent); color:var(--muted); }}
.mermaid {{ background:var(--code); border-radius:8px; padding:12px; }}
</style>
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
mermaid.initialize({{ startOnLoad: true, theme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default' }});
</script></head><body>
{body}
</body></html>"""


def md_to_html(md: str, base: Path | None = None) -> str:
    """Tiny Markdown renderer for our own report subset (headings, lists, tables, code, mermaid)."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0

    def inline(s: str) -> str:
        s = html.escape(s)
        import re
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        return s

    in_list = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            lang = ln[3:].strip()
            j = i + 1
            block = []
            while j < len(lines) and not lines[j].startswith("```"):
                block.append(lines[j])
                j += 1
            if in_list:
                out.append("</ul>" * in_list)
                in_list = 0
            if lang == "mermaid":
                out.append('<pre class="mermaid">' + html.escape("\n".join(block)) + "</pre>")
            else:
                out.append("<pre>" + html.escape("\n".join(block)) + "</pre>")
            i = j + 1
            continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in rows[0]) + "</tr></thead><tbody>")
            for r in rows[2:]:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue
        stripped = ln.lstrip()
        indent = (len(ln) - len(stripped)) // 2
        if stripped.startswith("- "):
            level = indent + 1
            while in_list < level:
                out.append("<ul>")
                in_list += 1
            while in_list > level:
                out.append("</ul>")
                in_list -= 1
            out.append(f"<li>{inline(stripped[2:])}</li>")
            i += 1
            continue
        if in_list:
            out.append("</ul>" * in_list)
            in_list = 0
        if ln.startswith("### "):
            out.append(f"<h3>{inline(ln[4:])}</h3>")
        elif ln.startswith("## "):
            out.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            out.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.startswith("> "):
            out.append(f"<blockquote>{inline(ln[2:])}</blockquote>")
        elif ln.startswith("![") and ln.rstrip().endswith(")") and "](" in ln:
            alt, src = ln[2:].split("](", 1)
            src = src.rstrip()[:-1]
            out.append(_img(alt, src, base))
        elif ln.strip():
            out.append(f"<p>{inline(ln)}</p>")
        i += 1
    if in_list:
        out.append("</ul>" * in_list)
    return "\n".join(out)


def _img(alt: str, src: str, base: Path | None) -> str:
    import base64
    import mimetypes
    p = Path(src) if Path(src).is_absolute() or base is None else Path(base) / src
    try:
        if p.is_file() and p.stat().st_size < 8_000_000:
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            data = base64.b64encode(p.read_bytes()).decode()
            return f'<figure><img alt="{html.escape(alt)}" src="data:{mime};base64,{data}" style="max-width:100%"><figcaption>{html.escape(alt)}</figcaption></figure>'
    except OSError:
        pass
    return f'<p><a href="{html.escape(src)}">{html.escape(alt)}</a></p>'


def write_report(eng) -> dict:
    md = build_markdown(eng)
    st = eng.state()
    atomic_write_text(eng.paths.report_md, md)
    atomic_write_text(eng.paths.report_html, HTML_SHELL.format(title=html.escape(st.title or st.run_id), body=md_to_html(md, eng.paths.dir)))
    return {"md": str(eng.paths.report_md), "html": str(eng.paths.report_html)}


def audit(eng) -> dict:
    """Stable machine-readable record (schema 'flower.audit/1')."""
    st = eng.state()
    g = st.graph()
    return {
        "schema": "flower.audit/1",
        "run": {"id": st.run_id, "plan_id": st.plan_id, "title": st.title, "status": st.status, "reason": st.status_reason,
                "created_at": st.created_at, "completed_at": st.completed_at, "inputs": st.inputs, "meta": st.meta,
                "cost": st.cost(), "events": st.last_seq},
        "plan": {"base_digest": st.base_digest, "digest": st.digest, "generation": st.generation,
                 "generations": [{k: v for k, v in gen.items() if k != "plan"} for gen in st.generations],
                 "approved_by": st.approved_by},
        "nodes": {nid: {"kind": g.nodes[nid].get("kind"), "status": st.nodes[nid].status if nid in st.nodes else "pending",
                        "needs": g.needs[nid],
                        "attempts": [a.__dict__ for a in (st.nodes[nid].attempts if nid in st.nodes else [])]}
                  for nid in g.topo()},
        "gates": {gid: gt.__dict__ for gid, gt in st.gates.items()},
        "amendments": {aid: am.__dict__ for aid, am in st.amendments.items()},
        "notes": st.notes,
    }
