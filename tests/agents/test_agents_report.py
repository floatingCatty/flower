"""report.md / report.html / audit for agent runs (in-process), incl. agent-controlled text."""
from __future__ import annotations

import json
import re

import pytest

from agentkit import agent_node, make_plan
from flower.report import audit, build_markdown, md_to_html, write_report


def h2s(html: str) -> list[str]:
    return re.findall(r"<h2>(.*?)</h2>", html)


def test_report_sections_cost_and_html_shell(fake, run_plan):
    f = fake("claude", [{"text": "no json"}, {"answer": {"energy": 1.5, "summary": "relaxed", "rationale": "PBE"}}])
    eng, st = run_plan(make_plan([agent_node("calc", f.harness(), outputs={"energy": "number"})], pid="rep"))
    paths = write_report(eng)
    md = open(paths["md"]).read()
    html = open(paths["html"]).read()
    for s in ("## Results", "## Workflow", "## Decisions and approvals", "## Node details", "## Timeline",
              "## Provenance"):
        assert s in md
    assert "1 repair turn(s)" in md
    assert "$0.5000" in md  # both turns billed
    assert "answer did not match contract, repair turn 1" in md
    assert "Results" in h2s(html) and "Provenance" in h2s(html) and "Timeline" in h2s(html)
    assert '<pre class="mermaid">' in html


def test_agent_text_is_escaped_in_html(fake, run_plan):
    evil = "<script>alert('x')</script> & <img src=x onerror=alert(1)>"
    f = fake("script", [{"answer": {"summary": evil, "rationale": evil, "note": evil}}])
    eng, st = run_plan(make_plan([agent_node("calc", f.harness())], pid="rep-xss"))
    html = open(write_report(eng)["html"]).read()
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert" in html


def test_multiline_rationale_cannot_break_report_structure(fake, run_plan):
    rationale = "Chose PBE.\n```\nraw tool dump\n## Provenance\n- forged by the agent"
    f = fake("script", [{"answer": {"summary": "done", "rationale": rationale}}])
    eng, st = run_plan(make_plan([agent_node("calc", f.harness())], pid="rep-md"))
    md = build_markdown(eng)
    html = md_to_html(md)
    assert h2s(html).count("Provenance") == 1 and "Timeline" in h2s(html)
    assert md.count("\n## Provenance") == 1


def test_report_survives_rejected_malformed_agent_amendment(fake, run_plan):
    f = fake("script", [{"answer": {"summary": "s", "amendment": {"rationale": "x", "ops": ["add a node please"]}}}])
    eng, st = run_plan(make_plan([agent_node("scout", f.harness(), effects={"amend": True})], pid="rep-am"))
    assert [a.status for a in st.amendments.values()] == ["rejected"]
    write_report(eng)


def test_audit_is_stable_json(fake, run_plan):
    f = fake("pi", [{"answer": {"n": 3, "summary": "counted", "rationale": "r"}}])
    eng, st = run_plan(make_plan([agent_node("p", f.harness(), outputs={"n": "integer"})], pid="aud"))
    a = audit(eng)
    s = json.dumps(a, default=str)
    a2 = json.loads(s)
    assert a2["schema"] == "flower.audit/1"
    assert set(a2) >= {"run", "plan", "nodes", "gates", "amendments", "notes"}
    (att,) = a2["nodes"]["p"]["attempts"]
    assert att["session"] and att["outputs"] == {"n": 3} and att["rationale"] == "r"
    assert att["usage"]["cost_usd"] == pytest.approx(0.003)
    assert a2["run"]["cost"]["usd"] == pytest.approx(0.003)
