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

from app.services.parent_child import (
    parent_context,
    window_spans,
)

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

RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
"""
The cross-encoder whose raw logits `citation_service` calibrates against.

Named here, once, because two places need to agree on it and they used to differ:

  - `_get_cross_encoder()` builds it.
  - `citation_service.HIGH_TRUST_SCORE` / `MEDIUM_TRUST_SCORE` are thresholds *in
    this model's logit space*. Swapping the model without recalibrating them does
    not error — it silently relabels every citation's trust level, keeping the
    same confident wording while the numbers underneath it mean something else.

`eval_rag.py` reports this string with every benchmark run so a change to it is
visible in the results rather than inferred from a suspicious metric.
"""


RERANKER_MAX_LENGTH = 512
"""
The cross-encoder's input window, in tokens, including the query.

WHY THIS IS PASSED EXPLICITLY
-----------------------------
`CrossEncoder.predict()` truncates each (query, passage) pair to `max_length`,
defaulting to the model's `max_seq_length` when the argument is omitted. Leaving it
omitted means the truncation is real but invisible: no warning, no error, and a
library upgrade that changed the default would change ranking quality with nothing in
this repository to show for it.

This is the same defect class as the embedder's 256-token window, and it is the same
reasoning: the model was trained at this length, so the fix is to stop feeding it more
than it can read — not to widen the window. Measured on this project's own chunks,
8-14% exceed 512 tokens (the range is the chars-per-token estimate, not the
measurement), so for those the reranker scores only a prefix.

Unlike the embedder, the truncation here is survivable by design: the reranker only
*orders* candidates that retrieval already found, so a dropped tail costs ranking
precision on long chunks rather than making them unreachable. What it must not do is
be silent — hence the explicit constant, and a test that fails if `predict()` is
called without it.

Windowed scoring — splitting a long passage and taking the best window's score —
would recover that precision. It multiplies rerank cost by the window count, so it
wants its own measurement before landing rather than being bundled here.
"""


RERANK_CHARS_PER_TOKEN = 3.5
"""
Characters per token, used to size a scoring slice from a token budget.

An approximation, and labelled as one. The exact figure needs the cross-encoder's
tokenizer, which means loading the model — and this module deliberately does not do
that at import time (see the lazy import note below). Code tokenizes at roughly 3-4
characters per token, so the slice this produces may be a little over or under the
model's real window. That is the right side to err on: an oversized slice is trimmed
by `max_length` (a small, bounded loss on one slice), while an undersized one would
needlessly split a passage that would have fit.

`RERANK_WINDOW_CHARS` below is the only thing that consumes this, so there is one
place to correct if a real measurement replaces the estimate.
"""

RERANK_WINDOW_CHARS = int(RERANKER_MAX_LENGTH * RERANK_CHARS_PER_TOKEN)
"""The scoring slice size in characters: 512 tokens at the ratio above, so 1792."""

RERANK_WINDOW_OVERLAP_CHARS = RERANK_WINDOW_CHARS // 10
"""
Slice overlap, at the same 10% ratio `parent_child` uses for embed windows (900/90).

The ratio matters, not the number: a fact that straddles a slice boundary must be
wholly inside at least one slice, or the passage scores as if it were absent.
"""


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
    return CrossEncoder(RERANKER_MODEL)


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

        # Score the PARENT, not whatever row happened to arrive.
        #
        # Dense candidates carry a window; BM25 candidates carry the whole chunk.
        # Reading `page_content` therefore scored a chunk on ~900 characters if the
        # dense branch found it and on up to 3000 if it did not — so a chunk's rank
        # depended on which branch's top-N it happened to land in, which is a
        # property of the retrieval plumbing rather than of relevance. Reading
        # `parent_context` makes the score a function of the chunk alone.
        #
        # Long parents are sliced rather than truncated. Before, everything past 512
        # tokens was discarded, so a chunk whose relevance sat in its tail scored as
        # if the tail did not exist. Slicing is the same fix as the embedder's, in
        # the same shape, using the same span maths.
        pairs: list[tuple[str, str]] = []
        owners: list[int] = []
        for index, doc in enumerate(documents):
            text = parent_context(doc)
            spans = window_spans(
                text, RERANK_WINDOW_CHARS, RERANK_WINDOW_OVERLAP_CHARS
            )
            # An empty document yields no spans; score it rather than dropping it,
            # so every input keeps a score and none silently disappears from the
            # ranking.
            for start, end in spans or [(0, 0)]:
                pairs.append((query, text[start:end]))
                owners.append(index)

        if not pairs:
            return documents[:top_n]

        # ONE predict() call for every slice of every candidate. Per-document calls
        # would repeat the tokenizer and model overhead once per candidate; batching
        # keeps the cost proportional to the number of slices rather than to the
        # number of round trips.
        scores = cross_encoder.predict(pairs, max_length=RERANKER_MAX_LENGTH)

        # Max over a candidate's slices: a passage is as relevant as its most
        # relevant part, which is what a model reading the whole chunk could find.
        # Mean would punish a long chunk for having one irrelevant section and sum
        # would reward length outright.
        best: dict[int, float] = {}
        for owner, score in zip(owners, scores):
            value = float(score)
            if owner not in best or value > best[owner]:
                best[owner] = value

        scored = sorted(
            ((best[index], doc) for index, doc in enumerate(documents)),
            key=lambda pair: pair[0],
            reverse=True,
        )
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
