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


def _make_doc(content: str, source: str, repo_url: str = "https://github.com/test/repo") -> Document:
    """Helper: create a Document with the same metadata structure as _load_and_split produces."""
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
    Simulate the delta re-indexing logic:
    - File A: unchanged → should be skipped
    - File B: changed → should be in new_docs
    - File C: new → should be in new_docs
    """
    hash_a = hashlib.sha256(b"file a content").hexdigest()[:16]
    hash_b_old = hashlib.sha256(b"file b old").hexdigest()[:16]
    hash_b_new = hashlib.sha256(b"file b new").hexdigest()[:16]
    hash_c = hashlib.sha256(b"file c content").hexdigest()[:16]

    # What's currently in the index (file A and B)
    indexed_map = {
        "/repo/a.py": {"hash": hash_a, "ids": ["id_a1", "id_a2"]},
        "/repo/b.py": {"hash": hash_b_old, "ids": ["id_b1"]},
    }

    # What the current repo scan produced
    documents = [
        _make_doc("file a content", "/repo/a.py"),        # unchanged
        _make_doc("file b new", "/repo/b.py"),            # changed
        _make_doc("file c content", "/repo/c.py"),        # new file
    ]
    # Fix hashes to match our test values
    documents[0].metadata["content_hash"] = hash_a
    documents[1].metadata["content_hash"] = hash_b_new
    documents[2].metadata["content_hash"] = hash_c

    current_sources = {"/repo/a.py", "/repo/b.py", "/repo/c.py"}

    # Replicate the delta logic from ingestion_service.py
    new_docs = []
    stale_ids = []
    seen_sources: set = set()

    for doc in documents:
        src = doc.metadata["source"]
        chash = doc.metadata["content_hash"]
        if src not in seen_sources:
            seen_sources.add(src)
            if src in indexed_map:
                if indexed_map[src]["hash"] == chash:
                    continue  # unchanged — skip
                else:
                    stale_ids.extend(indexed_map[src]["ids"])
        new_docs.append(doc)

    # Mark removed files (none in this test, but verify the logic)
    for src, info in indexed_map.items():
        if src not in current_sources:
            stale_ids.extend(info["ids"])

    # Assertions
    new_srcs = [d.metadata["source"] for d in new_docs]
    assert "/repo/a.py" not in new_srcs, "Unchanged file A should be skipped"
    assert "/repo/b.py" in new_srcs, "Changed file B should be re-indexed"
    assert "/repo/c.py" in new_srcs, "New file C should be indexed"

    assert "id_a1" not in stale_ids, "IDs for unchanged file A should NOT be deleted"
    assert "id_b1" in stale_ids, "Old IDs for changed file B must be deleted"


def test_delta_logic_removes_deleted_files():
    """Files that existed in the index but are no longer in the repo must be purged."""
    hash_old = hashlib.sha256(b"deleted file").hexdigest()[:16]

    indexed_map = {
        "/repo/deleted.py": {"hash": hash_old, "ids": ["id_del1", "id_del2"]},
        "/repo/kept.py":    {"hash": "aabbccdd11223344", "ids": ["id_k1"]},
    }

    documents = [
        _make_doc("kept file content", "/repo/kept.py"),
    ]
    documents[0].metadata["content_hash"] = "aabbccdd11223344"  # same hash → unchanged

    current_sources = {"/repo/kept.py"}  # deleted.py no longer in repo

    new_docs = []
    stale_ids = []
    seen_sources: set = set()

    for doc in documents:
        src = doc.metadata["source"]
        chash = doc.metadata["content_hash"]
        if src not in seen_sources:
            seen_sources.add(src)
            if src in indexed_map:
                if indexed_map[src]["hash"] == chash:
                    continue
                else:
                    stale_ids.extend(indexed_map[src]["ids"])
        new_docs.append(doc)

    for src, info in indexed_map.items():
        if src not in current_sources:
            stale_ids.extend(info["ids"])

    assert len(new_docs) == 0, "Unchanged kept.py should be skipped"
    assert "id_del1" in stale_ids, "Deleted file IDs must be purged"
    assert "id_del2" in stale_ids, "All IDs for deleted file must be purged"
    assert "id_k1" not in stale_ids, "Unchanged file IDs must NOT be purged"
