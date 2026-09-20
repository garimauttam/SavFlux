"""
test_citations.py — Line-precise, trust-scored citation assembly.

A citation is the product's trust claim: it tells a reviewer "this exact span
is the evidence". These tests pin the properties that make that claim honest:

  - spans survive the trip from chunk metadata to the __SOURCES__ payload
  - a file cited twice reports a span covering BOTH chunks, not just the first
  - trust levels come from real cross-encoder scores, and unscored chunks are
    reported as "unrated" rather than quietly downgraded to "low"
  - nothing is cited that the LLM was not actually shown
"""

import pytest
from langchain_core.documents import Document

from app.services.citation_service import (
    HIGH_TRUST_SCORE,
    MEDIUM_TRUST_SCORE,
    build_citations,
    format_citation_label,
    trust_from_score,
)
from app.services.reranker import RERANK_SCORE_KEY


def _doc(source, *, start=None, end=None, score=None, symbol=None, name=None, chunk=0):
    # Stable source ids look like "https://github.com/o/r::path/to/auth.py",
    # so the display name is the basename after both separators.
    meta = {
        "source": source,
        "file_name": name or source.split("::")[-1].split("/")[-1],
        "language": "py",
        "chunk_index": chunk,
    }
    if start is not None:
        meta["start_line"] = start
    if end is not None:
        meta["end_line"] = end
    if score is not None:
        meta[RERANK_SCORE_KEY] = score
    if symbol:
        meta["symbol_name"] = symbol
    return Document(page_content=f"code from {source}", metadata=meta)


# ── Trust levels ─────────────────────────────────────────────────────────────

def test_trust_levels_follow_cross_encoder_score():
    assert trust_from_score(HIGH_TRUST_SCORE + 1)[0] == "high"
    assert trust_from_score(HIGH_TRUST_SCORE)[0] == "high"
    assert trust_from_score(MEDIUM_TRUST_SCORE)[0] == "medium"
    assert trust_from_score(HIGH_TRUST_SCORE - 0.1)[0] == "medium"
    assert trust_from_score(MEDIUM_TRUST_SCORE - 0.1)[0] == "low"


def test_unscored_chunk_is_unrated_not_low():
    """
    A chunk the reranker never judged must not be labelled low-confidence.

    Reranking is skipped when there are fewer candidates than the requested
    top_n. Reporting those as "low" would tell the user the evidence was
    assessed and found weak, when in fact it was never assessed at all.
    """
    level, score = trust_from_score(None)
    assert level == "unrated"
    assert score is None

    citations = build_citations([_doc("a.py", start=1, end=9)])
    assert citations[0]["trust_level"] == "unrated"
    assert citations[0]["trust_score"] is None


# ── Span propagation ─────────────────────────────────────────────────────────

def test_span_reaches_the_sources_payload():
    citations = build_citations([
        _doc("repo::auth.py", start=42, end=58, score=5.0, symbol="verify_token"),
    ])
    assert len(citations) == 1
    entry = citations[0]
    assert entry["start_line"] == 42
    assert entry["end_line"] == 58
    assert entry["symbol_name"] == "verify_token"
    assert entry["trust_level"] == "high"
    assert format_citation_label(entry) == "auth.py:42-58"


def test_two_chunks_of_one_file_merge_into_a_covering_span():
    """A reader following the citation must land on a window containing both."""
    citations = build_citations([
        _doc("repo::auth.py", start=100, end=120, score=1.0, chunk=3),
        _doc("repo::auth.py", start=42, end=58, score=4.0, chunk=1),
    ])
    assert len(citations) == 1, "the same file must not be cited twice"
    entry = citations[0]
    assert entry["start_line"] == 42
    assert entry["end_line"] == 120
    assert entry["chunk_count"] == 2


def test_merged_citation_reports_its_best_evidence():
    """Trust and symbol must describe the strongest chunk, not the first seen."""
    citations = build_citations([
        _doc("repo::auth.py", start=100, end=120, score=-5.0, symbol="helper"),
        _doc("repo::auth.py", start=42, end=58, score=7.5, symbol="verify_token"),
    ])
    entry = citations[0]
    assert entry["trust_level"] == "high"
    assert entry["trust_score"] == 7.5
    assert entry["symbol_name"] == "verify_token"


def test_citation_order_follows_relevance():
    citations = build_citations([
        _doc("repo::first.py", start=1, end=5, score=9.0),
        _doc("repo::second.py", start=1, end=5, score=3.0),
        _doc("repo::third.py", start=1, end=5, score=1.0),
    ])
    assert [c["file_name"] for c in citations] == ["first.py", "second.py", "third.py"]


def test_chunks_without_spans_still_produce_a_citation():
    """Older indexes predate span metadata — degrade to a file-level citation."""
    citations = build_citations([_doc("repo::legacy.py", score=4.0)])
    entry = citations[0]
    assert "start_line" not in entry
    assert entry["trust_level"] == "high"
    assert format_citation_label(entry) == "legacy.py"


def test_single_line_span_renders_without_a_range():
    entry = build_citations([_doc("repo::x.py", start=7, end=7, score=3.0)])[0]
    assert format_citation_label(entry) == "x.py:7"


def test_documents_without_a_source_are_skipped():
    """A chunk with no source cannot be verified, so it must not be cited."""
    assert build_citations([Document(page_content="orphan", metadata={})]) == []


def test_payload_is_json_serialisable():
    """__SOURCES__ is streamed as JSON — numpy floats from the reranker break it."""
    import json

    citations = build_citations([
        _doc("repo::auth.py", start=42, end=58, score=5.0, symbol="verify_token"),
    ])
    assert json.loads(json.dumps(citations)) == citations


# ── Reranker contract ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rerank_attaches_scores_without_mutating_cached_documents():
    """
    The reranker must not write per-query scores onto the shared BM25 cache.

    Those Document objects are cached at module level and reused across
    requests; mutating them in place would leak one user's relevance scores
    into another user's citations.
    """
    from unittest.mock import patch

    from app.services import reranker as rr

    docs = [_doc(f"repo::f{i}.py", start=1, end=5) for i in range(6)]

    class _FakeEncoder:
        def predict(self, pairs):
            return [float(len(pairs) - i) for i in range(len(pairs))]

    with patch.object(rr, "_get_cross_encoder", return_value=_FakeEncoder()):
        ranked = await rr.rerank("query", docs, top_n=3)

    assert len(ranked) == 3
    assert all(RERANK_SCORE_KEY in d.metadata for d in ranked)
    # Scores descend — the top result is the most relevant.
    scores = [d.metadata[RERANK_SCORE_KEY] for d in ranked]
    assert scores == sorted(scores, reverse=True)
    # The originals are untouched.
    assert all(RERANK_SCORE_KEY not in d.metadata for d in docs)
