"""
test_bulk.py — P2 Bulk Operations ($0) tests.

Covers:
  - bulk_service bulk_delete/export/stats
  - API GET /bulk/stats, POST /bulk/delete, POST /bulk/export
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def test_bulk_service_stats_mocked(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.bulk_service as svc
    # Mock get_indexed_files to return sample
    fake_files = [
        {"source": "src/a.py", "language": "python", "repo_url": "https://github.com/o/r"},
        {"source": "src/b.js", "language": "javascript", "repo_url": "https://github.com/o/r"},
    ]
    with patch("app.services.bulk_service._get_collection") as mock_coll:
        # Mock collection for stats fallback
        mock_coll.return_value.get.return_value = {"ids": [], "metadatas": []}
        # Patch get_indexed_files via bulk_service's import inside function
        with patch("app.services.retrieval_service.get_indexed_files", return_value=fake_files):
            stats = svc.get_bulk_stats()
            assert stats["total_files"] == 2
            assert stats["by_language"]["python"] == 1
            assert stats["by_repo"]["https://github.com/o/r"] == 2


def test_bulk_delete_mocked(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.bulk_service as svc
    mock_coll = MagicMock()
    # First source found, second not found
    def fake_get(where=None, include=None):
        if where and where.get("source") == "src/a.py":
            return {"ids": ["id1", "id2"], "metadatas": [{}, {}]}
        return {"ids": [], "metadatas": []}
    mock_coll.get.side_effect = fake_get
    mock_coll.delete.return_value = None
    with patch.object(svc, "_get_collection", return_value=mock_coll):
        result = svc.bulk_delete_sources(["src/a.py", "src/notfound.py"])
        assert result["deleted"] == 2
        assert "src/notfound.py" in result["not_found"]
        assert result["requested"] == 2
        # Invalid sources filtered
        result2 = svc.bulk_delete_sources(["", "../bad", "src/a.py"])
        assert result2["requested"] == 1  # only valid one
        # Empty
        result3 = svc.bulk_delete_sources([])
        assert result3["deleted"] == 0


def test_bulk_export_mocked(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.bulk_service as svc
    mock_coll = MagicMock()
    mock_coll.get.return_value = {"documents": ["print('hi')"], "metadatas": [{"language": "python"}]}
    with patch.object(svc, "_get_collection", return_value=mock_coll):
        result = svc.bulk_export_sources(["src/a.py"])
        assert "markdown" in result
        assert result["found"] == 1
        assert result["filename"] == "bulk-export.md"
        # Not found
        mock_coll.get.return_value = {"documents": [], "metadatas": []}
        result2 = svc.bulk_export_sources(["src/missing.py"])
        assert result2["found"] == 0
        assert len(result2["not_found"]) == 1
        # Validation
        with pytest.raises(ValueError):
            svc.bulk_export_sources([])


def test_bulk_api(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.bulk_service as svc
    # Mock stats
    with patch.object(svc, "get_bulk_stats", return_value={"total_files": 5, "by_language": {"python": 5}, "by_repo": {"https://github.com/o/r": 5}, "files": []}):
        resp = client.get("/api/v1/bulk/stats")
        assert resp.status_code == 200
        assert resp.json()["total_files"] == 5
    # Mock delete
    with patch.object(svc, "bulk_delete_sources", return_value={"deleted": 3, "requested": 2, "not_found": [], "errors": [], "sources": ["a"]}) as mock_del:
        resp2 = client.post("/api/v1/bulk/delete", json={"sources": ["src/a.py"]})
        assert resp2.status_code == 200
        assert resp2.json()["deleted"] == 3
        # Bad request
        resp3 = client.post("/api/v1/bulk/delete", json={"sources": []})
        assert resp3.status_code == 400
        resp4 = client.post("/api/v1/bulk/delete", json={"sources": "notarray"})
        assert resp4.status_code == 400
        resp5 = client.post("/api/v1/bulk/delete", json={})
        assert resp5.status_code == 400
        # Too many
        resp6 = client.post("/api/v1/bulk/delete", json={"sources": ["a"] * 101})
        assert resp6.status_code == 400
    # Mock export
    with patch.object(svc, "bulk_export_sources", return_value={"markdown": "# hi", "found": 1, "requested": 1, "not_found": [], "filename": "bulk-export.md"}):
        resp7 = client.post("/api/v1/bulk/export", json={"sources": ["src/a.py"]})
        assert resp7.status_code == 200
        assert resp7.json()["markdown"] == "# hi"
        # Bad export
        resp8 = client.post("/api/v1/bulk/export", json={"sources": []})
        assert resp8.status_code == 400
        resp9 = client.post("/api/v1/bulk/export", json={"sources": ["a"] * 101})
        assert resp9.status_code == 400
    # ValueError from export
    with patch.object(svc, "bulk_export_sources", side_effect=ValueError("No sources")):
        resp10 = client.post("/api/v1/bulk/export", json={"sources": ["src/a.py"]})
        assert resp10.status_code == 400
