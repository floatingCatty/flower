"""Cross-run reuse (`flower run --reuse`), and file permissions."""
from __future__ import annotations

import os
import stat

from flower.engine import Engine, create_run
from flower.rundir import RunPaths

from core_helpers import drive


def test_reuse_only_when_definition_and_inputs_match(home, tmp_path):
    plan = {"flower": 1, "id": "r2", "inputs": {"n": {"type": "integer", "default": 1}},
            "nodes": [{"id": "a", "kind": "shell", "run": 'echo "{\\"v\\": ${inputs.n}}" > "$FLOWER_OUTPUTS"'},
                      {"id": "b", "kind": "shell", "run": "echo b"}]}
    e1 = create_run(plan, {"n": 1}, root=home, approve=True)
    assert drive(e1, timeout=20).status == "succeeded"
    e2 = create_run(plan, {"n": 2}, root=home, approve=True, reuse_from=[e1.state().run_id])
    assert drive(e2, timeout=20).status == "succeeded"
    st = e2.state()
    assert st.nodes["a"].result.reused_from is None and st.nodes["a"].result.outputs == {"v": 2}
    assert st.nodes["b"].result.reused_from.endswith(":b#a1")


def test_generated_files_respect_umask(home, tmp_path):
    old = os.umask(0o022)
    try:
        eng = create_run({"flower": 1, "id": "perm", "nodes": [{"id": "a", "kind": "shell", "run": "true"}]},
                         {}, root=home, approve=True)
        drive(eng, timeout=20)
        for p in (eng.paths.plan_file, eng.paths.events):
            mode = stat.S_IMODE(os.stat(p).st_mode)
            assert mode & 0o044 == 0o044, f"{p} is not group/world readable: {oct(mode)}"
    finally:
        os.umask(old)


def test_reuse_respects_cache_false(home, tmp_path):
    """BUGS #35: a step marked cache: false (environment checks) was reused from another run by --reuse."""
    plan = {"flower": 1, "id": "r3",
            "nodes": [{"id": "check", "kind": "shell", "cache": False, "run": "echo checked"},
                      {"id": "b", "kind": "shell", "run": "echo b"}]}
    e1 = create_run(plan, {}, root=home, approve=True)
    assert drive(e1, timeout=20).status == "succeeded"
    e2 = create_run(plan, {}, root=home, approve=True, reuse_from=[e1.state().run_id])
    assert drive(e2, timeout=20).status == "succeeded"
    st = e2.state()
    assert st.nodes["check"].result.reused_from is None          # ran again
    assert st.nodes["b"].result.reused_from.endswith(":b#a1")    # cacheable: reused
