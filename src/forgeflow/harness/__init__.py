"""Agent harness adapters."""
from __future__ import annotations

from .base import AgentRequest, Harness, HarnessResult, Invocation, extract_json
from .claude import ClaudeHarness
from .codex import CodexHarness
from .pi import PiHarness
from .script import ScriptHarness

HARNESSES: dict[str, type[Harness]] = {"claude": ClaudeHarness, "codex": CodexHarness, "pi": PiHarness,
                                       "script": ScriptHarness}


def get_harness(name: str) -> Harness:
    try:
        return HARNESSES[name]()
    except KeyError:
        raise ValueError(f"unknown harness {name!r}; known: {', '.join(HARNESSES)}") from None


__all__ = ["AgentRequest", "Harness", "HarnessResult", "Invocation", "extract_json", "get_harness", "HARNESSES"]
