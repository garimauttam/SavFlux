"""
test_file_tree.py — P2 File Tree Explorer ($0) tests.

Covers:
  - file_tree_service build_file_tree + search
  - API GET /file-tree and /file-tree/search
"""

from unittest.mock import patch

import pytest


def test_build_file_tree_basic():
    import app.services.file_tree_service as svc
    files = [
        {"source": "src/a.py", "file_name": "a.py", "language": "python", "repo_url": "https://github.com/o/r", "chunk_count": 2},
        {"source": "src/b.py", "file_name": "b.py", "language": "python", "repo_url": "https://github.com/o/r", "chunk_count": 1},
        {"source": "src/utils/c.py", "file_name": "c.py", "language": "python", "repo_url": "https://github.com/o/r", "chunk_count": 3},
        {"source": "README.md", "file_name": "README.md", "language": "markdown", "repo_url": "https://github.com/o/r", "chunk_count": 1},
    ]
    tree_data = svc.build_file_tree(files)
    assert tree_data["total_files"] == 4
    assert tree_data["total_dirs"] >= 2  # root + src + src/utils
    # Root has children
    root = tree_data["tree"]
    assert root["type"] == "dir"
    assert any(c["name"] == "src" for c in root["children"])
    assert any(c["name"] == "README.md" for c in root["children"])
    # src dir
    src_dir = next(c for c in root["children"] if c["name"] == "src")
    assert src_dir["file_count"] == 3
    assert "python" in src_dir["languages"]
    # Flat list
    assert len(tree_data["flat"]) == 4


def test_build_file_tree_with_github_prefix():
    import app.services.file_tree_service as svc
    files = [
        {"source": "https://github.com/o/r::src/a.py", "file_name": "a.py", "language": "python", "repo_url": "https://github.com/o/r", "chunk_count": 1},
    ]
    data = svc.build_file_tree(files)
    assert data["flat"][0]["path"] == "src/a.py"


def test_search_files_mocked():
    import app.services.file_tree_service as svc
    fake_files = [
        {"source": "src/auth.py", "file_name": "auth.py", "language": "python"},
        {"source": "src/db.py", "file_name": "db.py", "language": "python"},
    ]
    with patch.object(svc, "_get_indexed_files_sync", return_value=fake_files):
        results = svc.search_files("auth")
        assert len(results) == 1
        assert results[0]["file_name"] == "auth.py"
        # Empty query returns all
        results2 = svc.search_files("", limit=10)
        assert len(results2) == 2
        # No match
        results3 = svc.search_files("nonexistentxyz")
        assert len(results3) == 0


def test_file_tree_api(client):
    # Mock build_file_tree to avoid Chroma
    import app.services.file_tree_service as svc
    fake_tree = {
        "tree": {"name": "", "path": "", "type": "dir", "children": [{"name": "a.py", "path": "a.py", "type": "file", "language": "python", "source": "a.py"}], "file_count": 1, "languages": {"python": 1}},
        "flat": [{"name": "a.py", "path": "a.py", "type": "file", "language": "python", "source": "a.py"}],
        "total_files": 1,
        "total_dirs": 1,
    }
    with patch.object(svc, "build_file_tree", return_value=fake_tree):
        resp = client.get("/api/v1/file-tree")
        assert resp.status_code == 200
        assert resp.json()["total_files"] == 1
    # Search
    with patch.object(svc, "search_files", return_value=[{"source": "a.py", "file_name": "a.py"}]):
        resp2 = client.get("/api/v1/file-tree/search?q=a&limit=10")
        assert resp2.status_code == 200
        assert resp2.json()["total"] == 1
        assert resp2.json()["query"] == "a"
    # Search without q
    with patch.object(svc, "search_files", return_value=[]):
        resp3 = client.get("/api/v1/file-tree/search?limit=5")
        assert resp3.status_code == 200
