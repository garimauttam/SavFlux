"""
Tests for the review streams' markers — the shared-protocol half.

The rule these enforce is the one that was broken for as long as the product
existed: the agent stream and the review stream are the *same* protocol, so a
client needs one decoder and a field added for one surface exists on both. Every
marker in a review stream must therefore be a canonical `__STATUS__{json}` line
carrying `message` inside the object, never `__STATUS__text{json}` — the old
shape, which only some clients could parse.

The timings are tested too, and not for their values: a per-file `llm_ms` of 4
seconds and a `wait_ms` of 900 milliseconds are different diagnoses (a slow model
versus a concurrency queue), and the whole point of splitting them is that a
latency claim can name which one moved.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.services.stream_protocol import (
    MAX_STATUS_BYTES,
    STATUS_CLOSE,
    STATUS_OPEN,
    decode_status,
    strip_protocol_markers,
)

ORDINARY = "def handler(a, b):\n    return a + b\n" * 12


def block(name: str, body: str = "## 🔒 Security\nNone found") -> str:
    return f"=== FILE: {name} ===\n{body}\n=== END FILE ===\n"


def _markers_and_prose(output: str) -> tuple[list[dict], str]:
    markers: list[dict] = []
    cursor, prose = 0, ""
    while True:
        start = output.find(STATUS_OPEN, cursor)
        if start == -1:
            return markers, prose + output[cursor:]
        end = output.find(STATUS_CLOSE, start)
        assert end != -1, f"unterminated marker at {start}: {output[start:start + 160]!r}"
        inner = output[start + len(STATUS_OPEN):end]
        assert inner.lstrip().startswith("{"), (
            f"marker still carries a pre-protocol text prefix: {inner[:80]!r}"
        )
        markers.append(decode_status(inner))
        prose += output[cursor:start]
        cursor = end + len(STATUS_CLOSE)


def _collect_multi(files, *, review_impl=None, batch_impl=None):
    from app.services import multi_review_agent as mra

    settings = SimpleNamespace(
        review_mode="fast", review_max_full_files=80, review_llm_budget=1,
        review_concurrency=2, llm_provider="ollama", summary_mixture_models="",
        review_cache_enabled=False,
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

    async def drive():
        with patch.object(mra, "stream_fast_code_review", review_impl or review), \
             patch.object(mra, "stream_code_review", review_impl or review), \
             patch.object(mra, "stream_batch_code_review", batch_impl or batch), \
             patch.object(mra, "get_settings", return_value=settings), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            return "".join([token async for token in mra.stream_multi_review(files)])

    return asyncio.run(drive())


def _files(count: int = 2):
    return [
        {"file_name": f"module_{i}.py", "file_path": f"src/module_{i}.py",
         "content": ORDINARY, "language": "py"}
        for i in range(count)
    ]


# ── The multi-review stream ───────────────────────────────────────────────────


def test_every_review_marker_is_a_canonical_json_object():
    markers, _ = _markers_and_prose(_collect_multi(_files()))

    assert markers
    for marker in markers:
        assert marker.get("message"), marker
        assert len(json.dumps(marker).encode("utf-8")) <= MAX_STATUS_BYTES
        assert marker["step"] in {
            "planned", "file", "complete", "timing", "coverage", "summary",
            "summary_complete", "tool", "tool_done", "writing", "starting",
        }, marker["step"]


def test_the_plan_marker_reports_the_two_deterministic_phases():
    """
    Before this, the only way to learn that parsing 80 files took 900 ms was to
    time the whole review and subtract a guess. Both phases are on the wire now.
    """
    markers, _ = _markers_and_prose(_collect_multi(_files(3)))
    planned = next(m for m in markers if m["step"] == "planned")

    assert planned["total"] == 3
    for key in ("plan_ms", "context_ms"):
        assert isinstance(planned[key], int) and planned[key] >= 0, key
    assert "message" in planned and planned["message"].startswith("Planned 3 files")


def test_a_model_reviewed_file_reports_model_time_and_queue_time_separately():
    markers, _ = _markers_and_prose(_collect_multi(_files(2)))
    done = [m for m in markers if m["step"] == "complete" and m["id"] != "__repo_summary__"]

    assert done, "no per-file completion markers"
    for marker in done:
        assert isinstance(marker["llm_ms"], int) and marker["llm_ms"] >= 0
        assert isinstance(marker["wait_ms"], int) and marker["wait_ms"] >= 0
        assert marker["tier"] in {"fast", "full"}


def test_a_file_answered_without_a_model_does_not_claim_model_time():
    """
    A static report has no `llm_ms`, and `llm_ms: 0` would be worse than absent:
    it would read as "the model answered instantly" and average away into a
    benchmark as the fastest file in the run.
    """
    files = _files(2)
    files.append({"file_name": "package-lock.json", "file_path": "package-lock.json",
                  "content": '{"lockfileVersion": 3}\n' * 20, "language": "json"})
    markers, _ = _markers_and_prose(_collect_multi(files))
    static = [m for m in markers if m["step"] == "complete" and m.get("tier") == "static"]

    assert static
    assert all("llm_ms" not in marker for marker in static)


def test_the_run_reports_its_slowest_file_by_model_time():
    markers, _ = _markers_and_prose(_collect_multi(_files(2)))
    timing = next(m for m in markers if m["step"] == "timing" and m.get("stage") == "files")

    assert isinstance(timing["elapsed_ms"], int)
    assert timing["model_calls"] >= 1
    assert timing["slowest_file"].startswith("module_"), timing
    # 0 ms is a measurement, not a gap: this run's LLM is a stub that returns at
    # once, so the field has to survive the falsy value.
    assert timing["llm_ms"] == 0


def test_the_run_tells_a_batched_group_the_same_duration_it_earned():
    """
    One model call covering four files must show up as the same `llm_ms` on all
    four cards — that equality *is* the batching win, and a per-file average would
    hide it.
    """
    async def batch(files_slice, repo_context_map=None, model_override=""):
        for info in files_slice:
            yield block(info["file_name"], f"## {info['file_name']}")

    markers, _ = _markers_and_prose(_collect_multi(_files(4), batch_impl=batch))
    batched = [m for m in markers if m["step"] == "complete" and m.get("batch")]

    assert len(batched) >= 2, batched
    assert len({m["llm_ms"] for m in batched}) == 1
    assert len({m["batch"] for m in batched}) == 1


def test_section_markers_still_open_cards_with_the_identity_the_ui_keys_on():
    output = _collect_multi(_files(1))
    assert '__SECTION_START__{"id": "src/module_0.py", "file_name": "module_0.py"}' in output


def test_prose_survives_and_markers_do_not_leak_into_it():
    output = _collect_multi(_files(2))
    prose = strip_protocol_markers(output)

    assert "model review for module_0.py" in prose
    for leak in (STATUS_OPEN, "__SECTION_", "__ERROR__"):
        assert leak not in prose


# ── The single-file fast path ─────────────────────────────────────────────────


class _Chunk:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, replies):
        self._replies = replies

    def with_config(self, **_kwargs):
        return self

    async def astream(self, _messages):
        for reply in self._replies:
            yield _Chunk(reply)


def _fast_review(replies):
    from app.services import review_agent

    llm = _FakeLLM(replies)
    with patch.object(review_agent, "get_review_llm", lambda *_a, **_k: llm):
        out = asyncio.run(_drive(review_agent.stream_fast_code_review(
            "auth.py", "import requests\n\ndef get(u):\n    return requests.get(u, verify=False)\n",
            "python",
        )))
    return _markers_and_prose(out)


async def _drive(stream) -> str:
    return "".join([chunk async for chunk in stream])


def test_the_fast_path_publishes_the_static_analysis_cost():
    markers, prose = _fast_review(["## 🐛 Bugs & Risks\nOne.", ""])

    writing = next(m for m in markers if m["step"] == "writing")
    assert isinstance(writing["analysis_ms"], int) and writing["analysis_ms"] >= 0
    assert writing["findings"] >= 0
    assert "One." in prose


def test_think_tags_still_produce_canonical_markers():
    """DeepSeek-R1 wraps its reasoning in `<think>`; the notice is a marker too."""
    markers, prose = _fast_review(["<think>weighing options</think>", "## Score\n8"])

    thinking = next(m for m in markers if m["step"] == "thinking")
    assert thinking["message"] == "Reasoning…"
    assert "weighing options" not in prose
    assert "## Score" in prose


# ── The retrieval stage clock ─────────────────────────────────────────────────


def test_a_stage_marker_reports_the_stage_that_just_finished():
    """
    The stage that just ended is the only one whose cost is known when the next one
    starts, so `prev_step`/`prev_ms` name it rather than pretending to predict.
    """
    from app.services.retrieval_service import _StageClock

    clock = _StageClock()
    first = decode_status(clock.mark("planning", "Planning…")[len(STATUS_OPEN):-len(STATUS_CLOSE) - 1])
    assert "prev_step" not in first, "the first stage has nothing behind it"

    second = decode_status(clock.mark("dense-search", "Searching…")[len(STATUS_OPEN):-len(STATUS_CLOSE) - 1])
    assert second["prev_step"] == "planning"
    assert isinstance(second["prev_ms"], int) and second["prev_ms"] >= 0


def test_the_clock_closes_once_and_reports_a_total():
    from app.services.retrieval_service import _StageClock

    clock = _StageClock()
    clock.mark("planning", "Planning…")
    closing = decode_status(clock.close(model="answer")[len(STATUS_OPEN):-len(STATUS_CLOSE) - 1])

    assert closing["step"] == "stage_done"
    assert closing["prev_step"] == "planning"
    assert closing["total_ms"] >= closing["prev_ms"]
    assert clock.close() is None, "a second close() would report a stage that never ran"


def test_stage_markers_reach_the_chat_stream_in_order():
    """
    The stages are the answer to "why was that slow", so they must be on the wire
    in pipeline order and each must carry the previous stage's duration.
    """
    from app.services import retrieval_service

    clock = retrieval_service._StageClock()
    stream = [
        clock.mark("planning", "Planning…"),
        clock.mark("dense-search", "Searching 2 variants…"),
        clock.mark("lexical-search", "BM25…"),
        clock.mark("reranking", "Fusing…"),
        clock.mark("generation", "Generating…"),
        clock.close(),
    ]
    payloads = [decode_status(marker[len(STATUS_OPEN):-len(STATUS_CLOSE) - 1]) for marker in stream]

    assert [p["step"] for p in payloads] == [
        "planning", "dense-search", "lexical-search", "reranking", "generation", "stage_done",
    ]
    assert [p.get("prev_step") for p in payloads[1:]] == [
        "planning", "dense-search", "lexical-search", "reranking", "generation",
    ]
