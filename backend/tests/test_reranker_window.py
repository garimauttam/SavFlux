"""
D2: the cross-encoder's input window must be explicit, not inherited.

`CrossEncoder.predict()` truncates each (query, passage) pair to `max_length`, and
defaults to the model's `max_seq_length` when the argument is omitted. That default is
real truncation with no warning: measured on this project's chunks, 8-14% exceed 512
tokens, so the reranker scores only a prefix of them.

This file pins the explicit window and the call that uses it. The conftest patch for
`_get_cross_encoder` is session-wide, so the model itself is never loaded here — these
tests assert on the ARGUMENTS the scoring path passes, using a recording double.
"""

import asyncio
import inspect

import pytest
from langchain_core.documents import Document

from app.services import reranker as rr


class _RecordingCrossEncoder:
    """Records every predict() call, including its keyword arguments."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def predict(self, pairs, **kwargs):
        self.calls.append({"pairs": list(pairs), "kwargs": kwargs})
        return [float(len(pairs) - i) for i in range(len(pairs))]


def _docs(count: int = 3):
    return [
        Document(
            page_content=f"def handler_{i}(request):\n    return request.args\n" * (i + 1),
            metadata={"source": f"/x/m{i}.py", "chunk_index": i},
        )
        for i in range(count)
    ]


def test_the_window_constant_matches_the_model_it_is_documented_for():
    """
    A guard on the constant, not on the model.

    If someone swaps RERANKER_MODEL, this window is no longer correct for it — and
    the same reasoning that makes `citation_service`'s thresholds model-specific
    applies here. Failing loudly is the point: the alternative is a new model being
    silently truncated at a window chosen for the old one.
    """
    assert rr.RERANKER_MAX_LENGTH == 512
    assert "ms-marco-MiniLM-L-6-v2" in rr.RERANKER_MODEL, (
        "RERANKER_MAX_LENGTH was chosen for ms-marco-MiniLM-L-6-v2; a different "
        "model needs its own window, and citation_service needs recalibrating"
    )


def test_scoring_passes_the_window_explicitly(monkeypatch):
    """
    The defect this file exists for.

    Omitting max_length leaves truncation to a library default. This fails the
    moment someone reverts to `predict(pairs)`.
    """
    recorder = _RecordingCrossEncoder()
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: recorder)

    asyncio.run(rr.rerank("what does handler do?", _docs(), top_n=2))

    assert recorder.calls, "predict() was never called"
    for call in recorder.calls:
        assert "max_length" in call["kwargs"], (
            "predict() was called without max_length, so the truncation window is "
            "inherited from the library rather than chosen here"
        )
        assert call["kwargs"]["max_length"] == rr.RERANKER_MAX_LENGTH


def test_scoring_uses_the_configured_window_not_the_default(monkeypatch):
    """
    Prove the constant is READ, not merely present.

    Hard-coding 512 at the call site would satisfy the test above; changing the
    constant must move the argument.
    """
    recorder = _RecordingCrossEncoder()
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: recorder)
    monkeypatch.setattr(rr, "RERANKER_MAX_LENGTH", 256)

    asyncio.run(rr.rerank("q", _docs(), top_n=1))

    assert recorder.calls[0]["kwargs"]["max_length"] == 256


def test_scoring_still_ranks_and_annotates(monkeypatch):
    """The window change must not disturb the scores or the metadata contract."""
    recorder = _RecordingCrossEncoder()
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: recorder)
    docs = _docs(3)

    ranked = asyncio.run(rr.rerank("q", docs, top_n=2))

    assert len(ranked) == 2
    assert all(rr.RERANK_SCORE_KEY in d.metadata for d in ranked)
    assert [d.metadata[rr.RERANK_SCORE_KEY] for d in ranked] == sorted(
        (d.metadata[rr.RERANK_SCORE_KEY] for d in ranked), reverse=True
    )
    # The double returns len(pairs)-i, so the first document scores highest.
    assert ranked[0].metadata["chunk_index"] == 0


def test_the_pairs_sent_are_the_query_and_the_documents_parent_text(monkeypatch):
    """
    Pins what is scored: the query paired with the document's PARENT text.

    This used to assert `doc.page_content`, which is what the code read before the
    reranker was made source-agnostic. Those two agree only while a document is
    short enough to fit one slice — which is why the fixtures here are short, and
    why this test would have kept passing after the change while describing the old
    contract. The windowing behaviour is covered in `test_reranker_parent_scoring.py`.
    """
    recorder = _RecordingCrossEncoder()
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: recorder)
    docs = _docs(2)

    asyncio.run(rr.rerank("the query", docs, top_n=1))

    pairs = recorder.calls[0]["pairs"]
    assert [p[0] for p in pairs] == ["the query", "the query"]
    # One slice each, because these fixtures fit the scoring window.
    assert [p[1] for p in pairs] == [rr.parent_context(d) for d in docs]


def test_a_document_longer_than_the_window_is_still_scored(monkeypatch):
    """
    Truncation must degrade the score, not the pipeline. A passage far longer than
    the window still gets a score and still appears in the ranking.

    Two documents, `top_n=1`, on purpose: `rerank()` returns early when
    `len(documents) <= top_n`, so a single document would never reach `predict()`
    and this would pass without scoring anything.
    """
    recorder = _RecordingCrossEncoder()
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: recorder)
    huge = Document(
        page_content="x = 1\n" * 5000,
        metadata={"source": "/x/huge.py", "chunk_index": 0},
    )

    ranked = asyncio.run(rr.rerank("q", [huge, *_docs(1)], top_n=1))

    assert recorder.calls, "predict() was never called, so nothing was scored"
    assert len(ranked) == 1
    assert rr.RERANK_SCORE_KEY in ranked[0].metadata


def test_the_failure_path_still_returns_documents_unranked(monkeypatch):
    """
    Unchanged behaviour, pinned here because this file is the one that would notice
    if the explicit window broke the fallback.

    Three documents with `top_n=2` so that `len(documents) <= top_n` does not skip
    reranking first. With two, the early return would satisfy every assertion below
    without the failure path ever running — this test previously did exactly that.
    """
    def _boom():
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(rr, "_get_cross_encoder", _boom)
    monkeypatch.setattr(rr, "_reranker_failure_count", 0)
    docs = _docs(3)

    ranked = asyncio.run(rr.rerank("q", docs, top_n=2))

    assert ranked == docs[:2]
    assert all(rr.RERANK_SCORE_KEY not in d.metadata for d in ranked)


@pytest.mark.parametrize("top_n", [2, 5])
def test_reranking_is_skipped_when_there_is_nothing_to_order(monkeypatch, top_n):
    """
    Pin the early return that made two tests in this file vacuous.

    With `len(documents) <= top_n` there is no ordering to improve, so `rerank()`
    returns the list untouched and never loads the model. Worth pinning rather than
    leaving implicit, because the consequence is subtle: no `RERANK_SCORE_KEY` is
    written, and callers are documented to read a missing key as "unscored" rather
    than zero. A caller that read it as zero would be reading a skip as a bad score.

    Note the boundary is `<=`, not `<`: two documents with `top_n=1` DOES rerank,
    which is the case below.
    """
    recorder = _RecordingCrossEncoder()
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: recorder)
    docs = _docs(2)

    ranked = asyncio.run(rr.rerank("q", docs, top_n=top_n))

    assert ranked == docs, "the skip path must return the documents untouched"
    assert not recorder.calls, "the model was loaded when there was nothing to order"


def test_reranking_runs_on_the_other_side_of_that_boundary(monkeypatch):
    """
    The complement, so the condition is pinned from both sides rather than assumed.

    Two documents with `top_n=1`: `len(documents) > top_n`, so scoring happens and
    the list is cut to one.
    """
    recorder = _RecordingCrossEncoder()
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: recorder)

    ranked = asyncio.run(rr.rerank("q", _docs(2), top_n=1))

    assert recorder.calls, "two documents and top_n=1 must be scored, not skipped"
    assert len(ranked) == 1
    assert rr.RERANK_SCORE_KEY in ranked[0].metadata
