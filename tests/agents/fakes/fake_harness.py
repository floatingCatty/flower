#!/usr/bin/env python3
"""Fake coding-agent CLIs for flower tests (no LLM is ever called).

Usage (as a flower harness ``command``)::

    harness: {name: claude, command: [python, fake_harness.py, claude], env: {FAKE_SCENARIO: …, FAKE_LOG: …}}

flower appends its own flags after ``command[1:]``, so ``sys.argv[2:]`` is exactly the argv flower
built.  The behaviour of the N-th invocation is ``scenario["turns"][N]`` (the last entry repeats).  Every
invocation appends one JSON record (argv, stdin, cwd, interesting env vars) to ``$FAKE_LOG`` so tests can
assert on what flower passed.

Turn keys (all optional):
  answer      dict  -> the final JSON answer (rendered per ``format``: bare | fenced | only)
  prose       str   -> text before the answer (default "Done.")
  text        str   -> the exact final text (overrides answer/prose)
  exit        int   -> exit code (default 0)
  stderr      str   -> written to stderr
  hang        float -> after the preamble, sleep this long without any output
  noise_kb    int   -> emit ~N KiB of harmless stream events before the result
  files       {rel: content} -> created in cwd
  claude: is_error, subtype, structured ("answer" mirrors answer), cost, no_result, session (override id)
  codex : fail (turn.failed message), error_event (top-level error message), no_last_file, reconnect
  pi    : stop (stopReason, default "stop"), error_message, cost
"""
import json
import os
import sys
import time
import uuid

WATCH_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "FLOWER_INSIDE_RUN", "FLOWER_RUN_ID",
             "FLOWER_NODE_ID", "FLOWER_ATTEMPT", "FLOWER_OUTPUT_SCHEMA", "FLOWER_MODEL", "FLOWER_INPUTS", "FLOWER_NODE_DIR", "FAKE_EXTRA",
             "FLOWER_IN_TOPIC")


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def flag(argv, name):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def final_text(t):
    if "text" in t:
        return t["text"]
    ans = t.get("answer")
    prose = t.get("prose", "Done.")
    if ans is None:
        return prose
    body = json.dumps(ans)
    fmt = t.get("format", "bare")
    if fmt == "only":
        return body
    if fmt == "fenced":
        return f"{prose}\n\n```json\n{json.dumps(ans, indent=2)}\n```"
    return f"{prose}\n{body}"


def noise(t, make):
    kb = int(t.get("noise_kb") or 0)
    chunk = "x" * 1000
    for i in range(kb):
        emit(make(i, chunk))


def run_claude(argv, t):
    sid = t.get("session") or flag(argv, "--resume") or flag(argv, "--session-id") or str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-fake-1", "cwd": os.getcwd(),
          "tools": ["Bash", "Read", "Write"], "permissionMode": flag(argv, "--permission-mode")})
    emit({"type": "assistant", "session_id": sid, "message": {
        "id": "msg_1", "role": "assistant", "model": "claude-fake-1",
        "content": [{"type": "text", "text": "Let me look at the working directory."},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls -la"}}]}})
    emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "total 0\nfile.txt", "is_error": False}]}})
    if t.get("hang"):
        time.sleep(float(t["hang"]))
    noise(t, lambda i, c: {"type": "assistant", "session_id": sid, "message": {
        "role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_n{i}", "name": "Read",
                                          "input": {"file_path": f"/tmp/{i}", "pad": c}}]}})
    text = final_text(t)
    emit({"type": "assistant", "session_id": sid, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    if not t.get("no_result"):
        structured = t.get("structured")
        if structured == "answer":
            structured = t.get("answer")
        ev = {"type": "result", "subtype": t.get("subtype", "success"), "is_error": bool(t.get("is_error", False)),
              "duration_ms": 1234, "duration_api_ms": 1000, "num_turns": t.get("num_turns", 3), "result": text,
              "session_id": sid, "total_cost_usd": t.get("cost", 0.25),
              "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100,
                        "output_tokens": 50}}
        if structured is not None:
            ev["structured_output"] = structured
        emit(ev)


def run_codex(argv, t):
    if argv[:2] == ["exec", "resume"]:
        thread = argv[2]
    else:
        thread = t.get("thread") or ("thread-" + uuid.uuid4().hex[:12])
    last = flag(argv, "-o")
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    if t.get("reconnect"):
        emit({"type": "error", "message": "stream disconnected; reconnecting... 1/5"})
    emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "bash -lc ls",
                                           "aggregated_output": "", "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "bash -lc ls",
                                             "aggregated_output": "a\nb\n", "exit_code": 0, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "thinking about it"}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "status": "completed",
                                             "changes": [{"path": "notes.txt", "kind": "add"}]}})
    if t.get("hang"):
        time.sleep(float(t["hang"]))
    noise(t, lambda i, c: {"type": "item.completed", "item": {"id": f"n{i}", "type": "reasoning", "text": c}})
    if t.get("error_event"):
        emit({"type": "error", "message": t["error_event"]})
    if t.get("fail"):
        emit({"type": "turn.failed", "error": {"message": t["fail"]}})
        return
    text = final_text(t)
    emit({"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30}})
    if last and not t.get("no_last_file"):
        with open(last, "w") as fh:
            fh.write(text)


def run_pi(argv, t):
    sid = flag(argv, "--session") or flag(argv, "--session-id") or str(uuid.uuid4())
    emit({"type": "session", "version": 3, "id": sid, "timestamp": "2026-01-01T00:00:00Z", "cwd": os.getcwd()})
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    emit({"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "the prompt"}]}})
    emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "ls"}})
    emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash", "result": {"content": "a"},
          "isError": False})
    if t.get("hang"):
        time.sleep(float(t["hang"]))
    noise(t, lambda i, c: {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": c}})
    text = final_text(t)
    stop = t.get("stop", "stop")
    msg = {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": text}],
           "provider": "anthropic", "model": "fake-model",
           "usage": {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 120,
                     "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0, "cacheWrite": 0,
                              "total": t.get("cost", 0.003)}},
           "stopReason": stop}
    if t.get("error_message"):
        msg["errorMessage"] = t["error_message"]
    emit({"type": "message_end", "message": msg})
    emit({"type": "turn_end", "message": msg, "toolResults": []})
    emit({"type": "agent_end", "messages": [msg], "willRetry": False})


def run_script(argv, t):
    if t.get("action"):
        print(json.dumps({"flower_action": t["action"]}), flush=True)
    if t.get("hang"):
        time.sleep(float(t["hang"]))
    kb = int(t.get("noise_kb") or 0)
    for i in range(kb):
        print("log line " + "y" * 990, flush=False)
    print(final_text(t), flush=True)


def main():
    flavor = sys.argv[1]
    argv = sys.argv[2:]
    scen_path = os.environ["FAKE_SCENARIO"]
    log = os.environ["FAKE_LOG"]
    with open(scen_path) as fh:
        scen = json.load(fh)
    stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        with open(log) as fh:
            n = sum(1 for _ in fh)
    except FileNotFoundError:
        n = 0
    turns = scen["turns"]
    t = turns[min(n, len(turns) - 1)]
    rec = {"n": n, "flavor": flavor, "argv": argv, "stdin": stdin, "cwd": os.getcwd(),
           "env": {k: os.environ.get(k) for k in WATCH_ENV}}
    with open(log, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    for rel, content in (t.get("files") or {}).items():
        os.makedirs(os.path.dirname(os.path.abspath(rel)), exist_ok=True)
        with open(rel, "w") as fh:
            fh.write(content)
    {"claude": run_claude, "codex": run_codex, "pi": run_pi, "script": run_script}[flavor](argv, t)
    if t.get("stderr"):
        sys.stderr.write(t["stderr"] + "\n")
        sys.stderr.flush()
    sys.exit(int(t.get("exit", 0)))


if __name__ == "__main__":
    main()
