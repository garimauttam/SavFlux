"""
reranker.py — Cross-encoder re-ranking of retrieved chunks.

WHY TWO-STAGE RETRIEVAL?
Stage 1 (embedding search): Fast. Embeds query once → cosine similarity over all vectors.
  Problem: embeddings are "compressed summaries" — they lose nuance.
  A chunk about "password validation" and one about "token validation" look similar
  in embedding space even if only one answers your question.

Stage 2 (cross-encoder re-ranking): Accurate. Takes the question AND each chunk together
  as a single input, producing a relevance score. It reads both simultaneously,
  so it understands the relationship between them — much more precise.
  Problem: slow. Can't do this over 100,000 chunks. But over 15 chunks? ~50ms. Fine.

The pipeline is: fetch 15 candidates (MMR) → re-rank → keep top 5.
This gives you the speed of embedding search with the accuracy of cross-encoders.

INTERVIEW TALKING POINT:
"I implemented a two-stage retrieval pipeline: first MMR for diversity, then a
local cross-encoder (ms-marco-MiniLM) for precision. This improved answer quality
noticeably on ambiguous questions without adding any API cost."
"""

import asyncio
import logging
from functools import lru_cache
from langchain_core.documents import Document

logger = logging.getLogger(__name__)
# _reranker_unavailable is NOT a module-level permanent flag.
# Per-call exception handling falls back to unranked docs for that request only;
# the model is retried on the next request.  A permanent flag would silently
# disable reranking for the server lifetime after a single transient failure
# (e.g. one bad CI test run).
_reranker_failure_count = 0
_RERANKER_MAX_FAILURES = 5   # disable after 5 consecutive failures (real breakage)

# We import lazily inside the function to avoid loading the 80MB model
# at import time (which would slow down every cold start, even for requests
# that don't need re-ranking)


@lru_cache(maxsize=1)
def _get_cross_encoder():
    """
    Load the cross-encoder model once and cache it.
    lru_cache(maxsize=1) = singleton pattern for the model.
    The model is loaded from HuggingFace Hub on first call, then cached on disk.

    ms-marco-MiniLM-L-6-v2:
    - Trained on MS MARCO (Microsoft MAchine Reading COmprehension) — a massive
      passage-retrieval dataset. Perfect for code Q&A.
    - 6 transformer layers → fast enough for real-time re-ranking
    - ~80MB — small enough to include in a Docker image
    """
    from sentence_transformers import CrossEncoder
    return CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")


RERANK_SCORE_KEY = "rerank_score"
"""
Metadata key holding the cross-encoder relevance score for a chunk.

Written onto `Document.metadata` by `rerank()` so downstream consumers (the
trust ledger, diagnostics) can reason about *how* relevant a chunk was rather
than just its ordinal position. Absent when reranking was skipped or failed —
callers must treat a missing key as "unscored", never as zero.
"""


async def rerank(
    query: str,
    documents: list[Document],
    top_n: int = 5,
) -> list[Document]:
    """
    Re-rank a list of documents by relevance to the query.
    Returns the top_n most relevant documents, sorted by score descending.

    Each returned document carries its cross-encoder score under
    `metadata[RERANK_SCORE_KEY]`. The score is the evidence behind a citation's
    trust level: a chunk that merely placed first in a weak field is not the
    same as one the cross-encoder scored highly, and the UI should not present
    them identically.

    We run it in a thread because CrossEncoder.predict() is synchronous CPU work.
    Running it directly in the async event loop would block all other requests.
    asyncio.to_thread() moves it to a thread pool — event loop stays free.
    """
    global _reranker_failure_count
    if len(documents) <= top_n or _reranker_failure_count >= _RERANKER_MAX_FAILURES:
        # Not enough docs to bother re-ranking — return as-is.
        # Also skip if the cross-encoder has failed repeatedly (real breakage).
        return documents

    def _score():
        cross_encoder = _get_cross_encoder()
        # CrossEncoder expects a list of (query, passage) pairs
        pairs = [(query, doc.page_content) for doc in documents]
        scores = cross_encoder.predict(pairs)
        # Zip scores with docs, sort by score descending, return top_n docs
        scored = sorted(zip(scores, documents), key=lambda x: x[0], reverse=True)
        ranked: list[Document] = []
        for score, doc in scored[:top_n]:
            # Copy before mutating: these Documents come from a module-level BM25
            # cache shared across requests, so writing a per-query score onto them
            # in place would leak one user's relevance scores into another's results.
            ranked.append(Document(
                page_content=doc.page_content,
                metadata={**doc.metadata, RERANK_SCORE_KEY: float(score)},
            ))
        return ranked

    try:
        result = await asyncio.to_thread(_score)
        _reranker_failure_count = 0  # reset on success
        return result
    except Exception as exc:
        # Retrieval must remain usable when optional ML dependencies cannot load
        # (e.g. TensorFlow/Transformers binary mismatch in CI).
        # Count consecutive failures; only give up after _RERANKER_MAX_FAILURES.
        _reranker_failure_count += 1
        if _reranker_failure_count >= _RERANKER_MAX_FAILURES:
            logger.warning(
                "Cross-encoder reranking permanently disabled after %d failures. "
                "Last error: %s",
                _reranker_failure_count, exc,
            )
        else:
            logger.warning(
                "Cross-encoder reranking failed (attempt %d/%d); using fused rank: %s",
                _reranker_failure_count, _RERANKER_MAX_FAILURES, exc,
            )
        return documents[:top_n]
