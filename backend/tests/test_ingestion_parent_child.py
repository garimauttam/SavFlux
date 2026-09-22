"""
Write-side wiring tests: ingestion stores embed windows, not whole chunks.

The read side is covered in `test_parent_child_wiring.py`. These tests cover the
half that changes what is actually stored: after this, a Chroma row is a window,
and the whole chunk survives only in that row's `pc_parent_text`.

All of these drive the real `ingest_github_repo` / `ingest_uploaded_files` with the
clone, file scan and splitter stubbed out, so the delta logic, batching, counting
and Chroma calls are the production ones. `_to_index_rows` is deliberately NOT
stubbed — if it is bypassed, nothing here is testing the thing it claims to.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.documents import Document

from app.services import ingestion_service as ing
from app.services.parent_child import EMBED_WINDOW_CHARS, parent_context

REPO = "https://github.com/octo/demo.git"

# `ingest_github_repo` normalizes the URL before it stamps anything, so the stored
# `source` / `repo_url` are built from the normalized form. Fixtures that use the
# raw URL instead look like a *different repo* to the delta logic, which then
# treats every existing file as removed.
NORM = ing.normalize_repo_url(REPO)

BODY = "def handler(request):\n    return request.args.get('id')\n"


def _parent(path: str, token: str, repeat: int = 25, chunk_index: int = 0) -> Document:
    """
    A chunk long enough to need several windows.

    Length is load-bearing: a chunk that fits its window comes back as a single
    row, and every "more rows than chunks" assertion below would be vacuous.
    """
    text = BODY * repeat + f"\n    raise ValueError('{token}')\n"
    return Document(
        page_content=text,
        metadata={
            # These keys mirror what `_load_and_split` stamps. `repo_url` in
            # particular is load-bearing rather than decorative: the delta logic
            # filters existing rows by it, so a chunk without one makes every
            # re-ingest look like a brand-new repo.
            "source": f"{NORM}::{path}",
            "file_path": f"{NORM}::{path}",
            "file_name": path,
            "language": "python",
            "repo_url": NORM,
            "content_hash": f"hash-{token}",
            "chunk_index": chunk_index,
            "symbol_name": "handler",
            "start_line": 1,
            "end_line": 20,
        },
    )


class _FakeCollection:
    """Minimal chromadb collection surface: count / get / delete."""

    def __init__(self, store: "_FakeVectorstore") -> None:
        self._store = store

    def count(self) -> int:
        return len(self._store.rows)

    def get(self, include=None) -> dict:
        return {
            "ids": [rid for rid, _ in self._store.rows],
            "metadatas": [doc.metadata for _, doc in self._store.rows],
            "documents": [doc.page_content for _, doc in self._store.rows],
        }

    def delete(self, ids) -> None:
        doomed = set(ids)
        self._store.deleted_ids.extend(doomed)
        self._store.rows = [(r, d) for r, d in self._store.rows if r not in doomed]


class _FakeVectorstore:
    """
    Records what ingestion hands to Chroma.

    Ids are unique per row, matching langchain_chroma, which generates
    `str(uuid.uuid4())` per text. That matters here: if ids were derived from
    content, sibling windows of one parent could collide and silently overwrite.
    """

    def __init__(self, existing=None) -> None:
        self.rows: list[tuple[str, Document]] = list(existing or [])
        self.deleted_ids: list[str] = []
        self.batches: list[list[Document]] = []
        self._collection = _FakeCollection(self)

    def add_documents(self, documents):
        self.batches.append(list(documents))
        added = []
        for doc in documents:
            rid = f"row-{len(self.rows)}"
            self.rows.append((rid, doc))
            added.append(rid)
        return added


def _one_file(name: str):
    """
    A `_collect_files` stand-in that creates one real file under the temp clone.

    A lambda returning `write_text(...) or path` looks equivalent and is not:
    `write_text` returns the number of bytes written, so the expression yields an
    int for every non-empty file.
    """

    def _collect(root):
        path = Path(root) / name
        path.write_text("x = 1\n")
        return [path]

    return _collect


async def _ingest(store, documents, files):
    """Run the real pipeline with clone/scan/split stubbed and nothing else."""

    def _collect(root):
        # Create real files under the real tmp_dir: the delta logic calls
        # Path.relative_to() on them, so they cannot be bare strings.
        made = []
        for name in files:
            path = Path(root) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n")
            made.append(path)
        return made

    with patch.object(ing.git, "Repo", return_value=object()), \
         patch.object(ing, "_collect_files", side_effect=_collect), \
         patch.object(ing, "_load_and_split", return_value=documents), \
         patch.object(ing, "_get_vectorstore", return_value=store), \
         patch("app.services.trust_service.record_index"), \
         patch("app.services.history_service.seed_mirror_from_tmp"):
        return await ing.ingest_github_repo(REPO)


# ── Rows ──────────────────────────────────────────────────────────────────────

def test_ingestion_writes_more_rows_than_chunks():
    store = _FakeVectorstore()
    docs = [_parent("a.py", "ZEBRA"), _parent("b.py", "YAK")]

    result = asyncio.run(_ingest(store, docs, ["a.py", "b.py"]))

    assert result["status"] == "success"
    assert result["chunks_created"] == 2, "chunks_created must still count chunks"
    assert result["vectors_created"] > 2, (
        "rows should outnumber chunks once chunks exceed one window"
    )
    assert len(store.rows) == result["vectors_created"]


def test_every_row_carries_the_whole_chunk_not_the_window():
    store = _FakeVectorstore()
    parent = _parent("a.py", "ZEBRA")

    asyncio.run(_ingest(store, [parent], ["a.py"]))

    for _, row in store.rows:
        assert row.metadata["pc_parent_text"] == parent.page_content
        # The row itself is a fragment; the parent is what the LLM gets.
        assert len(row.page_content) <= EMBED_WINDOW_CHARS
        assert parent_context(row) == parent.page_content


def test_rows_fit_the_embed_window():
    """
    The entire point of the write side: nothing handed to the embedder exceeds
    the window, so nothing is silently truncated.
    """
    store = _FakeVectorstore()
    oversized = _parent("big.py", "ZEBRA", repeat=60)
    assert len(oversized.page_content) > 3000, "fixture must beat MAX_CHUNK_CHARS"

    asyncio.run(_ingest(store, [oversized], ["big.py"]))

    assert len(store.rows) > 1
    assert all(len(d.page_content) <= EMBED_WINDOW_CHARS for _, d in store.rows)


def test_rows_of_one_parent_share_the_chunk_index():
    """
    Fusion keys documents as `{source}::{chunk_index}`. If siblings did not share
    it, each window would fuse as a separate document and the scores that should
    accumulate onto one parent would scatter across several.
    """
    store = _FakeVectorstore()
    asyncio.run(_ingest(store, [_parent("a.py", "ZEBRA", chunk_index=7)], ["a.py"]))

    indices = {d.metadata["chunk_index"] for _, d in store.rows}
    assert indices == {7}, f"siblings disagree about their parent: {indices}"


def test_a_chunk_that_fits_is_stored_as_a_single_unchanged_row():
    store = _FakeVectorstore()
    small = Document(
        page_content="def tiny():\n    return 1\n",
        metadata={
            "source": f"{NORM}::s.py",
            "repo_url": NORM,
            "content_hash": "h-small",
        },
    )

    asyncio.run(_ingest(store, [small], ["s.py"]))

    assert len(store.rows) == 1
    assert store.rows[0][1].page_content == small.page_content


# ── Delta logic ───────────────────────────────────────────────────────────────

def test_reingesting_an_unchanged_repo_adds_no_rows():
    """
    Idempotency hinges on children inheriting the parent's content_hash.

    `indexed_map[src]` takes its hash from the first row it sees for a source. If
    a child's hash did not match the parent's, every re-ingest would look like a
    change and the index would grow without bound.
    """
    store = _FakeVectorstore()
    docs = [_parent("a.py", "ZEBRA")]

    first = asyncio.run(_ingest(store, docs, ["a.py"]))
    rows_after_first = len(store.rows)
    batches_after_first = len(store.batches)

    second = asyncio.run(_ingest(store, docs, ["a.py"]))

    assert first["vectors_created"] > 1, "fixture must have produced multiple rows"
    assert len(store.rows) == rows_after_first
    assert len(store.batches) == batches_after_first, "second run embedded something"
    assert second["chunks_created"] == 0


def test_changed_file_deletes_every_row_of_its_chunks():
    """
    Stale detection keys by `source` and collects all ids for it, so it should
    still delete every child. That held trivially when there was one row per
    chunk; with several it is an assumption worth pinning.
    """
    store = _FakeVectorstore()
    asyncio.run(_ingest(store, [_parent("a.py", "ZEBRA")], ["a.py"]))
    before = {rid for rid, _ in store.rows}
    assert len(before) > 1

    # Same path, new content hash — the file changed.
    asyncio.run(_ingest(store, [_parent("a.py", "YAK")], ["a.py"]))

    assert before <= set(store.deleted_ids), (
        "some rows from the previous version survived a re-index: "
        f"{before - set(store.deleted_ids)}"
    )


def test_removed_file_deletes_every_row_of_its_chunks():
    store = _FakeVectorstore()
    asyncio.run(_ingest(store, [_parent("gone.py", "ZEBRA")], ["gone.py"]))
    before = {rid for rid, _ in store.rows}
    assert len(before) > 1

    # File no longer collected, but the repo still ingests something.
    asyncio.run(_ingest(store, [_parent("kept.py", "YAK")], ["kept.py"]))

    assert before <= set(store.deleted_ids)


# ── Cross-path consistency ────────────────────────────────────────────────────

def test_upload_path_writes_the_same_row_shape_as_the_github_path():
    """
    Both paths write to one collection. If upload indexed whole chunks, the index
    would hold two row shapes and `parent_context` would be correct on only one.
    """
    github_store = _FakeVectorstore()
    parent = _parent("a.py", "ZEBRA")
    asyncio.run(_ingest(github_store, [parent], ["a.py"]))
    github_meta_keys = set(github_store.rows[0][1].metadata)

    upload_store = _FakeVectorstore()
    with patch.object(ing, "_load_and_split", return_value=[parent]), \
         patch.object(ing, "_get_vectorstore", return_value=upload_store):
        asyncio.run(ing.ingest_uploaded_files([("a.py", b"x = 1\n")]))

    assert upload_store.rows, "upload path wrote nothing"
    for _, row in upload_store.rows:
        assert set(row.metadata) == github_meta_keys
        assert row.metadata["pc_parent_text"] == parent.page_content


def test_upload_path_reports_chunks_and_vectors_separately():
    store = _FakeVectorstore()
    parent = _parent("a.py", "ZEBRA")

    with patch.object(ing, "_load_and_split", return_value=[parent]), \
         patch.object(ing, "_get_vectorstore", return_value=store):
        result = asyncio.run(ing.ingest_uploaded_files([("a.py", b"x = 1\n")]))

    assert result["chunks_created"] == 1
    assert result["vectors_created"] == len(store.rows) > 1


# ── Progress ──────────────────────────────────────────────────────────────────

def test_progress_reports_chunks_and_surfaces_vectors_only_when_they_differ():
    store = _FakeVectorstore()
    messages: list[str] = []

    async def collect(event):
        if event.get("step") == "done":
            messages.append(event["message"])

    async def run(docs):
        with patch.object(ing.git, "Repo", return_value=object()), \
             patch.object(ing, "_collect_files", side_effect=_one_file("a.py")), \
             patch.object(ing, "_load_and_split", return_value=docs), \
             patch.object(ing, "_get_vectorstore", return_value=store), \
             patch("app.services.trust_service.record_index"), \
             patch("app.services.history_service.seed_mirror_from_tmp"):
            return await ing.ingest_github_repo(REPO, progress_callback=collect)

    asyncio.run(run([_parent("a.py", "ZEBRA")]))
    assert "1 chunks" in messages[0]
    assert "vectors" in messages[0], "split chunks should mention vector count"

    store2 = _FakeVectorstore()
    store_batches = store2
    async def run_small():
        with patch.object(ing.git, "Repo", return_value=object()), \
             patch.object(ing, "_collect_files", side_effect=_one_file("s.py")), \
             patch.object(ing, "_load_and_split", return_value=[
                 Document(page_content="def tiny():\n    return 1\n",
                          metadata={"source": f"{NORM}::s.py", "content_hash": "h",
                                    "repo_url": NORM})
             ]), \
             patch.object(ing, "_get_vectorstore", return_value=store_batches), \
             patch("app.services.trust_service.record_index"), \
             patch("app.services.history_service.seed_mirror_from_tmp"):
            return await ing.ingest_github_repo(REPO, progress_callback=collect)

    asyncio.run(run_small())
    assert "vectors" not in messages[1], (
        "a one-window chunk should not be described in vector jargon"
    )
