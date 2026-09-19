"""
test_diff.py — P2 Diff Viewer ($0, difflib) tests.

Covers:
  - diff_service get_file_content + compute_diff
  - API GET /diff/file and POST /diff/compare
"""

from unittest.mock import patch, MagicMock

import pytest


def test_get_file_content_mocked():
    import app.services.diff_service as svc
    mock_coll = MagicMock()
    mock_coll.get.return_value = {
        "documents": ["line1\n", "line2\n"],
        "metadatas": [{"language": "python", "chunk_index": 0, "source": "src/a.py"}, {"language": "python", "chunk_index": 1, "source": "src/a.py"}],
    }
    with patch.object(svc, "_get_collection", return_value=mock_coll):
        data = svc.get_file_content("src/a.py")
        assert data["source"] == "src/a.py"
        assert "line1" in data["content"]
        assert data["language"] == "python"
        assert data["chunk_count"] == 2
        # Invalid path
        try:
            svc.get_file_content("../bad")
            assert False, "should raise"
        except ValueError:
            pass


def test_compute_diff_mocked():
    import app.services.diff_service as svc
    def fake_get(src):
        if src == "src/a.py":
            return {"source": "src/a.py", "content": "line1\nline2\nline3\n", "language": "python", "chunk_count": 1, "repo_url": ""}
        if src == "src/b.py":
            return {"source": "src/b.py", "content": "line1\nlineX\nline3\n", "language": "python", "chunk_count": 1, "repo_url": ""}
        raise ValueError("not found")
    with patch.object(svc, "get_file_content", side_effect=fake_get):
        result = svc.compute_diff("src/a.py", "src/b.py", context=3)
        assert "unified_diff" in result
        assert result["added"] == 1
        assert result["removed"] == 1
        assert 0 <= result["similarity"] <= 1
        assert result["a_lines"] == 3
        assert result["b_lines"] == 3


def test_diff_api(client):
    import app.services.diff_service as svc
    # Mock get_file_content for GET /diff/file
    with patch.object(svc, "get_file_content", return_value={"source": "src/a.py", "content": "hello", "language": "python", "chunk_count": 1, "repo_url": ""}):
        resp = client.get("/api/v1/diff/file?source=src/a.py")
        assert resp.status_code == 200
        assert resp.json()["source"] == "src/a.py"
        # Invalid source
        resp2 = client.get("/api/v1/diff/file?source=../bad")
        assert resp2.status_code == 400
        # Not found
        with patch.object(svc, "get_file_content", side_effect=ValueError("Source not found: x")):
            resp3 = client.get("/api/v1/diff/file?source=src/missing.py")
            assert resp3.status_code == 404
    # POST compare
    fake_diff = {
        "source_a": "src/a.py", "source_b": "src/b.py", "language_a": "python", "language_b": "python",
        "unified_diff": "@@ -1,2 +1,2 @@\n-line2\n+lineX\n", "added": 1, "removed": 1, "similarity": 0.8, "a_lines": 3, "b_lines": 3, "a_content": "a", "b_content": "b"
    }
    with patch.object(svc, "compute_diff", return_value=fake_diff):
        resp4 = client.post("/api/v1/diff/compare", json={"source_a": "src/a.py", "source_b": "src/b.py", "context": 3})
        assert resp4.status_code == 200
        assert resp4.json()["added"] == 1
        # Same source -> 400
        resp5 = client.post("/api/v1/diff/compare", json={"source_a": "src/a.py", "source_b": "src/a.py"})
        assert resp5.status_code == 400
        # Missing -> 400
        resp6 = client.post("/api/v1/diff/compare", json={"source_a": "", "source_b": "src/b.py"})
        assert resp6.status_code == 400
        # Not found -> 404
        with patch.object(svc, "compute_diff", side_effect=ValueError("Source not found: src/missing.py")):
            resp7 = client.post("/api/v1/diff/compare", json={"source_a": "src/missing.py", "source_b": "src/b.py"})
            assert resp7.status_code == 404
        # Invalid context fallback
        with patch.object(svc, "compute_diff", return_value=fake_diff) as mock_compute:
            resp8 = client.post("/api/v1/diff/compare", json={"source_a": "src/a.py", "source_b": "src/b.py", "context": "bad"})
            assert resp8.status_code == 200
            # Should have called with default 3
            assert mock_compute.call_args[0][2] == 3
