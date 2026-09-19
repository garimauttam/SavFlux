"""
test_snippets.py — P2 Snippet Vault ($0, local file) tests.

Covers:
  - snippet_service CRUD + star/use/search
  - API GET/POST/DELETE /snippets
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_snippet_service_crud(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.snippet_service as svc
    fake_path = tmp_path / "snippet_vault.json"
    with patch.object(svc, "_snippet_path", return_value=fake_path):
        assert svc.list_snippets() == []
        # Create
        s1 = svc.create_snippet("def hello():\n    print('hi')", title="Hello", language="python", tags=["demo"], source="src/app.py:10")
        assert s1["id"]
        assert s1["language"] == "python"
        assert s1["starred"] is False
        # List
        lst = svc.list_snippets()
        assert len(lst) == 1
        # Search
        found = svc.search_snippets("hello")
        assert len(found) == 1
        # Star toggle
        svc.toggle_star(s1["id"])
        assert svc.list_snippets()[0]["starred"] is True
        svc.toggle_star(s1["id"])
        assert svc.list_snippets()[0]["starred"] is False
        # Use
        svc.use_snippet(s1["id"])
        assert svc.list_snippets()[0]["use_count"] == 1
        # Language filter
        s2 = svc.create_snippet("console.log('hi')", language="javascript", tags=[])
        assert len(svc.list_snippets(language="python")) == 1
        assert len(svc.list_snippets(language="javascript")) == 1
        # Tag filter
        assert len(svc.list_snippets(tag="demo")) == 1
        # Starred filter
        svc.toggle_star(s1["id"])
        assert len(svc.list_snippets(starred=True)) == 1
        # Delete
        assert svc.delete_snippet(s1["id"]) is True
        assert len(svc.list_snippets()) == 1
        assert svc.delete_snippet("nope") is False
        # Clear
        n = svc.clear_snippets()
        assert n == 1
        assert svc.list_snippets() == []
        # Validation
        with pytest.raises(ValueError):
            svc.create_snippet("", title="empty")
        with pytest.raises(ValueError):
            svc.create_snippet("a" * 30000)


def test_snippets_api(client, tmp_path, monkeypatch):
    import app.services.snippet_service as svc
    fake_path = tmp_path / "snippets_api.json"
    with patch.object(svc, "_snippet_path", return_value=fake_path):
        # Create
        resp = client.post("/api/v1/snippets", json={"code": "print('hello')", "title": "Hello", "language": "python", "tags": ["test"]})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["code"] == "print('hello')"
        sid = data["id"]
        # List
        resp2 = client.get("/api/v1/snippets?limit=10")
        assert resp2.status_code == 200
        assert resp2.json()["total"] == 1
        # Search
        resp3 = client.get("/api/v1/snippets?q=hello")
        assert resp3.status_code == 200
        assert len(resp3.json()["snippets"]) == 1
        # Filter language
        resp4 = client.get("/api/v1/snippets?language=python")
        assert resp4.status_code == 200
        assert resp4.json()["total"] == 1
        # Star
        resp5 = client.post(f"/api/v1/snippets/{sid}/star")
        assert resp5.status_code == 200
        assert resp5.json()["starred"] is True
        # Use
        resp6 = client.post(f"/api/v1/snippets/{sid}/use")
        assert resp6.status_code == 200
        assert resp6.json()["use_count"] == 1
        # Delete
        resp7 = client.delete(f"/api/v1/snippets/{sid}")
        assert resp7.status_code == 200
        # Delete missing
        resp8 = client.delete("/api/v1/snippets/notfound123")
        assert resp8.status_code == 404
        # Clear (empty now)
        resp9 = client.delete("/api/v1/snippets")
        assert resp9.status_code == 200
        # Bad create
        resp10 = client.post("/api/v1/snippets", json={"code": ""})
        assert resp10.status_code == 400
        resp11 = client.post("/api/v1/snippets", json={"code": "hi", "tags": "notarray"})
        assert resp11.status_code == 400
