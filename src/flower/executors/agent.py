"""``agent`` nodes: a headless coding-agent harness does the work, flower holds the contract.

Per attempt:

1. Build a prompt = the node's prompt + a *node contract* block (inputs, workdir, upstream results,
   the exact JSON the final answer must contain, `summary` + `rationale` for provenance, and — only if
   the plan grants ``effects.amend`` — how to propose a plan amendment).
2. Launch the harness under the detached runner; record argv (``argv.json``) and session id.
3. When it exits, parse with the harness adapter, extract/validate the structured answer.
   An invalid or missing answer triggers a *repair turn* (resume the same session with the
   validation errors) up to ``repair_attempts`` times — separate from task retries (Smithers).
4. The verdict: outputs, files, usage/cost summed over all turns, summary, rationale, amendment.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from ..harness import AgentRequest, extract_json, get_harness
from ..harness.base import HarnessResult
from ..util import atomic_write_json, first_line, parse_duration, read_json, tail_str, truncate
from .base import (Executor, NodeCtx, Outcome, cancel_process, check_outputs, exit_failure, finish_contract, launch,
                   outputs_schema, poll_process)

EXTRA_KEYS = ("summary", "rationale", "amendment")
NON_RETRYABLE = {"auth", "config", "budget", "max_turns", "contract"}

AMEND_HELP = """\
If (and only if) your findings show that the workflow itself needs to change — e.g. an extra
calculation, a convergence test, an extra analysis — add an "amendment" field to your JSON:
  "amendment": {"rationale": "why the plan must change",
                "ops": [{"op": "add", "nodes": [<node spec>, ...]}]}
Ops: add (new nodes), detour (insert nodes after an existing node: {"op":"detour","after":"<id>","nodes":[...]}),
replace (a not-yet-started node), stop (skip pending nodes: {"op":"stop","nodes":["<id>"]}).
A node spec looks like {"id": "unique-id", "kind": "shell|function|agent|job", "needs": ["<id>"], ...}
using the same fields as the plan (e.g. shell: run; job: cluster, script, resources; agent: prompt, outputs).
{policy}
The amendment is only a proposal: flower validates it and a human (or the plan's policy) approves it.
"""


def _proc(ctx: NodeCtx, k: int) -> Path:
    return ctx.attempt_dir / ("proc" if k == 0 else f"proc-r{k}")


def answer_schema(node: dict) -> dict:
    s = outputs_schema(node.get("outputs") or {})
    s["properties"] = dict(s["properties"])
    s["properties"]["summary"] = {"type": "string", "description": "one sentence: what you did / found"}
    s["properties"]["rationale"] = {"type": "string", "description": "why you made the key choices (provenance)"}
    if (node.get("effects") or {}).get("amend"):
        s["properties"]["amendment"] = {"type": "object", "description": "optional plan amendment proposal"}
    s["required"] = list(dict.fromkeys(s["required"] + ["summary"]))
    return s


def contract_block(ctx: NodeCtx, upstream: dict) -> str:
    node = ctx.node
    lines = ["", "", "---", "## flower node contract",
             f"You are node `{node['id']}` (\"{node.get('title') or node['id']}\") of flower run `{ctx.run_id}`.",
             f"Working directory: {ctx.workdir}",
             "Do not run `flower` commands yourself; the workflow is driven from outside."]
    if ctx.inputs:
        lines.append(f"Inputs (also in {ctx.attempt_dir / 'inputs.json'}):")
        lines.append("```json\n" + truncate(json.dumps(ctx.inputs, indent=2, default=str), 4000) + "\n```")
    if upstream:
        lines.append("Upstream results available to you:")
        for nid, u in upstream.items():
            files = ", ".join(f"{k}={v}" for k, v in (u.get("files") or {}).items())
            lines.append(f"- `{nid}`: {u.get('summary') or '(no summary)'}" + (f" — files: {files}" if files else ""))
    if node.get("files"):
        lines.append("Files you must create (relative to the working directory): "
                     + ", ".join(f"`{v}`" for v in node["files"].values()))
    schema = answer_schema(node)
    lines += ["When you are finished, end your reply with ONE JSON object (no text after it) matching this schema:",
              "```json\n" + json.dumps(schema, indent=2) + "\n```",
              "`summary` is one sentence a human will read in the run report; `rationale` explains *why* "
              "(choices, parameters, judgement calls) and is kept as provenance."]
    amend = (node.get("effects") or {}).get("amend")
    if amend:
        pol = amend if isinstance(amend, dict) else {}
        policy = []
        if pol.get("auto_approve"):
            policy.append("Small amendments within the plan policy are applied automatically"
                          + (f" (max {pol['max_nodes']} nodes" if pol.get("max_nodes") else " (")
                          + (f", kinds {pol['kinds']})" if pol.get("kinds") else ")") + ".")
        lines.append(AMEND_HELP.replace("{policy}", " ".join(policy)))
    return "\n".join(lines)


class AgentExecutor(Executor):
    kind = "agent"

    # ------------------------------------------------------------ request building
    def _request(self, ctx: NodeCtx, prompt: str, proc: Path, resume: str | None = None,
                 session_id: str | None = None) -> AgentRequest:
        h = ctx.node.get("harness") or {}
        tools = h.get("tools") or {}
        harness = get_harness(h.get("name", "claude"))
        native = harness.native_schema and h.get("native_schema", True) and bool(ctx.node.get("outputs"))
        cmd = h.get("command")
        if isinstance(cmd, str):
            cmd = [cmd]
        return AgentRequest(prompt=prompt, cwd=str(ctx.workdir), proc_dir=str(proc), model=h.get("model"),
                            effort=h.get("effort"), system_append=ctx.node.get("system"),
                            output_schema=answer_schema(ctx.node) if native else None,
                            session_id=session_id, resume=resume, tools_allow=list(tools.get("allow") or []),
                            tools_deny=list(tools.get("deny") or []), max_turns=h.get("max_turns"),
                            budget_usd=h.get("budget_usd"), permission=h.get("permission", "bypass"),
                            extra_args=list(h.get("extra_args") or []), command=cmd,
                            env={k: str(v) for k, v in (h.get("env") or {}).items()})

    def _launch(self, ctx: NodeCtx, req: AgentRequest, proc: Path) -> dict:
        harness = get_harness((ctx.node.get("harness") or {}).get("name", "claude"))
        inv = harness.build(req)
        proc.mkdir(parents=True, exist_ok=True)
        atomic_write_json(proc / "argv.json", {"argv": inv.argv, "env_added": sorted(inv.env), "unset": inv.unset_env,
                                               "harness": harness.name, "cwd": req.cwd})
        t = ctx.node.get("timeout") or {}
        env = ctx.base_env()
        env.update(inv.env)
        return launch(proc, inv.argv, env=env, cwd=ctx.workdir, stdin_text=inv.stdin_text,
                      timeout_total=parse_duration(t.get("total")), timeout_idle=parse_duration(t.get("idle")),
                      unset_env=inv.unset_env)

    def _upstream(self, ctx: NodeCtx) -> dict:
        info = read_json(ctx.attempt_dir / "upstream.json")
        return info or {}

    # ------------------------------------------------------------ protocol
    def start(self, ctx: NodeCtx) -> dict:
        atomic_write_json(ctx.attempt_dir / "inputs.json", ctx.inputs)
        prompt = str(ctx.node["prompt"]) + contract_block(ctx, self._upstream(ctx))
        (ctx.attempt_dir / "prompt.md").write_text(prompt)
        harness = get_harness((ctx.node.get("harness") or {}).get("name", "claude"))
        sid = str(uuid.uuid4()) if harness.name in ("claude", "pi") else None
        req = self._request(ctx, prompt, _proc(ctx, 0), session_id=sid)
        handle = self._launch(ctx, req, _proc(ctx, 0))
        if sid:
            ctx.emit("agent.session", {"session_id": sid, "harness": harness.name})
        handle["harness"] = harness.name
        return handle

    def poll(self, ctx: NodeCtx) -> Outcome | None:
        harness = get_harness((ctx.node.get("harness") or {}).get("name", "claude"))
        k = ctx.repairs
        proc = _proc(ctx, k)
        state, info = poll_process(proc)
        if state == "running":
            live = harness.live(proc)
            live["turn"] = k
            atomic_write_json(ctx.attempt_dir / "live.json", live)
            if live.get("session") and not ctx.session:
                ctx.emit("agent.session", {"session_id": live["session"], "harness": harness.name})
                ctx.session = live["session"]
            return None
        if state == "lost":
            return Outcome.fail("lost", f"agent process lost: {info.get('why')}", usage=self._usage(ctx, k - 1))
        res = read_json(proc / "harness_result.json")
        if res is None:
            hr = harness.parse(proc, info)
            res = hr.__dict__
            atomic_write_json(proc / "harness_result.json", res)
        hr = HarnessResult(**res)
        if hr.session_id and hr.session_id != ctx.session:
            ctx.emit("agent.session", {"session_id": hr.session_id, "harness": harness.name})
            ctx.session = hr.session_id
        usage = self._usage(ctx, k)
        hard = exit_failure(info, proc, f"{harness.name} agent")
        if hard and hard.error_class in ("cancelled", "timeout", "idle_timeout", "killed", "spawn"):
            hard.usage = usage
            return hard
        if not hr.ok:
            cls = hr.error_class or "agent_error"
            oc = Outcome.fail("quota_retry" if cls == "quota" else cls,
                              hr.message or f"{harness.name} failed", usage=usage,
                              retryable=False if cls in NON_RETRYABLE else None,
                              details={"stop_reason": hr.stop_reason, "retry_after_s": hr.retry_after_s,
                                       "text_tail": tail_str(hr.text, 600)})
            return oc
        answer = hr.structured if isinstance(hr.structured, dict) else extract_json(hr.text)
        declared = ctx.node.get("outputs") or {}
        extras = [k2 for k2 in EXTRA_KEYS if k2 not in declared]
        problems = []
        if not isinstance(answer, dict):
            problems = ["the reply did not end with a JSON object"]
        else:
            core = {k2: v for k2, v in answer.items() if k2 not in extras}
            problems = check_outputs(core, declared)
        if problems:
            if k < int(ctx.node.get("repair_attempts", 2)):
                return self._repair(ctx, harness, hr, problems, k + 1)
            return Outcome.fail("schema_invalid", "agent answer never matched the output contract: "
                                + "; ".join(problems[:4]), usage=usage, retryable=False,
                                details={"text_tail": tail_str(hr.text, 1200)})
        outputs = {k2: v for k2, v in answer.items() if k2 not in extras}
        summary = answer.get("summary") if isinstance(answer.get("summary"), str) else first_line(hr.text, 200)
        rationale = answer.get("rationale") if isinstance(answer.get("rationale"), str) else None
        amendment = answer.get("amendment") if isinstance(answer.get("amendment"), dict) else None
        oc = Outcome(status="succeeded", outputs=outputs, usage=usage, summary=first_line(summary, 240),
                     rationale=rationale, amendment=amendment)
        atomic_write_json(ctx.attempt_dir / "answer.json", answer)
        return finish_contract(oc, ctx.node, ctx.workdir)

    def _repair(self, ctx: NodeCtx, harness, hr: HarnessResult, problems: list[str], k: int) -> Outcome | None:
        proc = _proc(ctx, k)
        msg = ("Your final answer did not satisfy the flower output contract:\n- " + "\n- ".join(problems[:8])
               + "\n\nReply now with ONLY the corrected JSON object matching this schema (no other text):\n"
               + json.dumps(answer_schema(ctx.node), indent=2))
        if harness.can_resume and (hr.session_id or ctx.session):
            req = self._request(ctx, msg, proc, resume=hr.session_id or ctx.session)
        else:
            original = (ctx.attempt_dir / "prompt.md").read_text()
            prev = hr.text if len(hr.text or "") <= 3000 else (hr.text[:1000] + "\n…\n" + hr.text[-2000:])
            prompt = (original + "\n\n---\nYour previous answer (head and tail):\n" + prev + "\n\n" + msg)
            req = self._request(ctx, prompt, proc)
        try:
            self._launch(ctx, req, proc)
        except Exception as exc:  # noqa: BLE001
            return Outcome.fail("spawn", f"could not launch repair turn: {exc}", usage=self._usage(ctx, k - 1))
        ctx.emit("agent.repair", {"n": k, "reason": "; ".join(problems[:4])})
        ctx.repairs = k
        return None

    def _usage(self, ctx: NodeCtx, upto: int) -> dict:
        total: dict = {"input_tokens": 0, "output_tokens": 0, "cost_usd": None, "turns": 0}
        cost, src = 0.0, None
        for k in range(0, max(upto, 0) + 1):
            r = read_json(_proc(ctx, k) / "harness_result.json")
            if not r:
                continue
            u = r.get("usage") or {}
            total["input_tokens"] += int(u.get("input_tokens") or 0)
            total["output_tokens"] += int(u.get("output_tokens") or 0)
            total["turns"] += int(u.get("turns") or 0)
            if u.get("cost_usd") is not None:
                cost += float(u["cost_usd"])
                src = u.get("cost_source") or "harness"
        if src:
            total["cost_usd"] = round(cost, 6)
            total["cost_source"] = src
        total["model"] = (ctx.node.get("harness") or {}).get("model")
        total["harness"] = (ctx.node.get("harness") or {}).get("name")
        return total

    def cancel(self, ctx: NodeCtx) -> None:
        cancel_process(_proc(ctx, ctx.repairs))
