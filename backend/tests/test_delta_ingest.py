"""
test_delta_ingest.py — Unit tests for the incremental delta re-indexing logic.

Tests the hash-based file skipping without needing a real ChromaDB connection
or embedding API calls.
"""

import hashlib
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from langchain_core.documents import Document

from app.services.ingestion_service import _load_and_split, normalize_repo_url


def _make_doc(content: str, source: str, repo_url: str = "https://github.com/test/repo",
              build: str = "") -> Document:
    """
    A Document with the metadata `_load_and_split` produces, `index_build` included.

    The stamp is not optional in a delta test: a chunk without one is a chunk whose
    pipeline nobody can attribute, which `plan_delta` deliberately re-embeds.
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    return Document(
        page_content=content,
        metadata={
            "source": source,
            "file_name": Path(source).name,
            "language": Path(source).suffix.lstrip("."),
            "repo_url": repo_url,
            "chunk_index": 0,
            "content_hash": content_hash,
            "index_build": build,
        },
    )


def test_content_hash_is_deterministic():
    """The same file content always produces the same content_hash."""
    code = "def hello(): return 42"
    h1 = hashlib.sha256(code.encode()).hexdigest()[:16]
    h2 = hashlib.sha256(code.encode()).hexdigest()[:16]
    assert h1 == h2
    assert len(h1) == 16


def test_normalize_repo_url_collapses_common_github_variants():
    expected = "https://github.com/test/repo"
    assert normalize_repo_url("https://github.com/test/repo") == expected
    assert normalize_repo_url("https://github.com/test/repo/") == expected
    assert normalize_repo_url("https://github.com/test/repo.git") == expected
    assert normalize_repo_url("git@github.com:test/repo.git") == expected


def test_content_hash_differs_on_content_change():
    """Different file content produces different content_hash values."""
    h1 = hashlib.sha256(b"def hello(): return 42").hexdigest()[:16]
    h2 = hashlib.sha256(b"def hello(): return 43").hexdigest()[:16]
    assert h1 != h2


def test_load_and_split_adds_content_hash(tmp_path):
    """_load_and_split() should add content_hash to every chunk's metadata."""
    py_file = tmp_path / "example.py"
    py_file.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")

    docs = _load_and_split([py_file], repo_url="https://github.com/test/repo")

    assert docs, "Expected at least one chunk"
    for doc in docs:
        assert "content_hash" in doc.metadata, "content_hash must be present in chunk metadata"
        assert len(doc.metadata["content_hash"]) == 16, "content_hash should be 16 hex chars"


def test_load_and_split_same_hash_for_all_chunks_of_same_file(tmp_path):
    """All chunks from the same file share the same content_hash."""
    py_file = tmp_path / "big.py"
    # Write enough code to produce multiple chunks
    code = "\n\n".join([f"def func_{i}():\n    return {i}\n" for i in range(50)])
    py_file.write_text(code, encoding="utf-8")

    docs = _load_and_split([py_file], repo_url="https://github.com/test/repo")

    assert len(docs) >= 2, "Expected multiple chunks from a large file"
    hashes = {doc.metadata["content_hash"] for doc in docs}
    assert len(hashes) == 1, "All chunks from the same file must share the same content_hash"


def test_delta_logic_skips_unchanged_files():
    """
    File A unchanged -> skipped; file B changed -> re-embedded with its old rows purged;
    file C new -> embedded.

    These two tests used to paste the production loop into the test body, line for line.
    That is a fixture, not a check: any edit to the real loop — including deleting it —
    left them green. They now call `plan_delta`, so the assertions describe behaviour.
    """
    from app.services.ingestion_service import plan_delta

    hash_a = _hash(b"file a content")
    hash_b_new = _hash(b"file b new")
    hash_c = _hash(b"file c content")
    build = "same-pipeline"

    # The shape `build_indexed_map` returns: sets per file, so a file whose stored rows
    # disagree with each other cannot be quietly resolved to whichever came first.
    indexed_map = {
        "/repo/a.py": {"hashes": {hash_a}, "builds": {build}, "ids": ["id_a1", "id_a2"]},
        "/repo/b.py": {"hashes": {_hash(b"file b old")}, "builds": {build}, "ids": ["id_b1"]},
    }
    documents = [
        _make_doc("file a content", "/repo/a.py", build=build),
        _make_doc("file b new", "/repo/b.py", build=build),
        _make_doc("file c content", "/repo/c.py", build=build),
    ]

    plan = plan_delta(
        indexed_map=indexed_map,
        documents=documents,
        current_sources={"/repo/a.py", "/repo/b.py", "/repo/c.py"},
        build_id=build,
    )

    new_srcs = [d.metadata["source"] for d in plan.new_docs]
    assert "/repo/a.py" not in new_srcs, "Unchanged file A should be skipped"
    assert "/repo/b.py" in new_srcs, "Changed file B should be re-indexed"
    assert "/repo/c.py" in new_srcs, "New file C should be indexed"

    assert "id_a1" not in plan.stale_ids, "IDs for unchanged file A should NOT be deleted"
    assert "id_b1" in plan.stale_ids, "Old IDs for changed file B must be deleted"
    assert plan.files_skipped == 1 and plan.files_changed == 1 and plan.files_new == 1


def test_delta_logic_removes_deleted_files():
    """Files that existed in the index but are no longer in the repo must be purged."""
    from app.services.ingestion_service import plan_delta

    build = "same-pipeline"
    indexed_map = {
        "/repo/deleted.py": {"hashes": {_hash(b"deleted file")}, "builds": {build},
                             "ids": ["id_del1", "id_del2"]},
        "/repo/kept.py": {"hashes": {"aabbccdd11223344"}, "builds": {build}, "ids": ["id_k1"]},
    }
    documents = [_make_doc("kept file content", "/repo/kept.py", build=build)]
    documents[0].metadata["content_hash"] = "aabbccdd11223344"  # same hash -> unchanged

    plan = plan_delta(
        indexed_map=indexed_map,
        documents=documents,
        current_sources={"/repo/kept.py"},
        build_id=build,
    )

    assert plan.new_docs == [], "Unchanged kept.py should be skipped"
    assert "id_del1" in plan.stale_ids, "Deleted file IDs must be purged"
    assert "id_del2" in plan.stale_ids, "All IDs for deleted file must be purged"
    assert "id_k1" not in plan.stale_ids, "Unchanged file IDs must NOT be purged"


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]
