"""flower ui: auth, read API, actions through the engine, file-serving guard."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from flower.engine import create_run
from flower.ui import serve

from core_helpers import drive


@pytest.fixture
def ui_server(home):
    plan = {"flower": 1, "id": "ui", "title": "UI test",
            "nodes": [{"id": "a", "kind": "shell", "run": 'echo hi > out.txt; echo "{\\"v\\": 1}" > "$FLOWER_OUTPUTS"',
                       "files": {"out": "out.txt"}, "outputs": {"v": "integer"}},
                      {"id": "g", "kind": "gate", "needs": ["a"], "message": "v is ${a.outputs.v}. ok?"},
                      {"id": "w", "kind": "wait", "signal": "go"}]}
    eng = create_run(plan, {}, root=home, approve=True)
    drive(eng, timeout=20)
    box = {}
    ready = threading.Event()

    def on_ready(srv, ui):
        box["srv"], box["port"] = srv, srv.server_address[1]
        ready.set()

    th = threading.Thread(target=serve, kwargs={"root": home, "port": 0, "actor": "human:ui-tester",
                                                "token": "tok", "ready": on_ready}, daemon=True)
    th.start()
    assert ready.wait(5)
    yield eng, f"http://127.0.0.1:{box['port']}"
    box["srv"].shutdown()


def get(url, token="tok"):
    req = urllib.request.Request(url, headers={"X-Flower-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


def post(url, body, token="tok"):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-Flower-Token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def test_token_required_for_page_and_api(ui_server):
    eng, base = ui_server
    for path in ("/", "/api/runs"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + path, token=None)
        assert e.value.code == 403
    status, body = get(base + "/?token=tok", token=None)
    assert status == 200 and b"flower" in body and b"tok" in body
    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{base}/api/runs/{eng.paths.run_id}/note", {"text": "x"}, token="wrong")
    assert e.value.code == 403


def test_run_view_lists_nodes_gates_and_waits(ui_server):
    eng, base = ui_server
    runs = json.loads(get(base + "/api/runs")[1])["runs"]
    assert runs[0]["id"] == eng.paths.run_id and runs[0]["decisions"] == 1
    run = json.loads(get(f"{base}/api/runs/{eng.paths.run_id}")[1])["run"]
    assert [n["id"] for n in run["nodes"]][:1] == ["a"] and {n["id"] for n in run["nodes"]} == {"a", "g", "w"}
    assert run["gates"][0]["id"] == "g#a1" and "v is 1" in run["gates"][0]["message"]
    assert run["waits"] == [{"node": "w", "signal": "go", "deadline_at": None}]
    node = json.loads(get(f"{base}/api/runs/{eng.paths.run_id}/node/a")[1])["node"]
    assert node["attempts"][0]["outputs"] == {"v": 1} and node["attempts"][0]["files"][0]["name"] == "out"
    tl = json.loads(get(f"{base}/api/runs/{eng.paths.run_id}/timeline")[1])["events"]
    assert any("plan APPROVED" in e["text"] for e in tl)
    hist = json.loads(get(f"{base}/api/runs/{eng.paths.run_id}/history")[1])
    assert hist["generations"][0]["generation"] == 0


def test_actions_go_through_the_engine_and_are_attributed(ui_server):
    eng, base = ui_server
    rid = eng.paths.run_id
    post(f"{base}/api/runs/{rid}/signal", {"name": "go", "data": '{"x": 2}'})
    r = post(f"{base}/api/runs/{rid}/answer", {"gate": "g#a1", "decision": "approve", "text": "fine"})
    assert r["ok"]
    st = drive(eng, timeout=20)
    assert st.status == "succeeded"
    s = eng.state()
    assert s.gates["g#a1"].by == "human:ui-tester" and s.gates["g#a1"].text == "fine"
    assert s.nodes["w"].result.outputs["data"] == {"x": 2}
    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{base}/api/runs/{rid}/answer", {"gate": "g#a1", "decision": "approve"})
    assert e.value.code == 400  # already answered -> clean error, not a crash


def test_file_endpoint_only_serves_run_files(ui_server, tmp_path):
    eng, base = ui_server
    rid = eng.paths.run_id
    out = eng.state().nodes["a"].result.files["out"]["path"]
    status, body = get(f"{base}/api/runs/{rid}/file?path={urllib.request.quote(out)}")
    assert status == 200 and body.strip() == b"hi"
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    for bad in (str(secret), "../../../../etc/passwd", str(eng.paths.dir / ".." / ".." / "x")):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(f"{base}/api/runs/{rid}/file?path={urllib.request.quote(bad)}")
        assert e.value.code == 403
