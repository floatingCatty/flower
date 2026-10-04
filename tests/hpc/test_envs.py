"""Environment recipes: an agent explores (flower remote exec), flower freezes and replays the recipe, and
nodes with `environment:` get a generated step that checks it (else installs it) and activate it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flower import envs as envmod
from flower.plan import check as plan_check, normalize, validate

HELLO_SETUP = ('mkdir -p "$FLOWER_ENV_PREFIX/bin"\n'
               'printf \'#!/bin/sh\\necho "hello v1"\\n\' > "$FLOWER_ENV_PREFIX/bin/hello-tool"\n'
               'chmod +x "$FLOWER_ENV_PREFIX/bin/hello-tool"\n'
               'echo installed >> "$HOME/setup-runs.txt"\n')


def _recipe(root: Path, name="hello", setup=HELLO_SETUP, check='hello-tool | grep -q "hello v1" && echo "hello v1 OK"\n',
            freeze=True) -> Path:
    d = root / "envs" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "setup.sh").write_text(setup)
    (d / "activate.sh").write_text('export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"\n')
    (d / "check.sh").write_text(check)
    if freeze:
        envmod.freeze(d, by="test")
    return d


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    h = tmp_path / "userhome"  # where $HOME/.flower/envs/... lands on the (local) "remote"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _cluster(ff, **kw):
    c = {"transport": "local", "scheduler": "none", "remote_root": str(ff.tmp / "remote"), "min_poll": "0.2s",
         "lost_after": "1s"}
    c.update(kw)
    return {"box": c}


def _plan(ff, src: Path, clusters=None, extra=None):
    nodes = [{"id": "use", "kind": "shell", "cluster": "box", "environment": "hello",
              "run": 'echo "{\\"said\\": \\"$(hello-tool)\\"}" > "$FLOWER_OUTPUTS"', "outputs": {"said": "string"}}]
    return ff.plan(nodes + (extra or []), clusters=clusters or _cluster(ff), _source={"dir": str(src)})


# ------------------------------------------------------------------ freezing

def test_freeze_hashes_every_recipe_file_and_detects_edits(tmp_path):
    d = _recipe(tmp_path, freeze=False)
    assert envmod.status(d)[0] == "draft"
    fz = envmod.freeze(d, by="t")
    assert envmod.status(d)[0] == "frozen" and set(fz["files"]) >= {"setup.sh", "activate.sh", "check.sh"}
    (d / "lock.txt").write_text("pinned\n")  # a file the agent added counts
    assert envmod.status(d)[0] == "changed"
    fz2 = envmod.freeze(d, by="t")
    assert fz2["hash"] != fz["hash"] and fz2["previous"] == fz["hash"] and "lock.txt" in fz2["files"]
    assert (d / "history" / envmod.short(fz["hash"]) / "setup.sh").exists()
    assert (d / "history" / envmod.short(fz2["hash"]) / "lock.txt").exists()
    assert envmod.freeze(d, by="t").get("unchanged") is True


def test_freeze_drafts_setup_from_logged_successful_commands(tmp_path):
    d = tmp_path / "envs" / "x"
    envmod.new(d)
    for cmd, rc, probe in (("which thing", 1, True), ("make thing", 0, False), ("broken", 2, False),
                           ("thing --version", 0, True), ("install thing", 0, False)):
        envmod.log_session(d, {"at": "t", "cluster": "c", "cmd": cmd, "rc": rc, "probe": probe})
    (d / "activate.sh").write_text("export A=1\n")
    (d / "check.sh").write_text("true\n")
    fz = envmod.freeze(d, by="t")
    text = (d / "setup.sh").read_text()
    assert fz["drafted_setup"] and text.index("make thing") < text.index("install thing")
    assert "broken" not in text and "--version" not in text and "which thing" not in text


def test_freeze_refuses_template_scripts(tmp_path):
    d = tmp_path / "envs" / "x"
    envmod.new(d)
    with pytest.raises(Exception) as e:
        envmod.freeze(d, by="t")
    assert getattr(e.value, "code", "") == "env_incomplete"


# ------------------------------------------------------------------ plans

def test_plan_gets_one_env_step_per_env_and_cluster(ff, tmp_path):
    src = tmp_path / "proj"
    d = _recipe(src)
    plan = _plan(ff, src, extra=[{"id": "use2", "kind": "shell", "cluster": "box", "environment": "hello",
                                  "run": "hello-tool"}])
    p = plan_check(plan)
    ids = [n["id"] for n in p["nodes"]]
    assert ids.count("env-hello-box") == 1 and ids[0] == "env-hello-box"
    use = next(n for n in p["nodes"] if n["id"] == "use")
    assert "env-hello-box" in use["needs"]
    h = envmod.status(d)[1]["hash"]
    assert use["prelude"][0] == f'export FLOWER_ENV_PREFIX="$HOME/.flower/envs/hello-{envmod.short(h)}" ' \
                                f'FLOWER_ENV_DIR="$HOME/.flower/envs/hello-{envmod.short(h)}.recipe"'
    again = normalize(p)  # stored plans are normalised again: nothing doubles
    assert [n["id"] for n in again["nodes"]] == ids
    assert next(n for n in again["nodes"] if n["id"] == "use")["prelude"] == use["prelude"]


@pytest.mark.parametrize("case", ["missing", "draft", "changed", "no_cluster"])
def test_environment_validation(ff, tmp_path, case):
    src = tmp_path / "proj"
    if case != "missing":
        d = _recipe(src, freeze=case != "draft")
        if case == "changed":
            (d / "check.sh").write_text("false\n")
    plan = _plan(ff, src)
    if case == "no_cluster":
        plan["nodes"][0].pop("cluster")
    norm = normalize(plan)
    msgs = [i["message"] for i in validate(norm) if i["code"] == "environment"]
    if case == "no_cluster":   # a shell/function step without a cluster uses the environment on this machine
        if norm["nodes"][-1].get("kind") in ("shell", "function"):
            assert msgs == [] and norm["clusters"]["local"]["transport"] == "local", msgs
        return
    want = {"missing": "no recipe envs/hello/", "draft": "is not frozen", "changed": "edited after it was frozen"}[case]
    assert any(want in m for m in msgs), msgs


# ------------------------------------------------------------------ runs

def test_env_step_installs_once_then_finds_it_present(ff, tmp_path, home_dir):
    src = tmp_path / "proj"
    _recipe(src)
    eng = ff.run(_plan(ff, src))
    st = ff.drive(eng)
    assert st.status == "succeeded", ff.why(eng)
    assert st.nodes["env-hello-box"].result.outputs["how"] == "installed"
    assert st.nodes["use"].result.outputs["said"] == "hello v1"
    eng2 = ff.run(_plan(ff, src))
    st2 = ff.drive(eng2)
    assert st2.status == "succeeded", ff.why(eng2)
    assert st2.nodes["env-hello-box"].result.outputs["how"] == "present"
    assert (home_dir / "setup-runs.txt").read_text().count("installed") == 1  # setup.sh ran once


def test_install_never_only_checks(ff, tmp_path, home_dir):
    src = tmp_path / "proj"
    _recipe(src)
    eng = ff.run(_plan(ff, src, clusters=_cluster(ff, install="never")))
    st = ff.drive(eng)
    assert st.status == "failed"
    err = st.nodes["env-hello-box"].last.error
    assert "install: never" in err["message"] or "not allowed" in err["message"], err
    assert st.nodes["use"].status == "skipped"
    assert not (home_dir / "setup-runs.txt").exists()


def test_failing_setup_is_reported_with_its_log(ff, tmp_path, home_dir):
    src = tmp_path / "proj"
    _recipe(src, setup='echo "compiler not found: gfortran" >&2\nexit 7\n')
    eng = ff.run(_plan(ff, src))
    st = ff.drive(eng)
    err = st.nodes["env-hello-box"].last.error
    assert st.status == "failed" and "gfortran" in err["message"], err


def test_changed_recipe_installs_into_a_new_prefix(ff, tmp_path, home_dir):
    src = tmp_path / "proj"
    d = _recipe(src)
    st = ff.drive(ff.run(_plan(ff, src)))
    p1 = st.nodes["env-hello-box"].result.outputs["prefix"]
    (d / "setup.sh").write_text(HELLO_SETUP + "# v1 again, edited\n")
    envmod.freeze(d, by="t")
    st2 = ff.drive(ff.run(_plan(ff, src)))
    p2 = st2.nodes["env-hello-box"].result.outputs["prefix"]
    assert p1 != p2 and Path(p1).is_dir() and Path(p2).is_dir()  # the old environment is never mutated
    assert st2.nodes["env-hello-box"].result.outputs["how"] == "installed"


# ------------------------------------------------------------------ CLI: explore, freeze, replay

def _cli(args, cwd, env):
    return subprocess.run([sys.executable, "-m", "flower", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=120)


def test_cli_explore_freeze_replay(tmp_path, home_dir):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "plan.yaml").write_text(json.dumps({
        "flower": 1, "id": "p", "clusters": {"box": {"transport": "local", "scheduler": "none",
                                                     "remote_root": str(tmp_path / "remote")}},
        "nodes": [{"id": "x", "kind": "shell", "run": "true"}]}))
    env = {**os.environ, "HOME": str(home_dir), "FLOWER_HOME": str(proj)}
    env.pop("BASH_ENV", None)
    assert _cli(["env", "new", "hello"], proj, env).returncode == 0
    rx = ["remote", "exec", "--env", "hello", "--plan", "plan.yaml", "--cluster", "box", "--"]
    probe = ["remote", "exec", "--probe", *rx[2:]]
    r = _cli([*probe, 'echo "prefix=$FLOWER_ENV_PREFIX"'], proj, env)
    assert r.returncode == 0 and f"prefix={home_dir}/.flower/envs/hello-explore" in r.stdout
    r = _cli([*rx, HELLO_SETUP.replace("\n", "; ").rstrip("; ")], proj, env)
    assert r.returncode == 0, r.stderr
    d = proj / "envs" / "hello"
    (d / "activate.sh").write_text('export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"\n')
    (d / "check.sh").write_text('hello-tool | grep -q "hello v1"\n')
    r = _cli([*probe, "hello-tool"], proj, env)  # activate.sh is sourced while exploring
    assert r.stdout.strip() == "hello v1", r.stderr
    r = _cli([*probe, 'bash "$FLOWER_ENV_DIR/check.sh" && echo checked'], proj, env)  # the recipe as written so far
    assert r.returncode == 0 and "checked" in r.stdout, r.stdout + r.stderr
    r = _cli(["env", "freeze", "hello"], proj, env)
    assert r.returncode == 0 and "drafted" in r.stdout, r.stdout + r.stderr
    assert "hello-tool" in (d / "setup.sh").read_text() and "echo \"prefix" not in (d / "setup.sh").read_text()
    for _ in range(2):  # from scratch, twice: two fresh prefixes, both pass the check
        r = _cli(["env", "replay", "hello", "--plan", "plan.yaml", "--cluster", "box", "--fresh"], proj, env)
        assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
    fz = json.loads((d / "FROZEN.json").read_text())
    assert [x["ok"] for x in fz["replays"]] == [True, True] and fz["replays"][0]["prefix"] != fz["replays"][1]["prefix"]
    r = _cli(["env", "check", "hello", "--plan", "plan.yaml", "--cluster", "box"], proj, env)
    assert r.returncode != 0  # nothing installed at the recipe's own prefix yet: check only, no setup
    r = _cli(["env", "replay", "hello", "--plan", "plan.yaml", "--cluster", "box"], proj, env)
    assert r.returncode == 0 and "installed" in r.stdout
    assert _cli(["env", "check", "hello", "--plan", "plan.yaml", "--cluster", "box"], proj, env).returncode == 0


def test_a_hanging_check_fails_fast_and_says_so(tmp_path):
    """BUGS #27: check.sh ran unbounded inside the env step's whole budget; a program that hangs on a new host
    (QE's ld1.x with 96 OpenMP threads) blocked the step for its full timeout with an empty log."""
    from flower import envs as envmod
    d = tmp_path / "recipe"
    d.mkdir()
    (d / "activate.sh").write_text("true\n")
    (d / "check.sh").write_text("sleep 30\n")
    (d / "setup.sh").write_text("mkdir -p \"$FLOWER_ENV_PREFIX\"\n")
    script = envmod.setup_script("slow", "sha256:" + "0" * 64, recipe_dir=str(d), allow_install=False,
                                 env_prefix=str(tmp_path / "pfx"), check_timeout=2)
    import subprocess, time
    t0 = time.time()
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and time.time() - t0 < 20
    assert "did not finish within 2s" in (tmp_path / "check.log").read_text()


def test_a_local_step_can_name_an_environment(ff, tmp_path, home_dir):
    """A shell step with `environment:` and no cluster runs on this machine with the recipe (implicit cluster
    `local`): reproducible local analysis without a hard-coded interpreter path."""
    src = tmp_path / "proj"
    _recipe(src)
    plan = {"flower": 1, "id": "loc", "nodes": [
        {"id": "a", "kind": "shell", "environment": "hello", "run": 'echo "{\\"v\\": \\"$(hello-tool)\\"}" > "$FLOWER_OUTPUTS"',
         "outputs": {"v": "string"}}]}
    p = src / "plan.yaml"
    p.write_text(json.dumps(plan))
    from flower.plan import check, load_plan_file
    norm = check(load_plan_file(str(p)))
    assert norm["clusters"]["local"] == {"transport": "local", "scheduler": "none"}
    assert any(n["id"] == "env-hello-local" for n in norm["nodes"])
    eng = ff.run(load_plan_file(str(p)))
    st = ff.drive(eng, timeout=60)
    assert st.status == "succeeded", ff.why(eng)
    assert st.nodes["a"].result.outputs["v"] == "hello v1"


def test_an_amendment_adds_a_local_step_with_an_environment(ff, tmp_path, home_dir):
    """The implicit `local` cluster also appears when such a step arrives by amendment (a running draft growing)."""
    src = tmp_path / "proj"
    _recipe(src)
    p = src / "plan.yaml"
    p.write_text(json.dumps({"flower": 1, "id": "am", "nodes": [{"id": "a", "kind": "shell", "run": "true"}]}))
    from flower.plan import load_plan_file, normalize_node
    eng = ff.run(load_plan_file(str(p)))
    ff.drive(eng, timeout=30)
    step = {"id": "b", "kind": "shell", "environment": "hello", "cluster": "local",   # as plan_edits delivers it
            "run": 'echo "{\\"v\\": \\"$(hello-tool)\\"}" > "$FLOWER_OUTPUTS"', "outputs": {"v": "string"}}
    eng.propose_amendment([{"op": "add", "nodes": [step]}], "add a local analysis step", by="test:x", auto_approve=True)
    st = ff.drive(eng, timeout=60)
    assert st.plan["clusters"]["local"]["transport"] == "local"
    assert st.nodes["b"].result.outputs["v"] == "hello v1", ff.why(eng)


def test_a_changed_env_script_is_not_a_plan_edit(ff, tmp_path, home_dir, monkeypatch):
    """BUGS #47: the generated environment step's script is flower's own; when flower changes it, the plan is not
    edited (a new recipe version still is)."""
    src = tmp_path / "proj"
    _recipe(src)
    plan = {"flower": 1, "id": "loc", "nodes": [
        {"id": "a", "kind": "shell", "environment": "hello", "run": 'echo "{\\"v\\": 1}" > "$FLOWER_OUTPUTS"',
         "outputs": {"v": "integer"}}]}
    p = src / "plan.yaml"
    p.write_text(json.dumps(plan))
    from flower.plan import load_plan_file
    eng = ff.run(load_plan_file(str(p)))
    assert ff.drive(eng, timeout=60).status == "succeeded", ff.why(eng)
    real = envmod.setup_script
    monkeypatch.setattr(envmod, "setup_script", lambda *a, **k: real(*a, **k) + "\n# a newer flower\n")
    assert eng.plan_edits(None)["ops"] == []
    _recipe(src, check='hello-tool | grep -q "hello v1" && echo "hello v1 OK (v2)"\n')   # a new recipe version
    assert "env-hello-local" in eng.plan_edits(None)["changed"]


def test_concurrent_installs_of_one_environment_wait_for_each_other(tmp_path):
    """BUGS #40: three runs found the same recipe missing at once and ran `conda create` into one prefix together."""
    import subprocess
    d = tmp_path / "recipe"
    d.mkdir()
    (d / "activate.sh").write_text('export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"\n')
    (d / "check.sh").write_text('[ -x "$FLOWER_ENV_PREFIX/bin/tool" ]\n')
    # a slow, non-reentrant install: fails if another install is in progress in the same prefix
    (d / "setup.sh").write_text('mkdir "$FLOWER_ENV_PREFIX" || exit 9\nsleep 2\nmkdir -p "$FLOWER_ENV_PREFIX/bin"\n'
                                'printf "#!/bin/sh\\necho ok\\n" > "$FLOWER_ENV_PREFIX/bin/tool"; chmod +x "$FLOWER_ENV_PREFIX/bin/tool"\n')
    script = envmod.setup_script("x", "sha256:" + "1" * 64, recipe_dir=str(d), allow_install=True,
                                 env_prefix=str(tmp_path / "pfx"))
    procs = []
    for i in range(3):
        w = tmp_path / f"w{i}"
        w.mkdir()
        procs.append(subprocess.Popen(["bash", "-c", script], cwd=w, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    hows = sorted("installed" if "x installed" in o[0] else "present" for o in outs)
    assert hows == ["installed", "present", "present"], outs
