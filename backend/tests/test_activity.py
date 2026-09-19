"""
test_activity.py — P2 Activity Feed ($0) tests.

Covers:
  - activity_service aggregation + kind filter + clear
  - API GET/DELETE /activity
"""

import json
import time
from pathlib import Path
from unittest.mock import patch


def test_activity_service(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.activity_service as svc
    # Create fake prompt and snippet data via their services
    import app.services.prompt_service as ps
    import app.services.snippet_service as ss
    fake_prompt_path = tmp_path / "prompt_library.json"
    fake_snippet_path = tmp_path / "snippet_vault.json"
    fake_analytics_path = tmp_path / "analytics_history.jsonl"
    # Patch paths in those services via patch.object on _prompt_path etc.
    with patch.object(ps, "_prompt_path", return_value=fake_prompt_path), \
         patch.object(ss, "_snippet_path", return_value=fake_snippet_path):
        # Create some data
        ps.create_prompt("What is auth?", title="Auth Q", tags=["a"], kind="chat")
        ps.record_query("Recent query test", kind="chat")
        ss.create_snippet("print('hi')", title="Hi", language="python", tags=["demo"])
        # Also write analytics
        fake_analytics_path.write_text(json.dumps({"ts": int(time.time()), "total_tokens": 100, "llm_calls": 2, "avg_latency_ms": 123, "health_avg": 80}) + "\n", encoding="utf-8")
        # Now get activity with patched base path
        # Need to patch activity_service's base path reading - it uses settings.chroma_persist_directory which is tmp_path via env
        # But its internal functions reference Path(settings...) at call time, so should read from tmp_path now
        # Also analytics path will be from base
        items = svc.get_activity(limit=20)
        assert len(items) >= 3  # at least prompt_saved, prompt_history, snippet
        # Kind filter
        only_snippets = svc.get_activity(limit=20, kind="snippet")
        assert all(i["kind"] == "snippet" for i in only_snippets)
        assert len(only_snippets) >= 1
        # Clear snippet via activity clear
        n = svc.clear_activity(kind="snippet")
        assert n >= 1
        assert len(svc.get_activity(limit=20, kind="snippet")) == 0
        # Clear all
        n2 = svc.clear_activity(kind=None)
        assert n2 >= 0


def test_activity_api(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.prompt_service as ps
    import app.services.snippet_service as ss
    import app.services.activity_service as svc
    fake_prompt_path = tmp_path / "act_prompt.json"
    fake_snippet_path = tmp_path / "act_snippet.json"
    with patch.object(ps, "_prompt_path", return_value=fake_prompt_path), \
         patch.object(ss, "_snippet_path", return_value=fake_snippet_path), \
         patch.object(svc, "get_activity", wraps=svc.get_activity) as mock_get:
        # Use real service but with patched underlying files via ps/ss patch
        # Create data
        ps.create_prompt("Test prompt for activity", title="Test", tags=[], kind="chat")
        # GET activity
        resp = client.get("/api/v1/activity?limit=10")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)
        # Filter kind
        resp2 = client.get("/api/v1/activity?kind=snippet&limit=10")
        assert resp2.status_code == 200
        # Invalid kind
        resp3 = client.get("/api/v1/activity?kind=invalid")
        assert resp3.status_code == 400
        # Clear kind
        resp4 = client.delete("/api/v1/activity?kind=snippet")
        assert resp4.status_code == 200
        assert resp4.json()["status"] == "cleared"
        # Clear all
        resp5 = client.delete("/api/v1/activity")
        assert resp5.status_code == 200
        # Invalid clear kind
        resp6 = client.delete("/api/v1/activity?kind=bad")
        assert resp6.status_code == 400
