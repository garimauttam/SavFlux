"""
Tests for batched multi-file review and the coverage report.

Batching is the one optimisation in this pipeline that can produce a *wrong*
result rather than a slow one: if the parser that splits a batched answer
mis-attributes a block, one file is shown another file's findings. That is worse
than not batching at all, so the splitter has its own tests and they lean on
mismatches deliberately.

The coverage token gets tests for the same reason: it is a claim about how much
of a repository a model actually saw, and it must not conflate "the parser
covered this file" with "the model failed on this file".
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.services.review_agent import (
    BATCH_END_MARKER,
    BATCH_FILE_MARKER,
    _batch_prompt,
    split_batch_review,
)

# ── Splitting a batched answer ────────────────────────────────────────────────


def block(name: str, body: str = "## 🔒 Security\nNone found") -> str:
    return f"=== FILE: {name} ===\n{body}\n=== END FILE ===\n"


def test_splits_each_file_into_its_own_review():
    text = block("a.py", "## A") + block("b.py", "## B")
    result = split_batch_review(text, ["a.py", "b.py"])

    assert result == {"a.py": "## A", "b.py": "## B"}


def test_a_model_that_shortens_a_path_still_matches_its_file():
    """
    Models routinely echo `service.py` after being shown `app/api/service.py`.
    Matching the basename keeps the answer attached to the right file; failing to
    match would silently demote a reviewed file to static analysis.
    """
    text = block("service.py", "## S")
    result = split_batch_review(text, ["app/api/service.py"])

    assert result == {"app/api/service.py": "## S"}


def test_a_file_absent_from_the_answer_is_absent_from_the_result():
    """
    The critical property: a missing file must produce *nothing*, so the caller
    falls back to static analysis instead of reusing a neighbour's text.
    """
    text = block("a.py") + block("c.py")
    result = split_batch_review(text, ["a.py", "b.py", "c.py"])

    assert "b.py" not in result
    assert set(result) == {"a.py", "c.py"}


def test_an_answer_in_a_different_order_is_still_attributed_correctly():
    text = block("c.py", "## C") + block("a.py", "## A") + block("b.py", "## B")
    result = split_batch_review(text, ["a.py", "b.py", "c.py"])

    assert result["a.py"] == "## A"
    assert result["b.py"] == "## B"
    assert result["c.py"] == "## C"


def test_content_that_looks_like_a_marker_inside_a_review_does_not_confuse_the_split():
    """A review *about* the marker format must not terminate the split early."""
    text = block("a.py", "## A\nthe parser uses `=== END FILE ===` as a delimiter")
    result = split_batch_review(text, ["a.py", "b.py"])

    assert "a.py" in result
    assert "b.py" not in result


def test_empty_or_unstructured_output_yields_nothing():
    assert split_batch_review("", ["a.py"]) == {}
    assert split_batch_review("I could not review these files.", ["a.py"]) == {}
    assert split_batch_review(block("a.py"), []) == {}


def test_a_header_without_a_footer_is_not_a_review():
    """A truncated stream must not be presented as a complete review."""
    assert split_batch_review("=== FILE: a.py ===\n## partial", ["a.py"]) == {}


# ── Prompt assembly ───────────────────────────────────────────────────────────


def test_the_prompt_carries_every_file_and_the_parser_contract():
    files = [
        {"file_name": "a.py", "content": "def a():\n    return 1\n", "language": "py"},
        {"file_name": "b.py", "content": "def b():\n    return 2\n", "language": "py"},
    ]
    prompt = _batch_prompt(files, {})

    for name in ("a.py", "b.py"):
        assert BATCH_FILE_MARKER.format(name=name) in prompt
    assert BATCH_END_MARKER in prompt
    # The model is told what the markers are for, not just that they exist.
    assert "automated parser" in prompt
    # Static facts travel with each file so the model does not re-derive them.
    assert "Verified static analysis" in prompt


def test_the_prompt_includes_cross_file_context_when_it_exists():
    files = [{"file_name": "a.py", "content": "x = 1\n" * 30, "language": "py"}]
    prompt = _batch_prompt(files, {"a.py": "imports b.py"})

    assert "Cross-file Repo Context" in prompt
    assert "imports b.py" in prompt


def test_the_prompt_truncates_a_long_file_and_says_so():
    files = [{"file_name": "a.py", "content": "y = 2\n" * 5000, "language": "py"}]
    prompt = _batch_prompt(files, {})

    assert "[Truncated" in prompt       # the model knows it is not seeing everything
    assert len(prompt) < 20_000


# ── Coverage reporting ────────────────────────────────────────────────────────


def _coverage_token(output: str) -> dict:
    """
    The run's coverage token, decoded with the protocol's own decoder.

    This used to slice the marker text apart on `"step": "coverage"` and `...`,
    which coupled a behavioural test to the marker's *format* — so the test
    failed when the format was canonicalised, for a reason that had nothing to do
    with coverage. Parsing through `decode_status` keeps the assertions about the
    numbers where they belong.
    """
    from app.services.stream_protocol import STATUS_CLOSE, STATUS_OPEN, decode_status

    cursor = 0
    while True:
        start = output.find(STATUS_OPEN, cursor)
        if start == -1:
            raise AssertionError(f"no coverage token in output:\n{output[:2000]}")
        end = output.find(STATUS_CLOSE, start)
        if end == -1:
            raise AssertionError(f"unterminated status marker:\n{output[max(0, start - 200):start + 400]}")
        payload = decode_status(output[start + len(STATUS_OPEN):end])
        if payload.get("step") == "coverage":
            return payload
        cursor = end + len(STATUS_CLOSE)


def _run(files, batch_impl=None, review_impl=None):
    from app.services import multi_review_agent as mra

    settings = SimpleNamespace(
        review_mode="fast", review_max_full_files=80, review_llm_budget=1,
        review_concurrency=2, llm_provider="ollama", summary_mixture_models="",
        review_cache_enabled=True,
    )

    async def review(file_name, content, language, repo_context="", model_override=""):
        yield f"model review for {file_name}"

    async def batch(files_slice, repo_context_map=None, model_override=""):
        for info in files_slice:
            yield block(info["file_name"], f"## {info['file_name']}")

    class _DeadLLM:
        def astream(self, messages):
            async def gen():
                raise RuntimeError("summary disabled in tests")
                yield ""
            return gen()

        def with_config(self, **_):
            return self

    async def collect():
        with patch.object(mra, "stream_fast_code_review", review_impl or review), \
             patch.object(mra, "stream_code_review", review_impl or review), \
             patch.object(mra, "stream_batch_code_review", batch_impl or batch), \
             patch.object(mra, "get_settings", return_value=settings), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            return "".join([tok async for tok in mra.stream_multi_review(files)])

    return asyncio.run(collect())


ORDINARY = '''\
"""Helpers."""


def add(a, b):
    return a + b


def scale(value, factor=2):
    return value * factor


def describe(value):
    return f"value={value}"
'''

LOCKFILE = '{"lockfileVersion": 3}\n' * 20


def test_coverage_separates_a_planner_decision_from_a_provider_failure(isolated_data_dir):
    """
    "The parser fully determined this file" and "the model failed on this file"
    are different statements about a repository, and the summary must not merge
    them into one number.
    """
    files = [
        {"file_name": f"module_{i}.py", "content": ORDINARY, "language": "py"}
        for i in range(4)
    ]
    files.append({"file_name": "package-lock.json", "content": LOCKFILE, "language": "json"})

    async def partial_batch(files_slice, repo_context_map=None, model_override=""):
        # The model answers about the first two files and forgets the rest.
        for info in files_slice[:2]:
            yield block(info["file_name"], f"## {info['file_name']}")

    coverage = _coverage_token(_run(files, batch_impl=partial_batch))

    assert coverage["total"] == 5
    assert coverage["planned_static"] == 1        # the lockfile, by policy
    assert coverage["fallback_static"] >= 1       # files the batch omitted
    assert coverage["llm"] + coverage["static"] == coverage["total"]
    assert coverage["pct"] == round(coverage["llm"] / 5 * 100)


def test_coverage_reports_cache_hits_without_calling_them_misses(isolated_data_dir):
    files = [
        {"file_name": f"module_{i}.py", "content": ORDINARY, "language": "py"}
        for i in range(4)
    ]

    first = _coverage_token(_run(files))
    assert first["cache_hits"] == 0

    second = _coverage_token(_run(files))
    assert second["cache_hits"] == 4              # every file served from cache
    assert second["llm"] == 4                     # and still counted as reviewed
    assert second["static"] == 0
    assert second["pct"] == 100
