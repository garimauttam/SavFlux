"""
citation_service.py — Turns retrieved chunks into line-precise, trust-scored citations.

WHAT A CITATION HAS TO EARN
The Trust Ledger already proves *which file* an answer came from. A file is not
evidence: `retrieval_service.py` is 766 lines, and "the answer is somewhere in
there" is not something a reviewer can check. A citation becomes evidence when it
says `retrieval_service.py:612-640` and the reader can jump straight to those
lines and see the claim for themselves.

THREE THINGS THIS MODULE PRODUCES

1. `start_line` / `end_line` — the exact span, taken from chunk metadata written
   at ingest time (AST spans for Python, offset-derived spans for everything
   else). Free: no LLM call, no extra retrieval pass.

2. `trust_level` / `trust_score` — derived from the cross-encoder score the
   reranker already computed. The level is *evidence about the retrieval*, not a
   guess. A citation the cross-encoder scored 8.4 and one it scored -2.1 are
   both "top 5 results", and the UI must not present them as equally reliable.

3. `symbol_name` — the enclosing function/class, so a citation reads
   `auth.py:42-58 · verify_token` rather than a bare line range.

WHY A SEPARATE MODULE
`stream_answer` is already a long function. Citation assembly is pure
(chunks in → dicts out), so putting it here makes it directly unit-testable
without mocking ChromaDB, an LLM, or an event loop.
"""

from __future__ import annotations

from typing import Any, Iterable

from langchain_core.documents import Document

from app.services.reranker import RERANK_SCORE_KEY

# ── Trust thresholds ─────────────────────────────────────────────────────────
# Calibrated against ms-marco-MiniLM-L-6-v2, whose raw logits for code Q&A
# typically land in [-11, +11]. These are deliberately conservative: a citation
# is only called "high" when the cross-encoder is clearly confident, because the
# cost of over-trusting a citation (a reviewer believing an unverified claim) is
# far higher than the cost of under-trusting one (they open the file and check).
HIGH_TRUST_SCORE = 2.0
MEDIUM_TRUST_SCORE = -3.0


def trust_from_score(score: float | None) -> tuple[str, float | None]:
    """
    Map a cross-encoder score to a (level, rounded_score) pair.

    `None` — reranking was skipped (too few candidates) or unavailable. We report
    "unrated" rather than defaulting to "low": the chunk was never judged, and
    silently labelling unjudged evidence as low-quality is its own kind of lie.
    """
    if score is None:
        return "unrated", None
    if score >= HIGH_TRUST_SCORE:
        return "high", round(score, 2)
    if score >= MEDIUM_TRUST_SCORE:
        return "medium", round(score, 2)
    return "low", round(score, 2)


def _merge_span(
    existing: dict[str, Any], start: int | None, end: int | None
) -> None:
    """
    Widen a citation's span to cover an additional chunk from the same file.

    When two chunks of one file are both cited, the reader needs a window that
    contains both, not whichever happened to be processed first.
    """
    if start is None or end is None:
        return
    current_start = existing.get("start_line")
    current_end = existing.get("end_line")
    existing["start_line"] = start if current_start is None else min(current_start, start)
    existing["end_line"] = end if current_end is None else max(current_end, end)


def build_citations(documents: Iterable[Document]) -> list[dict[str, Any]]:
    """
    Build the `__SOURCES__` payload from the documents used to answer.

    One entry per source file, in the order the files were first cited (which is
    relevance order, because `documents` arrives reranked). Multiple chunks from
    the same file merge into one entry whose span covers all of them and whose
    trust reflects its best-scoring chunk.

    Every value is JSON-serialisable — this dict is streamed straight to the UI.
    """
    citations: list[dict[str, Any]] = []
    by_source: dict[str, dict[str, Any]] = {}

    for doc in documents:
        metadata = doc.metadata or {}
        source = metadata.get("source", "")
        if not source:
            continue

        raw_score = metadata.get(RERANK_SCORE_KEY)
        score = float(raw_score) if isinstance(raw_score, (int, float)) else None
        start_line = metadata.get("start_line")
        end_line = metadata.get("end_line")
        start_line = int(start_line) if isinstance(start_line, int) else None
        end_line = int(end_line) if isinstance(end_line, int) else None

        existing = by_source.get(source)
        if existing is None:
            level, rounded = trust_from_score(score)
            entry: dict[str, Any] = {
                "file_name": metadata.get("file_name", ""),
                "source": source,
                "language": metadata.get("language", ""),
                "trust_level": level,
                "trust_score": rounded,
                "chunk_count": 1,
            }
            if start_line is not None and end_line is not None:
                entry["start_line"] = start_line
                entry["end_line"] = end_line
            # Discontinuous evidence (a module chunk gathers imports at the top
            # plus constants between functions). Without this the UI would
            # highlight the whole hull — 150 lines to point at 20.
            if metadata.get("line_ranges"):
                entry["line_ranges"] = metadata["line_ranges"]
            if metadata.get("symbol_name"):
                entry["symbol_name"] = metadata["symbol_name"]
            if isinstance(metadata.get("chunk_index"), int):
                entry["chunk_index"] = metadata["chunk_index"]
            by_source[source] = entry
            citations.append(entry)
            continue

        # Same file cited again — widen the span and keep the strongest evidence.
        existing["chunk_count"] = existing.get("chunk_count", 1) + 1
        _merge_span(existing, start_line, end_line)
        # Union the precise ranges so a merged citation still highlights only
        # the lines that were actually retrieved. A chunk with no detailed
        # ranges contributes its plain span.
        incoming = metadata.get("line_ranges") or (
            f"{start_line}-{end_line}" if start_line is not None and end_line is not None else ""
        )
        if incoming:
            merged = existing.get("line_ranges")
            existing["line_ranges"] = f"{merged},{incoming}" if merged else incoming
        if score is not None:
            previous = existing.get("trust_score")
            if previous is None or score > previous:
                level, rounded = trust_from_score(score)
                existing["trust_level"] = level
                existing["trust_score"] = rounded
                # The symbol should name the best-scoring chunk, not the first.
                if metadata.get("symbol_name"):
                    existing["symbol_name"] = metadata["symbol_name"]

    return citations


def format_citation_label(citation: dict[str, Any]) -> str:
    """
    Render a citation as `auth.py:42-58` (or `auth.py` when no span is known).

    Used in the LLM prompt so the model cites the same spans the UI displays,
    instead of inventing line numbers — the grounding rules forbid citing lines
    the model has not seen, and this is how it sees them.
    """
    name = citation.get("file_name") or citation.get("source", "?")
    start = citation.get("start_line")
    end = citation.get("end_line")
    if not isinstance(start, int):
        return str(name)
    if isinstance(end, int) and end != start:
        return f"{name}:{start}-{end}"
    return f"{name}:{start}"
