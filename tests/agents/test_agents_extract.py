"""Unit tests: final-answer JSON extraction and error classification (harness/base.py)."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from forgeflow.harness.base import classify_error, extract_json, parse_retry_after


# ---------------------------------------------------------------- extract_json

def test_whole_text_object():
    assert extract_json('{"a": 1, "b": [1, 2]}') == {"a": 1, "b": [1, 2]}


def test_whole_text_with_bom_and_whitespace():
    assert extract_json('\ufeff  \n{"a": 1}\n  ') == {"a": 1}


def test_whole_text_array_is_returned_as_is():
    # the executor decides that a list is not an acceptable answer
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


@pytest.mark.parametrize("text", ["", None, "   ", "no json here", "{ not json", "}}}}"])
def test_nothing_to_extract(text):
    assert extract_json(text) is None


def test_fenced_json_block():
    text = 'All done.\n\n```json\n{"energy": -5.4, "summary": "ok"}\n```\n'
    assert extract_json(text) == {"energy": -5.4, "summary": "ok"}


def test_fenced_block_without_language_tag():
    text = 'Result:\n```\n{"x": 1}\n```'
    assert extract_json(text) == {"x": 1}


def test_last_fence_wins_over_earlier_fence():
    text = 'Example:\n```json\n{"x": 0}\n```\nFinal:\n```json\n{"x": 1}\n```'
    assert extract_json(text) == {"x": 1}


def test_invalid_last_fence_falls_back_to_earlier_valid_fence():
    text = '```json\n{"x": 1}\n```\nthen\n```json\n{"x": oops}\n```'
    assert extract_json(text) == {"x": 1}


def test_last_balanced_object_after_prose():
    text = 'I computed the energy and here is the answer {"energy": 3, "summary": "s"}'
    assert extract_json(text) == {"energy": 3, "summary": "s"}


def test_json_quoted_earlier_in_prose_loses_to_final_answer():
    text = ('The tool printed {"status": "intermediate", "energy": 0} while running.\n'
            'Final answer:\n{"status": "final", "energy": 42}')
    assert extract_json(text) == {"status": "final", "energy": 42}


def test_braces_inside_strings_are_ignored():
    obj = {"code": "int main() { return 0; }", "note": "a } b { c", "summary": "s"}
    text = "Here you go:\n" + json.dumps(obj)
    assert extract_json(text) == obj


def test_escaped_quotes_and_backslashes_inside_strings():
    obj = {"msg": 'he said "}" and left \\', "path": "C:\\dir\\"}
    text = "answer: " + json.dumps(obj) + "\n"
    assert extract_json(text) == obj


def test_nested_objects():
    obj = {"a": {"b": {"c": [1, {"d": 2}]}}, "summary": "nested"}
    assert extract_json("prefix " + json.dumps(obj)) == obj


def test_trailing_stray_brace_after_answer():
    text = 'answer {"a": 1} and a smiley :}'
    assert extract_json(text) == {"a": 1}


def test_trailing_brace_inside_quotes_after_answer():
    text = '{"a": 1}\nNote: never type "}" by itself.'
    assert extract_json(text) == {"a": 1}


def test_two_bare_objects_last_wins():
    assert extract_json('{"a": 1}\n{"b": 2}') == {"b": 2}


def test_unicode_content():
    obj = {"summary": "能量收敛 ✓", "e": 1}
    assert extract_json("结果：" + json.dumps(obj, ensure_ascii=False)) == obj


def test_final_bare_answer_beats_earlier_fenced_example():
    text = ('The schema example from the task was:\n```json\n{"energy": 0, "summary": "example"}\n```\n'
            'I ran the calculation. Final answer:\n{"energy": -7.25, "summary": "converged"}')
    assert extract_json(text) == {"energy": -7.25, "summary": "converged"}


QUADRATIC_PROG = r"""
from forgeflow.harness.base import extract_json
code = "\n".join("    if (x) { y(); }" for _ in range(4000)) + "\n" + "}" * 3000
assert extract_json(code) is None
"""


def test_extraction_is_not_quadratic_on_brace_heavy_output():
    # An agent/script dumping ~75 KB of code-like text with unmatched closing braces and no final JSON
    # must not stall the tick (which holds the run lock). Bounded in a subprocess to keep the suite fast.
    try:
        subprocess.run([sys.executable, "-c", QUADRATIC_PROG], check=True, timeout=4)
    except subprocess.TimeoutExpired:
        pytest.fail("extract_json took > 4 s on 75 KB of brace-heavy text")


# ---------------------------------------------------------------- classify_error

@pytest.mark.parametrize("text,cls", [
    ("Invalid API key · Please run /login", "auth"),
    ("Error: not logged in", "auth"),
    ("OAuth token has expired", "auth"),
    ("HTTP 401 Unauthorized", "auth"),
    ("authentication_failed", "auth"),
    ("You've hit your usage limit · resets 3pm", "quota"),
    ("you've hit your session limit", "quota"),
    ("Usage limit reached for this month", "quota"),
    ("rate_limit exceeded", "quota"),
    ("rate limited by upstream", "quota"),
    ("429 Too Many Requests", "quota"),
    ("insufficient_quota", "quota"),
    ("error: unknown model 'gpt-9'", "config"),
    ("model foo does not exist", "config"),
    ("error: unrecognized argument --frobnicate", "config"),
    ("error: unexpected argument '--x' found", "config"),
    ("segmentation fault", None),
    ("", None),
    (None, None),
])
def test_classify_error(text, cls):
    assert classify_error(text) == cls


def test_auth_wins_over_quota_when_both_match():
    assert classify_error("401 unauthorized after 429 too many requests") == "auth"


@pytest.mark.parametrize("text,secs", [
    ("rate limited, retry after 30s", 30.0),
    ("Please try again in 2 minutes.", 120.0),
    ("try again in 1 hour", 3600.0),
    ("try again in 45 seconds", 45.0),
    ("nothing useful", None),
    ("", None),
])
def test_parse_retry_after(text, secs):
    assert parse_retry_after(text) == secs
