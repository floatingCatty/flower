"""Template references (typed whole-string refs, interpolation, escapes) and the safe expression language."""
from __future__ import annotations

import multiprocessing as mp

import pytest

from forgeflow import template as tpl
from forgeflow.template import TemplateError

CTX = {
    "inputs": {"n": 3, "name": "si", "tags": ["a", "b"], "cfg": {"k": 1.5}, "flag": False, "dollar": "$${x}"},
    "run": {"id": "r1", "dir": "/runs/r1"},
    "nodes": {"a": {"outputs": {"items": [1, 2, 3], "nested": {"deep": [{"v": "x"}]}, "s": "true", "ok": 1},
                    "files": {"log": "/tmp/a.log"}, "dir": "/w/a", "summary": "done"}},
    "env": {"HOME": "/home/me"},
}
R = tpl.make_resolver(CTX)


def render(v):
    return tpl.render(v, R)


def test_whole_string_ref_keeps_raw_type():
    assert render("${inputs.n}") == 3
    assert render("${inputs.tags}") == ["a", "b"]
    assert render("${inputs.cfg}") == {"k": 1.5}
    assert render("${inputs.flag}") is False
    assert render("${a.outputs.items}") == [1, 2, 3]
    assert render("  ${a.outputs.items}  ") == [1, 2, 3]
    assert render("${ inputs.n }") == 3


def test_interpolation_stringifies_non_strings_as_json():
    assert render("n=${inputs.n}") == "n=3"
    assert render("tags=${inputs.tags}") == 'tags=["a", "b"]'
    assert render("${inputs.name}-${inputs.n}") == "si-3"
    assert render("flag=${inputs.flag}") == "flag=false"


def test_paths_into_lists_and_dicts():
    assert render("${a.outputs.nested.deep.0.v}") == "x"
    assert render("${a.outputs.items.-1}") == 3
    assert render("${a.files.log}") == "/tmp/a.log"
    assert render("${a.dir}/x") == "/w/a/x"
    assert render("${a.summary}") == "done"
    assert render("${run.id}") == "r1"
    assert render("${env.HOME}") == "/home/me"


def test_nested_structures_rendered_recursively():
    out = render({"x": ["${inputs.n}", {"y": "v=${inputs.name}"}], "z": 7})
    assert out == {"x": [3, {"y": "v=si"}], "z": 7}


def test_escape():
    assert render("$${inputs.n}") == "${inputs.n}"
    assert render("cost: $${HOME} and ${inputs.n}") == "cost: ${HOME} and 3"
    assert tpl.find_refs("$${inputs.n}") == []


def test_missing_refs_raise_template_error():
    for bad in ("${inputs.nope}", "${a.outputs.missing}", "${zz.outputs.x}", "${a.outputs.items.9}",
                "x ${item}", "${index}"):
        with pytest.raises(TemplateError):
            render(bad)


def test_escape_not_applied_to_substituted_values():
    assert render("${inputs.dollar}") == "$${x}"          # whole-string: raw, untouched
    assert render("v=${inputs.dollar}") == "v=$${x}"      # interpolated: should be untouched too


def test_find_and_node_refs():
    refs = tpl.find_refs({"a": "${x.outputs.y} ${inputs.q}", "b": ["${item.k}", "${env.P}"]})
    assert ("x", ["outputs", "y"]) in refs
    assert tpl.node_refs({"a": "${x.outputs.y} ${inputs.q} ${run.id} ${item} ${index} ${env.P}"}) == {"x"}


# ------------------------------------------------------------------ expressions

def ev(expr):
    return tpl.evaluate(expr, R)


@pytest.mark.parametrize("expr,val", [
    ("${inputs.n} > 2", True),
    ("${inputs.n} == 3 and not ${inputs.flag}", True),
    ("${inputs.flag} or ${inputs.n}", 3),
    ("len(${a.outputs.items}) == 3", True),
    ("max(${a.outputs.items}) + 1", 4),
    ("'a' in ${inputs.tags}", True),
    ("'z' not in ${inputs.tags}", True),
    ("${a.outputs.ok} == true", True),
    ("${inputs.flag} == false", True),
    ("${inputs.cfg}['k'] * 2", 3.0),
    ("${a.outputs.items}[0]", 1),
    ("1 < ${inputs.n} <= 3", True),
    ("'yes' if ${inputs.n} > 1 else 'no'", "yes"),
    ("null == None", True),
    ("[1, 2] == [1, 2]", True),
    ("{'a': 1}['a']", 1),
    ("-${inputs.n}", -3),
    ("${inputs.n} % 2", 1),
    ("${inputs.n} // 2", 1),
    ("${inputs.n} ** 2", 9),
    ("round(2.567, 1)", 2.6),
    ("all([true, ${inputs.n} > 0])", True),
])
def test_expressions(expr, val):
    assert ev(expr) == val
    tpl.check_expr(expr)


@pytest.mark.parametrize("expr", [
    "${inputs.name}.upper()",          # attribute access
    "(lambda: 1)()",                   # lambda
    "open('/etc/passwd')",             # arbitrary call
    "__import__('os').system('true')",  # dunder import
    "[x for x in ${inputs.tags}]",     # comprehension
    "str(1, encoding='x')",             # keyword args to an allowed call (eval-time rejection)
    "().__class__",
    "${a.outputs.items}.__len__()",
])
def test_unsafe_expressions_rejected(expr):
    with pytest.raises(TemplateError):
        tpl.check_expr(expr) if "encoding" not in expr else None
        ev(expr)


def test_unknown_names_rejected():
    with pytest.raises(TemplateError):
        ev("os")
    with pytest.raises(TemplateError):
        tpl.check_expr("((")


def test_true_false_inside_string_literals_untouched():
    assert ev('${a.outputs.s} == "true"') is True
    assert ev("'false'") == "false"


def _eval_in_child(expr, q):
    try:
        tpl.check_expr(expr)
        q.put(("ok", repr(tpl.evaluate(expr, lambda *a: None))[:20]))
    except TemplateError as exc:
        q.put(("rejected", str(exc)))


def test_expression_resource_bomb_is_bounded():
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_eval_in_child, args=("9 ** 9 ** 9 > 0", q))
    p.start()
    p.join(2)
    hung = p.is_alive()
    if hung:
        p.kill()
        p.join()
    assert not hung, "evaluating `9 ** 9 ** 9 > 0` did not finish (or get rejected) within 2s"
