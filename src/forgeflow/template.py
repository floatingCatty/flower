"""``${...}`` references and safe expressions.

References (resolved at node start, frozen into ``node.started``):

* ``${inputs.NAME}``                 run input
* ``${NODE.outputs.KEY[.path...]}``  structured output of the node's latest successful attempt
* ``${NODE.files.NAME}``             absolute path of a declared output file
* ``${NODE.dir}``                    the node attempt's working directory
* ``${NODE.summary}``                one-line summary of the node result
* ``${run.id}`` ``${run.dir}``        run identity
* ``${item}`` ``${item.field}`` ``${index}``  foreach expansion bindings
* ``${env.NAME}``                    environment variable of the forgeflow process
* ``${plan.dir}``                    directory of the plan file (for helper scripts shipped with it)
* ``${feedback}``                    text of the gate decision that sent this node back for rework
                                     (empty on the first pass; creates no dependency edge)

A string that is exactly one reference evaluates to the raw value (list, dict, number...);
otherwise values are interpolated (non-strings as JSON). ``$${`` escapes a literal ``${``.
"""
from __future__ import annotations

import ast
import json
import operator
import re
from typing import Any, Callable

REF = re.compile(r"(?<!\$)\$\{\s*([A-Za-z_][A-Za-z0-9_\-\[\]]*(?:\.[A-Za-z0-9_\-\[\]]+)*)\s*\}")
# one pass: either the escape `$${` or a reference, so substituted values are never re-scanned
ESC_OR_REF = re.compile(r"\$\$\{|(?<!\$)\$\{\s*([A-Za-z_][A-Za-z0-9_\-\[\]]*(?:\.[A-Za-z0-9_\-\[\]]+)*)\s*\}")
RESERVED_ROOTS = {"inputs", "run", "item", "index", "env", "feedback", "plan"}


class TemplateError(Exception):
    pass


def parse_ref(text: str) -> tuple[str, list[str]]:
    parts = text.split(".")
    return parts[0], parts[1:]


def find_refs(value: Any) -> list[tuple[str, list[str]]]:
    """All references inside a (nested) value, as (root, path) pairs."""
    out: list[tuple[str, list[str]]] = []
    if isinstance(value, str):
        for m in REF.finditer(value):
            out.append(parse_ref(m.group(1)))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(find_refs(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(find_refs(v))
    return out


def node_refs(value: Any) -> set[str]:
    return {root for root, _ in find_refs(value) if root not in RESERVED_ROOTS}


def _dig(value: Any, path: list[str], full: str) -> Any:
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, (list, tuple)) and re.fullmatch(r"-?\d+", key):
            try:
                value = value[int(key)]
            except IndexError:
                raise TemplateError(f"${{{full}}}: index {key} out of range") from None
        else:
            raise TemplateError(f"${{{full}}}: no key {key!r}")
    return value


def render(value: Any, resolver: Callable[[str, list[str], str], Any]) -> Any:
    """Render references in a nested value. ``resolver(root, path, fulltext)`` returns the raw value."""
    if isinstance(value, str):
        m = REF.fullmatch(value.strip()) if value.strip().startswith("${") else None
        if m and value.strip() == m.group(0):
            root, path = parse_ref(m.group(1))
            return resolver(root, path, m.group(1))

        def sub(mm: re.Match) -> str:
            if mm.group(0) == "$${":
                return "${"
            root, path = parse_ref(mm.group(1))
            v = resolver(root, path, mm.group(1))
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

        return ESC_OR_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, resolver) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, resolver) for v in value]
    return value


def make_resolver(context: dict) -> Callable[[str, list[str], str], Any]:
    """Resolver over a context dict: {'inputs':…, 'run':…, 'nodes': {id: {'outputs','files','dir','summary'}}, 'item':…}."""

    def resolve(root: str, path: list[str], full: str) -> Any:
        if root in RESERVED_ROOTS:
            if root not in context:
                raise TemplateError(f"${{{full}}}: '{root}' is not available here")
            return _dig(context[root], path, full)
        nodes = context.get("nodes", {})
        if root not in nodes:
            raise TemplateError(f"${{{full}}}: node '{root}' has no successful result to reference")
        return _dig(nodes[root], path, full)

    return resolve


# ---------------------------------------------------------------- safe expressions

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.Pow: operator.pow}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not}
_FUNCS = {"len": len, "abs": abs, "min": min, "max": max, "round": round, "bool": bool,
          "int": int, "float": float, "str": str, "any": any, "all": all, "sum": sum}


def _eval(node: ast.AST, names: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, names)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        if node.id in ("true", "True"):
            return True
        if node.id in ("false", "False"):
            return False
        if node.id in ("null", "None"):
            return None
        raise TemplateError(f"unknown name {node.id!r} in expression")
    if isinstance(node, ast.BoolOp):
        vals = node.values
        if isinstance(node.op, ast.And):
            res = True
            for v in vals:
                res = _eval(v, names)
                if not res:
                    return res
            return res
        res = False
        for v in vals:
            res = _eval(v, names)
            if res:
                return res
        return res
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, names)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left, names), _eval(node.right, names)
        if isinstance(node.op, ast.Pow):
            if not all(isinstance(x, (int, float)) for x in (left, right)) or abs(right) > 64 or abs(left) > 1e6:
                raise TemplateError("expression: ** is limited to |base| <= 1e6 and |exponent| <= 64")
        if isinstance(node.op, ast.Mult):
            for seq, n in ((left, right), (right, left)):
                if isinstance(seq, (str, list, tuple)) and isinstance(n, int) and len(seq) * max(n, 0) > 1_000_000:
                    raise TemplateError("expression: sequence repetition too large")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, names)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, names)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, names) if _eval(node.test, names) else _eval(node.orelse, names)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, names) for e in node.elts]
    if isinstance(node, ast.Dict):
        return {_eval(k, names): _eval(v, names) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Subscript):
        target = _eval(node.value, names)
        idx = node.slice.value if isinstance(node.slice, ast.Index) else node.slice  # py<3.9 compat
        return target[_eval(idx, names)]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a, names) for a in node.args])
    raise TemplateError(f"unsupported expression element: {ast.dump(node)[:80]}")


def compile_expr(expr: str) -> tuple[ast.Expression, dict[str, tuple[str, list[str], str]]]:
    refs: dict[str, tuple[str, list[str], str]] = {}

    def sub(m: re.Match) -> str:
        name = f"__r{len(refs)}"
        root, path = parse_ref(m.group(1))
        refs[name] = (root, path, m.group(1))
        return name

    src = REF.sub(sub, expr.strip())
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise TemplateError(f"invalid expression {expr!r}: {exc.msg}") from None
    return tree, refs


def evaluate(expr: str, resolver: Callable[[str, list[str], str], Any]) -> Any:
    tree, refs = compile_expr(expr)
    names = {name: resolver(root, path, full) for name, (root, path, full) in refs.items()}
    return _eval(tree, names)


def check_expr(expr: str) -> None:
    """Syntax/safety check without resolving references."""
    tree, refs = compile_expr(expr)
    for n in ast.walk(tree):
        if isinstance(n, (ast.Attribute, ast.Lambda, ast.ListComp, ast.DictComp, ast.SetComp,
                          ast.GeneratorExp, ast.Await, ast.Starred)):
            raise TemplateError(f"expression {expr!r}: {type(n).__name__} is not allowed")
        if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in _FUNCS):
            raise TemplateError(f"expression {expr!r}: only {sorted(_FUNCS)} may be called")
