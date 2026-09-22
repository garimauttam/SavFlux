"""
Wiring tests: the read side of small-to-big.

`test_parent_child.py` tests `parent_child.py` as a pure module. This file covers
the other half — that `retrieval_service` actually calls it — and exists mostly for
one specific reason.

Dense and BM25 read *different fields of the same row*, and they have opposite
requirements. Dense must read the window, because the embedder silently truncates
at 256 tokens. BM25 must read the whole chunk, because it has no window and longer
text is strictly more signal. That split is one line wide, and both directions look
identical in review.

Getting it wrong is quiet. BM25 is the strongest leg (72.7% hit@5 against dense's
36.4%), so if it starts indexing 900-character windows instead of whole chunks, the
fused number can fall while the dense leg improves, with nothing in the diff to
explain it. These tests make that specific mistake fail loudly.
"""

import ast
import inspect
import textwrap

import pytest

from langchain_core.documents import Document

from app.services import retrieval_service as rs
from app.services.parent_child import (
    EMBED_WINDOW_CHARS,
    children_of,
    parent_context,
)

FILLER = "def handler(request):\n    return request.args.get('id')\n"


def _parent_chunk(tail_token: str) -> Document:
    """
    A chunk whose distinctive token sits beyond the embed window.

    Length matters: if the whole chunk fits in one window then `children_of`
    returns a single child equal to the parent, and every test below would pass
    whether or not the code is correct.
    """
    head = FILLER * 25
    return Document(
        page_content=head + f"\n    raise ValueError('{tail_token}')\n",
        metadata={"source": "repo::a.py", "chunk_index": 7, "content_hash": "h1"},
    )


def _rows(parent: Document) -> list[Document]:
    """What Chroma hands back after ingestion stores windows: one row per child."""
    return [
        Document(page_content=child.page_content, metadata=child.metadata)
        for child in children_of(parent)
    ]


def _neighbours(count: int = 3) -> list[Document]:
    """
    Unrelated chunks, so the corpus is not a single document.

    WHY THIS IS NOT OPTIONAL: rank_bm25 assigns Okapi IDF per term, and with only
    one document every term has df == N == 1, which drives IDF negative. The index
    substitutes `epsilon * average_idf`, which is also negative here, so every
    score lands <= 0 — and `BM25Index.search` drops scores <= 0.0. A one-document
    corpus therefore returns *nothing*, and a test that skipped these would fail
    for a reason that has nothing to do with the code under test.
    """
    return [
        Document(
            page_content=f"def unrelated_{i}(value):\n    return value\n",
            metadata={"source": f"repo::other{i}.py", "chunk_index": i},
        )
        for i in range(count)
    ]


def test_the_fixture_actually_splits_into_several_windows():
    """
    Guard the guard. Every assertion below is vacuous on an unsplit chunk, so the
    fixture is the first thing that has to be true.
    """
    parent = _parent_chunk("ZEBRA")
    assert len(parent.page_content) > EMBED_WINDOW_CHARS
    assert len(_rows(parent)) > 1


def test_bm25_corpus_reads_the_parent_and_drops_sibling_windows():
    parent = _parent_chunk("ZEBRA")
    rows = _rows(parent)

    corpus = rs._bm25_corpus(rows)

    # One entry per parent, not one per window: siblings share the parent's text,
    # so keeping them would index N identical copies and skew IDF.
    assert len(corpus) == 1
    assert corpus[0].page_content == parent.page_content
    assert corpus[0].metadata["chunk_index"] == 7


def test_bm25_finds_a_term_that_lives_outside_the_embed_window():
    """
    The payoff, at the lexical leg.

    The discriminator is the hit's *length*, not just that the term was found: if
    BM25 indexed raw rows, the token would still be matched — by the tail window,
    which contains it — but the returned document would be a ~900-character
    fragment instead of the whole chunk, and the model would be shown half a
    function.
    """
    parent = _parent_chunk("ZEBRA")
    index = rs.BM25Index(rs._bm25_corpus(_rows(parent) + _neighbours()))

    hits = index.search("ZEBRA", top_k=5)

    assert hits, "BM25 did not find a token that is present in the chunk"
    assert "ZEBRA" in hits[0].page_content
    assert len(hits[0].page_content) == len(parent.page_content)


def test_bm25_corpus_is_the_identity_for_rows_without_parent_metadata():
    """
    Why this step is safe to land before the re-index.

    An index built before this change has no `pc_*` metadata. For those rows
    `parent_context` returns `page_content` and no two rows share a parent id, so
    the corpus is byte-for-byte what it was — the wiring is a no-op until a
    re-ingest has actually stored windows.
    """
    legacy = [
        Document(
            page_content=f"chunk {i} body with distinct text",
            metadata={"source": "repo::b.py", "chunk_index": i},
        )
        for i in range(3)
    ]

    corpus = rs._bm25_corpus(legacy)

    assert [d.page_content for d in corpus] == [d.page_content for d in legacy]
    assert [d.metadata for d in corpus] == [d.metadata for d in legacy]


class _FakeCollection:
    def __init__(self, rows: list[Document]) -> None:
        self._rows = rows

    def count(self) -> int:
        return len(self._rows)

    def get(self, include=None) -> dict:
        return {
            "ids": [f"id{i}" for i in range(len(self._rows))],
            "documents": [d.page_content for d in self._rows],
            "metadatas": [d.metadata for d in self._rows],
        }


def test_get_bm25_index_wires_the_corpus_helper(monkeypatch):
    """
    `_bm25_corpus` being correct is not the same as it being called. This exercises
    the real build path — cache miss, Chroma read, index construction — with the
    disk cache stubbed out so the test never touches the filesystem.
    """
    parent = _parent_chunk("ZEBRA")
    fake = type("FakeVectorstore", (), {"_collection": _FakeCollection(_rows(parent))})()

    monkeypatch.setattr(rs, "_bm25_index_cache", None)
    monkeypatch.setattr(rs, "_bm25_doc_count", -1)
    monkeypatch.setattr(rs, "_load_bm25_from_disk", lambda expected: None)
    monkeypatch.setattr(rs, "_save_bm25_to_disk", lambda index, count: None)

    index = rs._get_bm25_index(fake)

    assert index is not None, "BM25 build failed; the other assertions would be vacuous"
    assert len(index.documents) == 1
    assert "ZEBRA" in index.documents[0].page_content
    assert len(index.documents[0].page_content) == len(parent.page_content)


def test_context_builder_feeds_the_model_the_parent_not_the_window():
    """
    A structural guard, kept alongside the behavioural one rather than instead.

    `test_the_model_receives_the_whole_parent_when_only_a_tail_window_matched`
    below drives the real generator and is the test that actually proves the
    payoff. This one stays because it is instant and its failure message names the
    specific revert, which is worth more than a diff when someone is mid-change:

        the LLM context builder is reading the stored row text, which for a
        window row is a fragment — use parent_context(doc) instead

    It parses rather than greps because the comments around this line discuss
    `doc.page_content` by name, and a substring check flags its own explanation.
    """
    source = inspect.getsource(rs.stream_answer)

    # Parse rather than grep: the docstring and comments around this line discuss
    # `doc.page_content` by name, and a substring check flags its own explanation.
    tree = ast.parse(textwrap.dedent(source))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "parent_context"
    ]
    assert calls, "stream_answer never calls parent_context(doc)"

    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "page_content"
    ]
    assert not reads, (
        "the LLM context builder is reading the stored row text, which for a window "
        "row is a fragment — use parent_context(doc) instead"
    )


# ── BM25 cache invalidation ───────────────────────────────────────────────────
#
# Small-to-big changes what `.count()` counts: rows are now windows, so the count
# grows faster than the chunk count. The staleness check is an equality test on
# that number, so the only thing that matters is that BOTH sides use the same
# unit. These tests pin the unit and the invalidation, because a mismatch here
# would let a stale index load as if it were fresh — and a stale BM25 index does
# not error, it just returns yesterday's results.


def _fake_store_with(rows):
    return type("FakeVectorstore", (), {"_collection": _FakeCollection(rows)})()


def test_bm25_cache_rebuilds_when_the_row_count_changes(monkeypatch):
    parent = _parent_chunk("ZEBRA")
    rows = _rows(parent)
    store = _fake_store_with(rows)

    monkeypatch.setattr(rs, "_bm25_index_cache", None)
    monkeypatch.setattr(rs, "_bm25_doc_count", -1)
    monkeypatch.setattr(rs, "_load_bm25_from_disk", lambda expected: None)
    monkeypatch.setattr(rs, "_save_bm25_to_disk", lambda index, count: None)

    first = rs._get_bm25_index(store)
    assert first is not None

    # Unchanged collection: the in-memory cache must be reused, not rebuilt.
    assert rs._get_bm25_index(store) is first

    # The index grew by one more window.
    store._collection._rows = rows + _rows(_parent_chunk("YAK"))[:1]
    second = rs._get_bm25_index(store)
    assert second is not first, "a changed row count must invalidate the index"


def test_the_disk_cache_compares_counts_in_the_same_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "_bm25_cache_path", lambda: tmp_path / "bm25_index.pkl")
    index = rs.BM25Index(rs._bm25_corpus(_rows(_parent_chunk("ZEBRA")) + _neighbours()))

    rs._save_bm25_to_disk(index, 4)

    assert rs._load_bm25_from_disk(4) is not None, "same count must load"
    assert rs._load_bm25_from_disk(5) is None, "a different count must be rejected"


def test_the_saved_count_is_the_collections_row_count(monkeypatch):
    """
    The saved number must be `.count()`, whatever that counts. Hard-coding it to
    the chunk count would silently disable the staleness check from here on.
    """
    parent = _parent_chunk("ZEBRA")
    rows = _rows(parent)
    store = _fake_store_with(rows)
    saved = {}

    monkeypatch.setattr(rs, "_bm25_index_cache", None)
    monkeypatch.setattr(rs, "_bm25_doc_count", -1)
    monkeypatch.setattr(rs, "_load_bm25_from_disk", lambda expected: None)
    monkeypatch.setattr(
        rs, "_save_bm25_to_disk", lambda index, count: saved.update(count=count)
    )

    rs._get_bm25_index(store)

    assert saved["count"] == store._collection.count() == len(rows)
    assert len(rows) > 1, "fixture must be windowed, or this asserts nothing"


def test_invalidate_clears_both_the_memory_and_disk_caches(tmp_path, monkeypatch):
    """
    Why this matters more than the count check: a re-ingest can replace content
    while leaving the row count identical. The count comparison cannot see that,
    so the explicit invalidation after ingestion is what actually keeps the index
    fresh — it is not redundant belt-and-braces.
    """
    cache_file = tmp_path / "bm25_index.pkl"
    monkeypatch.setattr(rs, "_bm25_cache_path", lambda: cache_file)
    rs._save_bm25_to_disk(
        rs.BM25Index(rs._bm25_corpus(_rows(_parent_chunk("ZEBRA")) + _neighbours())), 4
    )
    assert cache_file.exists()

    monkeypatch.setattr(rs, "_bm25_index_cache", object())
    monkeypatch.setattr(rs, "_bm25_doc_count", 99)

    rs.invalidate_bm25_cache()

    assert not cache_file.exists()
    assert rs._bm25_index_cache is None
    assert rs._bm25_doc_count == -1


# ── The payoff test: the real stream_answer, end to end ───────────────────────
#
# Everything above tests the wiring in isolation. This drives the actual async
# generator production calls and asserts on the prompt the LLM receives.
#
# The design constraint that makes it a real test: the token we look for must be
# UNREACHABLE from anything the retriever returns. If any retrieved document's
# page_content contained it, the assertion would hold whether or not
# parent_context is used, and the test could not fail. So the fixture puts one
# fact at the head of a long chunk and has retrieval return only the TAIL window.

HEAD_FACT = "def rotate_signing_key(material):\n    return material.hex()\n"
TAIL_FACT = "\ndef escalate_privileges(user):\n    return user.is_admin\n"
LONG_FILLER = "def helper(value):\n    return value + 1\n" * 45


def _split_parent() -> Document:
    """A chunk that splits, with a fact at each end and none in between."""
    return Document(
        page_content=HEAD_FACT + LONG_FILLER + TAIL_FACT,
        metadata={
            "source": "https://github.com/octo/demo::big.py",
            "file_name": "big.py",
            "language": "python",
            "repo_url": "https://github.com/octo/demo",
            "chunk_index": 0,
            "symbol_name": "escalate_privileges",
            "start_line": 1,
            "end_line": 60,
        },
    )


class _FakeRetriever:
    def __init__(self, docs):
        self._docs = docs

    async def ainvoke(self, query, **kwargs):
        return list(self._docs)


class _FakeBM25:
    """Empty results, so only the dense leg supplies context."""

    def search(self, query, top_k=15, repo_urls=None, file_filter=None):
        return []


class _FakeCollectionCount:
    def count(self):
        return 1


class _FakeVectorstore:
    def __init__(self, docs):
        self._docs = docs
        self._collection = _FakeCollectionCount()

    def as_retriever(self, **kwargs):
        return _FakeRetriever(self._docs)


class _CapturingLLM:
    """Records the messages it was asked to answer from, and streams a reply."""

    def __init__(self, sink):
        self._sink = sink

    def with_config(self, **kwargs):
        return self

    async def astream(self, messages):
        self._sink.append(messages)
        yield type("Chunk", (), {"content": "The tail function handles escalation."})()


@pytest.mark.asyncio
async def test_the_model_receives_the_whole_parent_when_only_a_tail_window_matched(
    monkeypatch,
):
    parent = _split_parent()
    windows = children_of(parent)
    tail_window = windows[-1]

    # Guard the guard. The test only means something if the retrieved text cannot
    # contain the head fact on its own.
    assert len(windows) > 1, "fixture must exceed the embed window"
    assert "escalate_privileges" in tail_window.page_content
    assert "rotate_signing_key" not in tail_window.page_content, (
        "the retrieved window contains the head fact, so this test could pass "
        "without parent_context"
    )
    assert "rotate_signing_key" in parent_context(tail_window)

    prompts = []
    monkeypatch.setattr(rs, "_get_vectorstore", lambda: _FakeVectorstore([tail_window]))
    monkeypatch.setattr(rs, "_get_bm25_index", lambda store: _FakeBM25())
    monkeypatch.setattr(rs, "_get_dep_graph_hints", lambda *a, **k: [])
    async def _passthrough_rerank(query, docs, top_n=5):
        return list(docs)

    monkeypatch.setattr(rs, "rerank", _passthrough_rerank)
    monkeypatch.setattr(rs, "get_chat_llm", lambda **kwargs: _CapturingLLM(prompts))

    pieces = [
        piece
        async for piece in rs.stream_answer(
            "How does escalate_privileges work?", chat_history=[]
        )
    ]

    assert prompts, "the LLM was never called; retrieval returned nothing"
    prompt = "\n".join(
        m.content for m in prompts[0] if getattr(m, "content", None)
    )

    # The payoff: the model sees the head fact even though no retrieved document
    # contains it. Only parent_context can have put it there.
    assert "rotate_signing_key" in prompt
    assert parent.page_content in prompt, "the model did not receive the full chunk"
    assert "The tail function handles escalation." in "".join(pieces)
