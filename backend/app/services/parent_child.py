"""
parent_child.py — Search with small units, read with large ones.

THE PROBLEM
-----------
A retrieval unit has to satisfy two requirements that pull in opposite directions.

**Search wants it small.** The embedding model reads 256 tokens and silently
discards the rest, and the cross-encoder reads 512 tokens shared with the query.
Anything longer than those windows is partly invisible to the very components that
rank it. Measured on this repo's own corpus: at the chunker's 3000-character
ceiling, **71% of a chunk's tokens never reach the embedder.**

**Generation wants it large.** The LLM answering "how does token verification
work?" needs the whole function, not its first forty lines. Handing it a fragment
produces an answer about a fragment.

One number cannot serve both, which is why `MAX_CHUNK_CHARS = 3000` was wrong for
both: too long for the models that rank it, and no larger than the models that read
it need.

THE FIX
-------
Keep the chunker's chunk as the **parent** — the unit the LLM reads and the unit
citations point at. Split it into window-sized **children**, and index those as the
rows the retriever searches.

The tail of a parent is no longer invisible, because it is the *body* of a
different child, with its own vector. Coverage comes from the union of children
rather than from any single vector being long enough — which is the only way to
cover text longer than the model's window without changing the model.

WHAT MAKES THIS CHEAP TO WIRE
-----------------------------
Children inherit their parent's `chunk_index`. Both fusion functions key documents
as `{source}::{chunk_index}`, so siblings collapse to one entry per parent and their
RRF scores *accumulate*. A parent whose first and last child both rank highly is
lifted above a parent with one lucky child — parent-level fusion, with no new code.

The cost is storage: each child's metadata carries the parent's full text, so a
parent split three ways stores its text four times. The alternative is a second
lookup at query time to resolve a parent id, which adds a failure mode (orphaned
children) to save disk on a corpus that is already small. Recorded here so the
trade is visible rather than accidental.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Iterable

from langchain_core.documents import Document

from app.services.ast_chunker import MAX_CHUNK_CHARS

# ~256 tokens at 3.5 characters per token, matching the default embedder
# (all-MiniLM-L6-v2). A test asserts this stays within that window; changing the
# embedder should change this number in the same commit.
EMBED_WINDOW_CHARS = 900

# Consecutive children overlap slightly so a fact lying across a boundary is wholly
# inside at least one child rather than half-visible in two.
CHILD_OVERLAP_CHARS = 90

# Metadata keys. Namespaced under `pc_` so they cannot collide with the chunker's
# own keys (symbol_name, start_line, line_ranges, ...).
PARENT_TEXT = "pc_parent_text"
PARENT_ID = "pc_parent_id"
CHILD_INDEX = "pc_child_index"
CHILD_COUNT = "pc_child_count"
PARENT_START_LINE = "pc_parent_start_line"
PARENT_END_LINE = "pc_parent_end_line"
CHILD_START = "pc_child_start"
CHILD_END = "pc_child_end"


def parent_id_of(metadata: dict[str, Any]) -> str:
    """
    Stable identity of the context unit a document belongs to.

    Deliberately the same string both fusion functions already use as a document
    key, so "these two rows are the same parent" means the same thing everywhere
    instead of being recomputed three ways.
    """
    explicit = metadata.get(PARENT_ID)
    if explicit:
        return str(explicit)
    return f"{metadata.get('source', '')}::{metadata.get('chunk_index', 0)}"


def window_spans(
    text: str,
    window_chars: int = EMBED_WINDOW_CHARS,
    overlap_chars: int = CHILD_OVERLAP_CHARS,
) -> list[tuple[int, int]]:
    """
    The (start, end) spans that cover `text`, in order. The ONE definition of this.

    WHY IT IS SHARED
    ----------------
    Ingestion slices a chunk to make embed windows; the reranker slices a chunk to
    score it without truncation. Different reasons, same arithmetic — and two copies
    of it drift, so the reranker would eventually cut on a boundary the indexer does
    not and score spans that were never indexed. One function, so "how this text is
    cut up" means one thing.

    A span is kept only if it reaches further than the span before it. A trailing
    window can start so late that it adds no text the previous one did not already
    cover; that would be a near-empty slice whose vector is noise and whose score is
    noise, while still costing a row or a prediction.
    """
    if window_chars < 1:
        raise ValueError(f"window_chars must be >= 1, got {window_chars}")
    if overlap_chars < 0 or overlap_chars >= window_chars:
        raise ValueError(
            f"overlap_chars must be >= 0 and < window_chars ({window_chars}), "
            f"got {overlap_chars}. An overlap at or above the window never advances "
            "and would emit windows forever."
        )
    if not text:
        return []
    if len(text) <= window_chars:
        return [(0, len(text))]

    step = window_chars - overlap_chars
    spans = [
        (start, min(start + window_chars, len(text)))
        for start in range(0, len(text), step)
    ]
    return [
        span for index, span in enumerate(spans)
        if index == 0 or span[1] > spans[index - 1][1]
    ]


def children_of(
    chunk: Document,
    window_chars: int = EMBED_WINDOW_CHARS,
    overlap_chars: int = CHILD_OVERLAP_CHARS,
) -> list[Document]:
    """
    Split one context chunk into the window-sized rows that get embedded.

    A chunk that already fits returns one child that is a copy of it, so every
    document in the index has the same shape. Uniformity matters more than saving a
    branch here: a pipeline with "sometimes a child, sometimes a parent" in it is a
    pipeline where `parent_context` is eventually forgotten on one path.

    The input is never mutated. Chunker output is cached and shared between
    requests, and writing parent bookkeeping onto a cached Document would leak one
    indexing run's metadata into another's.

    Validation lives in `window_spans`, which is where the windowing is defined —
    one place to change, one place to get wrong.
    """
    text = chunk.page_content
    source_meta = dict(chunk.metadata)
    pid = parent_id_of(source_meta)

    # Captured before any child is created, so every child agrees about the parent
    # even when the parent is itself a window of a larger function.
    parent_bookkeeping = {
        PARENT_ID: pid,
        PARENT_TEXT: text,
        PARENT_START_LINE: source_meta.get("start_line"),
        PARENT_END_LINE: source_meta.get("end_line"),
        # chunk_index is inherited deliberately: see the module docstring. Fusion
        # keys on it, so siblings accumulate onto one score.
        "chunk_index": source_meta.get("chunk_index", 0),
    }

    # Sliced by the shared definition, so the reranker cutting the same chunk for
    # scoring cannot land on different boundaries than ingestion indexed.
    windows = window_spans(text, window_chars, overlap_chars)

    children: list[Document] = []
    for index, (start, end) in enumerate(windows):
        metadata = {
            **source_meta,
            **parent_bookkeeping,
            CHILD_INDEX: index,
            CHILD_COUNT: len(windows),
            CHILD_START: start,
            CHILD_END: end,
        }
        children.append(Document(page_content=text[start:end], metadata=metadata))

    return children


def children_of_all(
    chunks: Iterable[Document],
    window_chars: int = EMBED_WINDOW_CHARS,
    overlap_chars: int = CHILD_OVERLAP_CHARS,
) -> list[Document]:
    """Flat-map `children_of` over a batch of chunks, preserving order."""
    out: list[Document] = []
    for chunk in chunks:
        out.extend(children_of(chunk, window_chars, overlap_chars))
    return out


def max_windows_per_parent(
    chunk_chars: int = MAX_CHUNK_CHARS,
    window_chars: int = EMBED_WINDOW_CHARS,
    overlap_chars: int = CHILD_OVERLAP_CHARS,
) -> int:
    """
    The most windows `children_of` can emit for one chunk.

    WHY THIS IS A BOUND AND NOT A GUESS
    -----------------------------------
    Indexed rows are windows, so any caller taking "the top N rows" is now taking
    the top N rows of something that is not one-per-chunk: N rows can cover as few
    as N/this distinct chunks. Retrieval used to ask for `CANDIDATE_COUNT` rows and
    get `CANDIDATE_COUNT` chunks. It kept asking for the same number and silently
    started getting fewer — the candidate pool thinned, with no error and no metric
    that would show it.

    The way back is not to pick a fudge factor. `children_of` walks the text in
    steps of `window_chars - overlap_chars`, so the number of spans it can produce
    is `ceil(len(text) / step)`, and it may only drop spans, never add them. The
    longest text it can be handed is `MAX_CHUNK_CHARS` — the AST chunker's ceiling,
    and also above the generic splitter's `chunk_size`, so it bounds both paths.
    That makes this an upper bound rather than an estimate, which is what lets the
    caller multiply instead of hope.

    At the current constants: window 900, overlap 90, step 810, ceiling 3000 →
    4 windows. `test_max_windows_per_parent_bounds_every_length` checks this against
    every possible length rather than the endpoints, because the span count is a
    ceiling and the interesting cases are the ones that just cross a boundary.

    It takes its arguments as parameters so the tests can vary them, and so a future
    change to the windowing maths has one place to update instead of a scattered
    multiplier.
    """
    if window_chars < 1:
        raise ValueError(f"window_chars must be >= 1, got {window_chars}")
    step = window_chars - overlap_chars
    if step < 1:
        raise ValueError(
            f"overlap_chars ({overlap_chars}) must be < window_chars ({window_chars}); "
            "a step of zero or less never advances and would emit windows forever."
        )
    if chunk_chars < 1:
        raise ValueError(f"chunk_chars must be >= 1, got {chunk_chars}")
    return max(1, ceil(chunk_chars / step))


def parent_context(doc: Document) -> str:
    """
    The text the LLM should read for this hit: the whole parent, not the window.

    Falls back to the document's own content, which is correct for any document
    that did not come through `children_of` — an older index, or a caller that
    indexed whole chunks.
    """
    text = doc.metadata.get(PARENT_TEXT)
    return text if isinstance(text, str) and text else doc.page_content


def dedupe_to_parents(docs: Iterable[Document]) -> list[Document]:
    """
    Keep the first (best-ranked) view of each parent, discarding its other children.

    Fusion already collapses siblings, because they share `chunk_index`. This is a
    second line of defence at the one point where it costs real money: the context
    window. Three children of one function take three slots and three copies of the
    same text, which is budget not spent on three *other* functions.
    """
    seen: set[str] = set()
    out: list[Document] = []
    for doc in docs:
        pid = parent_id_of(doc.metadata)
        if pid in seen:
            continue
        seen.add(pid)
        out.append(doc)
    return out


def expand_to_context(docs: Iterable[Document]) -> list[Document]:
    """
    Turn retrieved children into the documents the LLM reads.

    Dedupe first, then widen each survivor to its parent. Widen rather than replace:
    `page_content` becomes the parent text, but metadata keeps the child's own
    offsets, so a citation can highlight the precise region that matched while the
    model reads the whole function.
    """
    expanded: list[Document] = []
    for doc in dedupe_to_parents(docs):
        text = parent_context(doc)
        if text == doc.page_content:
            expanded.append(doc)
            continue
        expanded.append(Document(page_content=text, metadata=dict(doc.metadata)))
    return expanded
