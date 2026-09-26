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


def _run(files, batch_impl=None, review_impl=None, should_stop=None, concurrency=2):
    from app.services import multi_review_agent as mra

    settings = SimpleNamespace(
        review_mode="fast", review_max_full_files=80, review_llm_budget=1,
        review_concurrency=concurrency, llm_provider="ollama", summary_mixture_models="",
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
            return "".join([tok async for tok in mra.stream_multi_review(
                files, should_stop=should_stop,
            )])

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


# ── Stop: a run the reader abandoned must stop costing the model ───────────────


def _markers_of(text: str) -> list[dict]:
    from app.services.stream_protocol import STATUS_CLOSE, STATUS_OPEN, decode_status

    markers, cursor = [], 0
    while True:
        start = text.find(STATUS_OPEN, cursor)
        if start == -1:
            return markers
        end = text.find(STATUS_CLOSE, start)
        markers.append(decode_status(text[start + len(STATUS_OPEN):end]))
        cursor = end + len(STATUS_CLOSE)


def _stopped(name: str):
    """A file whose review is armed to stop the run as soon as it has been seen."""

    return name


def test_a_run_stopped_before_it_starts_reviews_nothing_and_says_so(isolated_data_dir):
    """
    `should_stop` true from the first check: zero model calls, one cancelled marker.

    The claim being pinned is the expensive one. A Stop that only stops *printing*
    leaves every queued file's model call running to completion on a machine with no
    budget, which is the opposite of why the button exists — so the workers consult
    the predicate before doing work, not only before describing it.
    """
    files = [{"file_path": f"pkg/{n}.py", "file_name": f"{n}.py", "language": "py",
              "content": ORDINARY} for n in ("a", "b", "c", "d")]
    calls: list[str] = []

    async def review(file_name, content, language, repo_context="", model_override=""):
        calls.append(file_name)
        yield f"model review for {file_name}"

    async def always_stopped():
        return True

    out = _run(files, review_impl=review, should_stop=always_stopped)
    markers = _markers_of(out)

    assert calls == [], f"a stopped run made {len(calls)} model calls"
    cancelled = [m for m in markers if m["step"] == "cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["published"] == 0 and cancelled[0]["not_reviewed"] == 4
    assert cancelled[0]["files_skipped"] == ["a.py", "b.py", "c.py", "d.py"]
    assert "were not reviewed" in cancelled[0]["message"]
    # No sections were opened: half a repo review is not a review.
    assert "__SECTION_START__" not in out


def test_files_queued_behind_a_stop_never_reach_the_model(isolated_data_dir):
    """
    Concurrency 1, three files, the stop armed by the first review's completion.

    Only the file already in flight does any work; the two still waiting on the
    semaphore must bail at their own check. With the guard only in the driver's loop,
    this test would see three calls and one published section — the version of Stop
    that looks like a cancellation and behaves like a bill.
    """
    # `auth.py` carries security-sensitive constructs, which is what the planner
    # reads as "worth a call of its own"; the other two go into a shared batch
    # request. Both paths are counted, because "the model was not called again" has to
    # be true of the batch path too or the test proves nothing about a 60-file repo.
    files = [
        {"file_path": "pkg/auth.py", "file_name": "auth.py", "language": "py",
         "content": ORDINARY + "\nimport hashlib\nhashlib.md5(b'x')\n"},
        {"file_path": "pkg/b.py", "file_name": "b.py", "language": "py", "content": ORDINARY},
        {"file_path": "pkg/c.py", "file_name": "c.py", "language": "py", "content": ORDINARY},
    ]
    calls: list[str] = []
    state = {"armed": False}

    async def review(file_name, content, language, repo_context="", model_override=""):
        calls.append(f"single:{file_name}")
        state["armed"] = True
        yield f"model review for {file_name}"

    async def batch(files_slice, repo_context_map=None, model_override=""):
        calls.append(f"batch:{len(files_slice)}")
        for info in files_slice:
            yield block(info["file_name"], f"## {info['file_name']}")

    async def stopping():
        return state["armed"]

    out = _run(files, review_impl=review, batch_impl=batch, should_stop=stopping, concurrency=1)
    markers = _markers_of(out)

    assert calls == ["single:auth.py"], f"expected one in-flight call, got {calls}"
    cancelled = next(m for m in markers if m["step"] == "cancelled")
    # `published` counts sections the reader was actually given. auth.py's review was
    # paid for and finished, but the reader had gone, so it is not something anyone
    # received — and the marker's whole job is to say what is missing.
    assert cancelled["files_skipped"] == ["auth.py", "b.py", "c.py"]
    assert cancelled["not_reviewed"] == 3
    assert cancelled["published"] == 0
    assert "model review for a.py" not in out


def test_a_stopped_run_is_not_reported_as_a_set_of_failures():
    """
    Cancellation is a state, not an error, and must not wear error clothing.

    The tempting implementation routes a stop through the same fallback path as a
    provider failure, which would mark every unreviewed file "static analysis shown —
    model call failed". A reader then hunts for a broken model that was never the
    problem, and the run's coverage numbers become wrong in the interesting
    direction.
    """
    files = [{"file_path": f"pkg/{n}.py", "file_name": f"{n}.py", "language": "py",
              "content": ORDINARY} for n in ("a", "b", "c")]

    async def always_stopped():
        return True

    out = _run(files, should_stop=always_stopped)
    markers = _markers_of(out)

    assert "__ERROR__" not in out
    assert not any(m["step"] in ("tool_error",) for m in markers)
    assert "Static analysis shown" not in out
    assert not any(m["step"] == "coverage" for m in markers), "a stopped run has no coverage to report"


def test_a_run_without_a_stop_predicate_is_unchanged():
    """The default path must not gain a cancelled marker or a lost section."""
    from app.services.stream_protocol import SECTION_CLOSE, SECTION_OPEN

    files = [{"file_path": f"pkg/{n}.py", "file_name": f"{n}.py", "language": "py",
              "content": ORDINARY} for n in ("a", "b")]

    out = _run(files)
    markers = _markers_of(out)

    assert not any(m["step"] == "cancelled" for m in markers)
    # One section per file, plus the repo summary that only a completed run writes.
    opened = [
        json.loads(line.split(SECTION_OPEN)[1].split(SECTION_CLOSE)[0])["id"]
        for line in out.splitlines() if line.startswith(SECTION_OPEN)
    ]
    # One section per file, plus the repo summary that only a completed run writes.
    assert opened == ["pkg/a.py", "pkg/b.py", "__repo_summary__"], opened


def test_a_stop_arriving_mid_flight_still_ends_the_run():
    """
    The reader leaves while every in-flight review is still waiting on the model.

    That is the ordinary case — a local model call outlives the patience that makes
    someone press Stop — and a design where the disconnect is only noticed *between*
    files cannot serve it: with concurrency 1 and three files, the driver is parked on
    the one call in flight and never regains control. So the run carries a watcher that
    cancels the outstanding work; this asserts the queue stayed empty and the call in
    flight was torn down rather than finished for nobody.
    """
    import time as _clock

    from app.services import multi_review_agent as mra

    # Content the planner sends to a model on its own — a file it can settle with the
    # parser would be routed static, and a static file has no call to cancel.
    risky = "\n".join([
        "import requests",
        "",
        "def check(token, secret, user):",
        "    total = 0",
        "    for part in token.split('.'):",
        "        if len(part) > 64 and part.startswith('x'):",
        "            total += sum(ord(c) for c in part)",
        "    if len(secret) < 8:",
        "        raise ValueError('short secret')",
        "    resp = requests.get('http://internal', verify=False)",
        "    return resp.ok and total < 100",
    ] * 8)
    files = [{"file_path": f"pkg/{n}.py", "file_name": f"{n}.py", "language": "py",
              "content": risky} for n in ("a", "b", "c")]
    recorder = {"started": [], "cancelled": [], "finished": []}
    # A one-element box because `collect`'s closure assigns to it and the predicate
    # reads it; a plain local would need a `nonlocal` in a nested function.
    started_at = [0.0]

    async def review(file_name, content, language, repo_context="", model_override=""):
        recorder["started"].append(file_name)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            recorder["cancelled"].append(file_name)
            raise
        recorder["finished"].append(file_name)
        yield f"model review for {file_name}"

    async def batch(files_slice, repo_context_map=None, model_override=""):
        names = [info["file_name"] for info in files_slice]
        recorder["started"].append("batch:" + ",".join(names))
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            recorder["cancelled"].extend(names)
            raise
        recorder["finished"].extend(names)
        for info in files_slice:
            yield f"## {info['file_name']}"

    settings = SimpleNamespace(
        review_mode="fast", review_max_full_files=80, review_llm_budget=80,
        review_concurrency=1, llm_provider="ollama", summary_mixture_models="",
        review_cache_enabled=False,
    )

    async def collect():
        async def stopping():
            return _clock.monotonic() - started_at[0] > 0.05

        class _DeadLLM:
            def astream(self, messages):
                async def gen():
                    raise RuntimeError("summary disabled in tests")
                    yield ""
                return gen()

            def with_config(self, **_):
                return self

        with patch.object(mra, "stream_fast_code_review", review), \
             patch.object(mra, "stream_code_review", review), \
             patch.object(mra, "stream_batch_code_review", batch), \
             patch.object(mra, "get_settings", lambda: settings), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            started_at[0] = _clock.monotonic()
            return "".join([tok async for tok in mra.stream_multi_review(
                files, should_stop=stopping,
            )])

    async def run_with_timeout():
        # The timeout is the assertion in disguise: without the watcher this test
        # would sit through a 30-second fake model call before failing.
        return await asyncio.wait_for(collect(), 5.0)

    out = asyncio.run(run_with_timeout())

    # One call was allowed in flight by the semaphore; the other two never started.
    assert recorder["started"] == ["a.py"], recorder["started"]
    assert recorder["cancelled"] == ["a.py"], recorder["cancelled"]
    assert recorder["finished"] == []
    # Nothing was published after the stop, and nothing claimed to have finished.
    assert "__SECTION_START__" not in out
    assert '"complete"' not in out


def test_a_consumer_that_dies_while_reviews_are_open_releases_the_model():
    """
    The task reading the stream is cancelled mid-await, and the model is let go.

    This is the shape a real disconnect takes on a server: Starlette can abandon a
    streaming response's generator instead of unwinding it, and an abandoned frame runs
    no `finally` of mine — which is why the run also hangs its teardown on the consumer
    *task* finishing. The reviews are detached tasks (that is what lets six of them
    share two semaphore slots) and detached tasks outlive the frame that made them, so
    "the response ended" is not the same statement as "the work stopped".

    Here the consumer is ours to cancel, so the effect is measured rather than assumed:
    the file in flight is torn down, and the two behind it never start.
    """
    import time as _clock

    from app.services import multi_review_agent as mra

    risky = "\n".join([
        "import requests",
        "",
        "def check(token, secret):",
        "    total = 0",
        "    for part in token.split('.'):",
        "        if len(part) > 64:",
        "            total += 1",
        "    resp = requests.get('http://internal', verify=False)",
        "    return resp.ok and total < 100",
        "",
    ] * 10)
    files = [{"file_path": f"pkg/{n}.py", "file_name": f"{n}.py", "language": "py",
              "content": risky} for n in ("a", "b", "c")]
    recorder = {"started": [], "ended": [], "finished": []}

    async def review(file_name, content, language, repo_context="", model_override=""):
        recorder["started"].append(file_name)
        try:
            await asyncio.sleep(30)
        finally:
            recorder["ended"].append(file_name)
        recorder["finished"].append(file_name)
        yield f"model review for {file_name}"

    async def batch(files_slice, repo_context_map=None, model_override=""):
        for info in files_slice:
            await review(info["file_name"], "", "", "", "")
            yield f"## {info['file_name']}"

    settings = SimpleNamespace(
        review_mode="fast", review_max_full_files=80, review_llm_budget=80,
        review_concurrency=1, llm_provider="ollama", summary_mixture_models="",
        review_cache_enabled=False,
    )

    async def scenario():
        async def pull():
            async for _chunk in mra.stream_multi_review(files):
                pass

        class _DeadLLM:
            def astream(self, messages):
                async def gen():
                    raise RuntimeError("summary disabled in tests")
                    yield ""
                return gen()

            def with_config(self, **_):
                return self

        with patch.object(mra, "stream_fast_code_review", review), \
             patch.object(mra, "stream_code_review", review), \
             patch.object(mra, "stream_batch_code_review", batch), \
             patch.object(mra, "get_settings", lambda: settings), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            consumer = asyncio.create_task(pull())
            deadline = _clock.monotonic() + 2.0
            while not recorder["started"] and _clock.monotonic() < deadline:
                await asyncio.sleep(0.01)
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
            # Bounded wait for the cancellation to be delivered, so a run that leaves
            # its work suspended fails here instead of hanging the suite.
            deadline = _clock.monotonic() + 1.0
            while (len(recorder["ended"]) < len(recorder["started"])
                   and _clock.monotonic() < deadline):
                await asyncio.sleep(0.01)

        return dict(recorder)

    out = asyncio.run(scenario())

    assert out["started"] == ["a.py"], out["started"]
    assert out["ended"] == ["a.py"], f"in-flight review left running: {out}"
    assert out["finished"] == [], f"a cancelled consumer still completed a review: {out}"
