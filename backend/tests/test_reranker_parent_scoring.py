"""
The reranker must score a chunk the same way whichever branch found it.

THE DEFECT
----------
Dense candidates carry a window; BM25 candidates carry the whole chunk (that is what
`_bm25_corpus` builds), and `two_branch_rrf` gives dense precedence when both found a
chunk. `rerank()` read `doc.page_content`, so it compared scores computed on
~900-character windows against scores computed on up-to-3000-character parents with
everything past 512 tokens discarded.

Worse than the mix was which case a chunk fell into: 154 of 880 fused candidates over
the benchmark queries were found by both branches but arrived as a window, because
dense's top-N happened to include them. So a chunk's rank depended on where one
branch's truncation boundary fell — a property of the plumbing, not of relevance.

THE FIX, AND WHAT THESE TESTS PIN
---------------------------------
Score `parent_context(doc)` — the chunk, not the row — and slice a long parent rather
than truncating it. The tests below are ordered by how much they would catch: the
consistency property first, because that is the defect; then the tail payoff; then the
arithmetic.
"""

import asyncio
import inspect
import unittest.mock as mock

import pytest
from langchain_core.documents import Document

from app.services import reranker as rr
from app.services.ast_chunker import MAX_CHUNK_CHARS
from app.services.parent_child import (
    children_of,
    max_windows_per_parent,
    parent_context,
    window_spans,
)


@pytest.fixture(autouse=True)
def _never_load_the_real_model(monkeypatch):
    """
    Every test here scores through a double, by default.

    `conftest.py` patches `_get_cross_encoder` only inside the `client` fixture, so a
    test that calls `rerank()` without patching gets the real one — which loads weights
    from huggingface.co with retries. The first version of this file did exactly that
    in one test and took 30 seconds while attempting a download, and would have failed
    outright on a machine with no network.
    """
    monkeypatch.setattr(rr, "_get_cross_encoder", lambda: _RecordingEncoder())


class _RecordingEncoder:
    """Scores by a callback over the passage text, and records every call."""

    def __init__(self, scorer=None):
        self.calls = []
        self._scorer = scorer or (lambda passage: 0.0)

    def predict(self, pairs, **kwargs):
        self.calls.append({"pairs": list(pairs), "kwargs": kwargs})
        return [float(self._scorer(p[1])) for p in pairs]


def _long_chunk(head: str = "", tail: str = "", filler_reps: int = 45) -> Document:
    filler = "def helper(value):\n    return value + 1\n" * filler_reps
    return Document(
        page_content=head + filler + tail,
        metadata={"source": "repo::big.py", "chunk_index": 0, "repo_url": "repo"},
    )


def _short_chunk(text: str = "def tiny():\n    return 1\n") -> Document:
    return Document(
        page_content=text,
        metadata={"source": "repo::small.py", "chunk_index": 1, "repo_url": "repo"},
    )


def _window_row(parent: Document, index: int = -1) -> Document:
    """The row the dense branch actually stores: one window, with the parent in metadata."""
    child = children_of(parent)[index]
    return Document(page_content=child.page_content, metadata=dict(child.metadata))


def _score_of(doc: Document, query: str = "q", scorer=None) -> float:
    """
    Run the real rerank() over one document and read back its score.

    THREE documents with top_n=2, deliberately: `rerank()` returns early when
    `len(documents) <= top_n`, so two documents with top_n=2 would score nothing and
    this helper would fail on a missing key rather than on the behaviour under test.
    The probe marker identifies the document after `rerank()` has copied it.
    """
    probe = "target"
    target = Document(page_content=doc.page_content, metadata={**doc.metadata, "_probe": probe})
    controls = [
        _short_chunk("def control_a():\n    return 'a'\n"),
        _short_chunk("def control_b():\n    return 'b'\n"),
    ]
    encoder = _RecordingEncoder(scorer)

    with mock.patch.object(rr, "_get_cross_encoder", return_value=encoder):
        ranked = asyncio.run(rr.rerank(query, [target, *controls], top_n=2))

    for out in ranked:
        if out.metadata.get("_probe") == probe:
            return out.metadata[rr.RERANK_SCORE_KEY]
    raise AssertionError(
        f"the probed document did not reach the top 2: {[d.metadata for d in ranked]}"
    )


# ── The defect, stated directly ───────────────────────────────────────────────

def test_a_chunk_scores_the_same_whether_it_arrives_as_a_window_or_a_parent():
    """
    THE test. The same chunk, supplied two ways, must score identically.

    The scorer is deliberate about it: it rewards a marker that sits in the chunk's
    TAIL. Before this change the window row carried the full parent in metadata but
    only the window in `page_content`, so scoring that row read the window — and for
    a chunk whose marker is past the window, the two inputs disagreed.
    """
    parent = _long_chunk(tail="\nMARKER_TAIL_RELEVANT\n")

    as_row = _window_row(parent, 0)          # dense branch's shape
    as_parent = Document(page_content=parent.page_content, metadata=dict(parent.metadata))

    # Guard: the two rows must genuinely differ, or the assertion is vacuous.
    assert as_row.page_content != as_parent.page_content
    assert "MARKER_TAIL" not in as_row.page_content
    assert "MARKER_TAIL" in parent_context(as_row), "metadata no longer carries the parent"

    scorer = lambda passage: 1.0 if "MARKER_TAIL_RELEVANT" in passage else 0.0

    assert _score_of(as_row, scorer=scorer) == _score_of(as_parent, scorer=scorer)


def test_the_score_does_not_depend_on_the_content_of_the_row_that_arrived():
    """
    Stronger form of the same property: two rows of DIFFERENT chunks sharing a
    parent identity score the same, because both resolve to the same parent.

    This is what "source-agnostic" means in practice — a candidate's score is a
    function of its chunk, not of which window the retriever happened to surface.
    """
    parent = _long_chunk(tail="\nMARKER_TAIL_RELEVANT\n")
    children = children_of(parent)
    assert len(children) > 1, "fixture must split, or there is only one row to compare"

    scorer = lambda passage: 1.0 if "MARKER_TAIL_RELEVANT" in passage else 0.0
    scores = {
        _score_of(Document(page_content=c.page_content, metadata=dict(c.metadata)), scorer=scorer)
        for c in children
    }

    assert len(scores) == 1, f"sibling windows scored differently: {scores}"


def test_a_fact_past_the_scoring_window_is_not_lost():
    """
    The payoff, with the counterfactual measured rather than described.

    A chunk whose only relevant text is in its tail used to score on its head alone,
    so it ranked as if the tail did not exist. Both numbers are computed here.
    """
    parent = _long_chunk(tail="\nMARKER_TAIL_RELEVANT\n")
    text = parent.page_content
    limit = rr.RERANK_WINDOW_CHARS

    # What truncation would have seen: only the first window's worth.
    head_only = text[:limit]
    assert "MARKER_TAIL" not in head_only, "fixture must put the fact past the window"

    scored_passages = []

    def scorer(passage):
        scored_passages.append(passage)
        return 1.0 if "MARKER_TAIL_RELEVANT" in passage else 0.0

    assert _score_of(
        Document(page_content=text, metadata=dict(parent.metadata)), scorer=scorer
    ) == 1.0
    assert any("MARKER_TAIL_RELEVANT" in p for p in scored_passages), (
        "no slice contained the tail fact, yet the score was 1.0"
    )


# ── The arithmetic ────────────────────────────────────────────────────────────

def test_the_score_is_the_max_over_slices():
    """
    Max, not mean and not sum.

    Mean would punish a long chunk for containing one irrelevant section; sum would
    reward length outright. A passage is as relevant as its most relevant part.
    """
    parent = _long_chunk(tail="\nMARKER_TAIL_RELEVANT\n")
    doc = Document(page_content=parent.page_content, metadata=dict(parent.metadata))

    def scorer(passage):
        return 7.0 if "MARKER_TAIL_RELEVANT" in passage else 0.25

    assert _score_of(doc, scorer=scorer) == 7.0


def test_a_chunk_that_fits_the_window_is_scored_exactly_once():
    """
    No behaviour change for the common case: one slice, one pair, same text.

    Most chunks are shorter than the scoring window, so this is what almost every
    candidate sees, and it must be byte-identical to what the reranker did before.
    """
    doc = _short_chunk()
    encoder = _RecordingEncoder()
    with mock.patch.object(rr, "_get_cross_encoder", return_value=encoder):
        asyncio.run(rr.rerank("q", [doc, _short_chunk("def other():\n    return 2\n")], top_n=1))

    assert len(encoder.calls) == 1, "one predict call, not one per document"
    pairs = encoder.calls[0]["pairs"]
    assert len(pairs) == 2, "one pair per document for short documents"
    assert [p[1] for p in pairs] == [
        "def tiny():\n    return 1\n",
        "def other():\n    return 2\n",
    ]


def test_every_slice_of_every_candidate_goes_into_one_predict_call():
    """
    Batching is the reason this is affordable. Per-document calls would repeat the
    tokenizer and model overhead once per candidate.
    """
    docs = [
        Document(page_content=_long_chunk(tail=f"\nMARKER_{i}\n").page_content,
                 metadata={"source": f"repo::b{i}.py", "chunk_index": i, "repo_url": "repo"})
        for i in range(3)
    ]
    encoder = _RecordingEncoder()

    with mock.patch.object(rr, "_get_cross_encoder", return_value=encoder):
        asyncio.run(rr.rerank("q", docs, top_n=1))

    assert len(encoder.calls) == 1, f"expected one batched call, got {len(encoder.calls)}"
    assert len(encoder.calls[0]["pairs"]) > 3, "long documents produced no extra slices"


def test_the_slice_count_per_chunk_never_exceeds_the_window_bound():
    """
    The cost is bounded by a constant, not by how long a chunk happens to be.

    A chunk can be at most MAX_CHUNK_CHARS, so its slice count is bounded — which is
    what makes the multiplier a fixed cost rather than a corpus-dependent blowup.
    """
    import unittest.mock as mock

    # Three documents and top_n=2 so reranking actually runs — with two documents
    # the `len(documents) <= top_n` early return would skip scoring entirely, and
    # `encoder.calls` would be empty rather than showing zero slices.
    controls = [
        _short_chunk("def control_a():\n    return 'a'\n"),
        _short_chunk("def control_b():\n    return 'b'\n"),
    ]
    bound = max_windows_per_parent(
        chunk_chars=MAX_CHUNK_CHARS,
        window_chars=rr.RERANK_WINDOW_CHARS,
        overlap_chars=rr.RERANK_WINDOW_OVERLAP_CHARS,
    )

    for length in (1, 500, 1792, 1793, 3000):
        doc = Document(
            page_content="y" * length,
            metadata={"source": "s", "chunk_index": 0},
        )
        encoder = _RecordingEncoder()
        with mock.patch.object(rr, "_get_cross_encoder", return_value=encoder):
            asyncio.run(rr.rerank("q", [doc, *controls], top_n=2))

        assert encoder.calls, f"no scoring happened for length {length}"
        slices = len(encoder.calls[0]["pairs"]) - len(controls)
        assert 1 <= slices <= bound, (
            f"a {length}-char chunk produced {slices} slices, outside 1..{bound}"
        )


def test_the_slice_budget_follows_from_the_token_window():
    """
    The constants must stay related. Someone raising RERANKER_MAX_LENGTH without the
    slice size following would silently reintroduce truncation on every slice.
    """
    assert rr.RERANK_WINDOW_CHARS == int(
        rr.RERANKER_MAX_LENGTH * rr.RERANK_CHARS_PER_TOKEN
    )
    assert rr.RERANK_WINDOW_OVERLAP_CHARS < rr.RERANK_WINDOW_CHARS
    # The ratio, not the number: parent_child windows embed text at 900/90.
    assert rr.RERANK_WINDOW_CHARS // rr.RERANK_WINDOW_OVERLAP_CHARS == 10


def test_the_reranker_slices_through_the_shared_function_with_the_shared_parameters(
    monkeypatch,
):
    """
    One definition of windowing, enforced at the call site.

    Asserting that the source contains "window_spans(" does not test this: it passes
    with the wrong parameters, and passes if the arithmetic is reimplemented inline
    and the name survives only in a comment. Neither would fail, and the drift this
    guards against — the reranker scoring spans that were never indexed — is exactly
    a silent one. So watch the call instead of reading the text.
    """
    import app.services.parent_child as pc

    calls: list[tuple] = []

    def spy(text, window_chars, overlap_chars=None):
        calls.append((text, window_chars, overlap_chars))
        return pc.window_spans(text, window_chars, overlap_chars)

    monkeypatch.setattr(rr, "window_spans", spy)

    parent = _long_chunk(tail="\nMARKER_TAIL_RELEVANT\n")
    doc = Document(page_content=parent.page_content, metadata=dict(parent.metadata))
    encoder = _RecordingEncoder()
    with mock.patch.object(rr, "_get_cross_encoder", return_value=encoder):
        asyncio.run(
            rr.rerank(
                "q",
                [doc, _short_chunk("def a():\n    return 1\n"), _short_chunk("def b():\n    return 2\n")],
                top_n=2,
            )
        )

    assert calls, "the reranker did not slice through window_spans at all"
    assert all(c[1] == rr.RERANK_WINDOW_CHARS for c in calls), (
        f"the reranker windowed at {[c[1] for c in calls]}, not RERANK_WINDOW_CHARS"
    )
    assert all(c[2] == rr.RERANK_WINDOW_OVERLAP_CHARS for c in calls), (
        f"the reranker overlapped at {[c[2] for c in calls]}, not "
        f"RERANK_WINDOW_OVERLAP_CHARS — its slices would not match indexed spans"
    )


def test_the_reranker_cuts_a_parent_exactly_where_the_ingester_has_it():
    """
    The boundaries, checked against the ingestion path's own bookkeeping rather than
    against a copy of the arithmetic written out here.

    An earlier version of this test restated the span maths — and got it wrong: it
    expected a trailing span that `window_spans` correctly drops for adding no new
    text. A test that reimplements the thing it is testing can only ever confirm it
    agrees with itself. `pc_child_start` / `pc_child_end` are what ingestion actually
    indexed, so they are the oracle.
    """
    for length in (1, 899, 900, 901, 1792, 1793, 3000, 6000):
        text = "z" * (length - 1) + "Q"
        parent = Document(page_content=text, metadata={"source": "s", "chunk_index": 0})

        indexed = [(c.metadata["pc_child_start"], c.metadata["pc_child_end"]) for c in children_of(parent)]
        assert indexed == window_spans(text, 900, 90), (
            f"children_of no longer follows window_spans at length {length}"
        )
        assert [text[a:b] for a, b in indexed] == [c.page_content for c in children_of(parent)]

        # The reranker's own parameters, sliced by the same shared function.
        spans = window_spans(text, rr.RERANK_WINDOW_CHARS, rr.RERANK_WINDOW_OVERLAP_CHARS)
        assert spans[0][0] == 0
        assert spans[-1][1] == len(text), "the tail must be reachable"


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_an_empty_document_still_gets_a_score():
    """
    A document with no text must not silently vanish from the ranking — a candidate
    dropped here would be invisible to every caller downstream.
    """
    empty = Document(page_content="", metadata={"source": "s", "chunk_index": 9})

    score = _score_of(empty, scorer=lambda passage: 0.5)

    assert score == 0.5


def test_the_unchanged_fallback_path_still_returns_documents_unranked(monkeypatch):
    """Unchanged behaviour, re-pinned because this file is where it would break."""
    def _boom():
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(rr, "_get_cross_encoder", _boom)
    monkeypatch.setattr(rr, "_reranker_failure_count", 0)
    docs = [_short_chunk(), _short_chunk("def b():\n    return 2\n"), _short_chunk("def c():\n    return 3\n")]

    ranked = asyncio.run(rr.rerank("q", docs, top_n=2))

    assert ranked == docs[:2]
    assert all(rr.RERANK_SCORE_KEY not in d.metadata for d in ranked)


def test_originals_are_still_not_mutated():
    """
    The BM25 cache is module-level and shared across requests, so a score written
    onto a cached Document in place would leak between users.
    """
    docs = [_short_chunk(), _short_chunk("def b():\n    return 2\n")]

    asyncio.run(rr.rerank("q", docs, top_n=1))

    assert all(rr.RERANK_SCORE_KEY not in d.metadata for d in docs)
