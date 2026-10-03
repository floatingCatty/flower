"""Standalone shim used by ``function`` nodes: ``python _callfn.py module:func kwargs.json outputs.json ctx.json``.

Deliberately imports nothing from forgeflow so it runs under *any* interpreter (the user's science
environment), not just the one forgeflow is installed in.
"""
import importlib
import inspect
import json
import os
import sys
import traceback


def main() -> int:
    target, kwargs_path, out_path, ctx_path = sys.argv[1:5]
    mod_name, _, fn_name = target.partition(":")
    with open(kwargs_path) as fh:
        kwargs = json.load(fh)
    with open(ctx_path) as fh:
        ctx = json.load(fh)
    for p in reversed(ctx.get("pythonpath") or []):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        fn = getattr(importlib.import_module(mod_name), fn_name)
        params = inspect.signature(fn).parameters
        if "ctx" in params and "ctx" not in kwargs:
            kwargs["ctx"] = ctx
        accepts_any = any(p.kind == p.VAR_KEYWORD for p in params.values())
        if not accepts_any:
            unknown = [k for k in kwargs if k not in params]
            if unknown:
                raise TypeError(f"{target}() does not accept {unknown}; it takes {list(params)}")
        result = fn(**kwargs)
    except Exception:
        traceback.print_exc()
        return 1
    if not isinstance(result, dict):
        result = {"result": result}
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    os.replace(tmp, out_path)
    print(json.dumps(result, default=str)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
