"""
test_parent_child.py — Small units to search with, large units to read.

The defect these tests exist for: the embedder reads 256 tokens and silently drops
the rest, while the chunker emits up to 3000 characters. Measured at the ceiling,
71% of a chunk's tokens never reach the embedder, so dense search cannot find a fact
near the end of a long function.

`test_a_tail_fact_becomes_reachable_once_it_is_a_child` is the payoff — the same
fixture that is unfindable as a whole chunk becomes findable as a child. Its
counterpart in `test_offline_embedder.py` shows the unfindable case, and the two
together are the before and after.

No network, no weights, no model download.
"""

from __future__ import annotations

import pytest
from langchain_core.documents import Document

from app.services.offline_embedder import OfflineEmbedder, DenseIndex, rank_by_dense
from app.services.parent_child import (
    CHILD_COUNT,
    CHILD_INDEX,
    EMBED_WINDOW_CHARS,
    PARENT_END_LINE,
    PARENT_ID,
    PARENT_START_LINE,
    PARENT_TEXT,
    children_of,
    children_of_all,
    dedupe_to_parents,
    expand_to_context,
    parent_context,
    parent_id_of,
)


def _chunk(text: str, **meta) -> Document:
    base = {
        "source": "/repo/app.py",
        "file_name": "app.py",
        "chunk_index": 3,
        "symbol_name": "verify",
        "symbol_type": "function",
        "start_line": 10,
        "end_line": 60,
    }
    base.update(meta)
    return Document(page_content=text, metadata=base)


def _long_text(token_count: int = 300) -> str:
    return "\n".join(f"const handler{i} = () => process(item{i});" for i in range(token_count))


# ── Windowing ────────────────────────────────────────────────────────────────


def test_a_chunk_that_already_fits_becomes_exactly_one_child():
    """Uniformity: every indexed row is a child, so no caller needs a branch."""
    chunk = _chunk("def verify(t):\n    return jwt.decode(t, SECRET)\n")
    children = children_of(chunk)

    assert len(children) == 1
    assert children[0].page_content == chunk.page_content
    assert children[0].metadata[CHILD_COUNT] == 1
    assert children[0].metadata[PARENT_TEXT] == chunk.page_content


def test_every_child_fits_the_window():
    """The whole point: no row is longer than the embedder can read."""
    children = children_of(_chunk(_long_text()))
    assert len(children) > 1, "fixture did not need splitting"
    for child in children:
        assert len(child.page_content) <= EMBED_WINDOW_CHARS


def test_the_children_cover_the_whole_parent():
    """
    Coverage is what replaces a long window.

    Every character of the parent must appear in at least one child, or the fix has
    simply moved the blind spot rather than removed it.
    """
    text = _long_text()
    children = children_of(_chunk(text))

    covered = bytearray(len(text))
    for child in children:
        covered[child.metadata["pc_child_start"]:child.metadata["pc_child_end"]] = b"\x01" * len(
            child.page_content
        )

    gaps = [i for i, flag in enumerate(covered) if not flag]
    assert not gaps, f"{len(gaps)} characters of the parent appear in no child"


def test_consecutive_children_overlap():
    """A fact spanning a boundary must be wholly inside at least one child."""
    children = children_of(_chunk(_long_text()))
    assert len(children) >= 2
    first_end = children[0].metadata["pc_child_end"]
    second_start = children[1].metadata["pc_child_start"]
    assert second_start < first_end, "children do not overlap"


def test_an_overlap_at_or_above_the_window_is_rejected():
    """
    Such an overlap never advances, so the splitter would emit children forever.
    Refusing is better than hanging on a large file.
    """
    with pytest.raises(ValueError):
        children_of(_chunk(_long_text()), window_chars=100, overlap_chars=100)
    with pytest.raises(ValueError):
        children_of(_chunk(_long_text()), window_chars=100, overlap_chars=150)
    with pytest.raises(ValueError):
        children_of(_chunk(_long_text()), window_chars=0)


def test_the_last_window_is_not_a_near_empty_remainder():
    """
    A trailing window that adds no text past the previous one is dropped.

    Keeping it would add a row whose vector is noise — a short fragment of an
    unrelated token run — which is worse than no row.
    """
    for length in (901, 1000, 1200, 1650, 1800, 2700):
        text = "x" * length
        children = children_of(_chunk(text))
        ends = [c.metadata["pc_child_end"] for c in children]

        for child in children:
            assert len(child.page_content) > 0, f"empty child for length {length}"

        # The invariant that actually matters, and the only one that catches a
        # redundant trailing window: every child must reach further than the one
        # before it. A child whose end equals its predecessor's covers no new text,
        # so it is a duplicate row with a near-identical vector competing for a
        # result slot.
        #
        # Asserting only "non-empty" and "the last end equals the length" is not
        # enough — a redundant 30-character tail satisfies both, which is how the
        # first version of this test passed while the guard was deleted.
        for previous, current in zip(ends, ends[1:]):
            assert current > previous, (
                f"length {length}: a child covers no text past the previous one "
                f"(ends {ends})"
            )

        # The union must still cover the parent.
        assert ends[-1] == length, (ends, length)


# ── Identity and fusion compatibility ────────────────────────────────────────


def test_children_keep_the_parents_chunk_index():
    """
    This is what makes the fix cheap: both fusion functions key on
    `{source}::{chunk_index}`, so siblings collapse to one entry whose RRF scores
    accumulate. Changing this would silently disable parent-level fusion.
    """
    children = children_of(_chunk(_long_text(), chunk_index=7))
    assert {c.metadata["chunk_index"] for c in children} == {7}
    assert {c.metadata[PARENT_ID] for c in children} == {parent_id_of({"source": "/repo/app.py", "chunk_index": 7})}


def test_siblings_but_not_strangers_share_a_parent_id():
    a = children_of(_chunk(_long_text(), chunk_index=1))
    b = children_of(_chunk(_long_text(), chunk_index=2))
    assert a[0].metadata[PARENT_ID] != b[0].metadata[PARENT_ID]


def test_children_carry_the_parents_bookkeeping():
    """Citations and context both need the parent's span, not the child's."""
    children = children_of(_chunk(_long_text()))
    for child in children:
        assert child.metadata[PARENT_START_LINE] == 10
        assert child.metadata[PARENT_END_LINE] == 60
        assert child.metadata[PARENT_TEXT] == _long_text()
        assert child.metadata[CHILD_INDEX] < child.metadata[CHILD_COUNT]


def test_the_input_chunk_is_never_mutated():
    """
    Chunker output is cached and shared. Writing child bookkeeping onto a shared
    Document would leak one indexing run's metadata into every later request that
    reads the same cached chunk.
    """
    chunk = _chunk(_long_text())
    before = dict(chunk.metadata)
    children_of(chunk)
    assert chunk.metadata == before
    assert PARENT_TEXT not in chunk.metadata


def test_children_of_all_preserves_order():
    chunks = [_chunk(_long_text(), chunk_index=i) for i in range(3)]
    children = children_of_all(chunks)
    indices = [c.metadata["chunk_index"] for c in children]
    assert indices == sorted(indices)


# ── Expansion ────────────────────────────────────────────────────────────────


def test_parent_context_returns_the_whole_parent():
    children = children_of(_chunk(_long_text()))
    assert len(children) > 1
    # A middle child is a fragment...
    assert len(children[0].page_content) < len(_long_text())
    # ...but what the LLM would read is the whole thing.
    assert parent_context(children[0]) == _long_text()


def test_parent_context_falls_back_for_documents_that_were_never_split():
    """An older index, or a caller that indexed whole chunks, must still work."""
    plain = _chunk("def f(): pass")
    assert parent_context(plain) == "def f(): pass"


def test_expand_to_context_widens_text_but_keeps_the_childs_offsets():
    """
    Context comes from the parent; the citation stays precise.

    Replacing metadata wholesale would lose the child's own span, and a citation
    pointing at the whole 60-line function when 12 lines matched is a worse answer
    than one pointing at the 12.
    """
    children = children_of(_chunk(_long_text()))
    expanded = expand_to_context([children[0]])

    assert expanded[0].page_content == _long_text()
    assert "pc_child_start" in expanded[0].metadata


def test_dedupe_keeps_one_view_per_parent():
    """
    Three children of one function would otherwise take three context slots and
    three copies of the same text.
    """
    children = children_of(_chunk(_long_text()))
    assert len(children) > 1
    assert len(dedupe_to_parents(children)) == 1


def test_dedupe_keeps_the_best_ranked_view():
    """Input order is rank order, so the first child is the one that matched best."""
    children = children_of(_chunk(_long_text()))
    kept = dedupe_to_parents(children)
    assert kept[0].metadata[CHILD_INDEX] == children[0].metadata[CHILD_INDEX]


def test_dedupe_preserves_distinct_parents_in_order():
    first = children_of(_chunk(_long_text(), chunk_index=1))
    second = children_of(_chunk(_long_text(), chunk_index=2))
    mixed = [first[0], second[0], first[-1], second[-1]]
    kept = dedupe_to_parents(mixed)
    assert [d.metadata["chunk_index"] for d in kept] == [1, 2]


# ── The payoff: the defect, fixed ────────────────────────────────────────────


def _chunk_with_tail_fact() -> str:
    body = [f"const handler{i} = () => process(item{i});" for i in range(220)]
    body.append("const ROLLBACK_TICKET = 'QX-4417';")
    return "\n".join(body)


def test_the_window_is_smaller_than_the_chunker_ceiling():
    """
    A tripwire on the relationship that causes the defect.

    If the chunker's ceiling ever drops below the embed window, this fails and the
    author should reconsider whether children are still needed — not silently keep
    paying for them.
    """
    from app.services import code_chunker

    assert EMBED_WINDOW_CHARS < code_chunker.MAX_CHUNK_CHARS


def test_a_tail_fact_becomes_reachable_once_it_is_a_child():
    """
    THE BEFORE AND AFTER.

    As a whole chunk, the identifier at the end is outside the embedder's window and
    dense search scores it at zero (its counterpart test in test_offline_embedder.py
    asserts exactly that). Once split into children, the tail is the *body* of a
    later child — a short text the embedder can read in full — and the query finds
    it.

    Nothing about the embedding model changed. Coverage came from the union of
    children instead of from one vector being long enough.
    """
    parent = _chunk(_chunk_with_tail_fact())
    embedder = OfflineEmbedder()

    # Before: the fact is invisible to a vector built from the whole chunk.
    whole_scores = rank_by_dense("ROLLBACK_TICKET QX-4417", [parent], embedder)
    assert whole_scores[0][1] < 0.05, "fixture no longer reproduces the defect"

    # After: it is the body of a child, and that child is the top hit.
    children = children_of(parent)
    hits = rank_by_dense("ROLLBACK_TICKET QX-4417", children, embedder)
    best_doc, best_score = hits[0]
    whole_score = whole_scores[0][1]

    assert "ROLLBACK_TICKET" in best_doc.page_content, (
        "the top-ranked child is not the one holding the fact"
    )

    # Measured margins, not a guessed threshold: the whole chunk scores 0.0000
    # because the identifier is in no vector at all, and the child that contains it
    # scores 0.143. The gap is the fix.
    #
    # 0.143 rather than the 0.87 a five-token control scores, because this child is
    # ~250 tokens of boilerplate with one distinctive identifier in it — and this
    # hashing double dilutes its signal across a long document in a way a real model
    # does not. The absolute value is a property of the double; what matters is that
    # the fact is now in a vector and that vector ranks first.
    assert best_score > 0.05, f"the tail child was not found ({best_score})"
    assert best_score > whole_score, (best_score, whole_score)

    # And the LLM still receives the whole function, not the window.
    assert parent_context(best_doc) == _chunk_with_tail_fact()


def test_indexing_children_does_not_lose_the_parent_text():
    """
    Round trip over a dense index: search the children, expand the winner, and the
    text that comes back is the parent in full.
    """
    parents = [
        _chunk(_chunk_with_tail_fact(), chunk_index=1),
        _chunk("\n".join(f"renderWidget{i}(props);" for i in range(220)), chunk_index=2),
    ]
    children = children_of_all(parents)
    index = DenseIndex(children, OfflineEmbedder())

    top = index.search("ROLLBACK_TICKET QX-4417", top_k=3)
    context = expand_to_context(top)

    assert context, "nothing retrieved"
    assert context[0].page_content == _chunk_with_tail_fact()
