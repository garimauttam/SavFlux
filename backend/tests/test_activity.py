"""
test_activity.py — Activity Feed ($0) tests.

Covers:
  - activity_service aggregation + kind filter + clear
  - API GET/DELETE /activity

All state is redirected into a per-test tmp dir by the `isolated_data_dir`
fixture (conftest.py), so these tests never touch a developer's real
chroma_data/ directory.
"""

import json
import time


def test_activity_service(isolated_data_dir):
    """Aggregation pulls from every underlying store, and clear() delegates."""
    import app.services.activity_service as svc
    import app.services.prompt_service as ps
    import app.services.snippet_service as ss
    import app.services.analytics_service as ans

    # Seed each store through its own public API — no path guessing.
    ps.create_prompt("What is auth?", title="Auth Q", tags=["a"], kind="chat")
    ps.record_history("Recent query test", kind="chat")
    ss.create_snippet("print('hi')", title="Hi", language="python", tags=["demo"])
    ans._history_path().write_text(
        json.dumps({
            "ts": int(time.time()),
            "total_tokens": 100,
            "llm_calls": 2,
            "avg_latency_ms": 123,
            "health_avg": 80,
        }) + "\n",
        encoding="utf-8",
    )

    items = svc.get_activity(limit=20)
    kinds = {item["kind"] for item in items}
    # The feed must surface all four seeded sources, not just whichever store
    # happened to share a path with the aggregator.
    assert {"prompt_saved", "prompt_history", "snippet", "analytics"} <= kinds

    only_snippets = svc.get_activity(limit=20, kind="snippet")
    assert only_snippets, "snippet filter returned nothing"
    assert all(item["kind"] == "snippet" for item in only_snippets)

    # Feed rows are newest-first.
    timestamps = [item.get("ts", 0) for item in items]
    assert timestamps == sorted(timestamps, reverse=True)

    assert svc.clear_activity(kind="snippet") >= 1
    assert svc.get_activity(limit=20, kind="snippet") == []
    # Clearing one kind must not wipe the others.
    assert svc.get_activity(limit=20, kind="prompt_saved")

    assert svc.clear_activity(kind=None) >= 1
    assert svc.get_activity(limit=20, kind="prompt_saved") == []


def test_activity_api(client, isolated_data_dir):
    """The route validates `kind` and reports what it cleared."""
    import app.services.prompt_service as ps

    ps.create_prompt("Test prompt for activity", title="Test", tags=[], kind="chat")

    resp = client.get("/api/v1/activity?limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["items"], list)
    assert body["total"] == len(body["items"])
    assert any(item["kind"] == "prompt_saved" for item in body["items"])

    assert client.get("/api/v1/activity?kind=snippet&limit=10").status_code == 200
    assert client.get("/api/v1/activity?kind=invalid").status_code == 400

    cleared = client.delete("/api/v1/activity?kind=snippet")
    assert cleared.status_code == 200
    assert cleared.json()["status"] == "cleared"

    assert client.delete("/api/v1/activity").status_code == 200
    assert client.delete("/api/v1/activity?kind=bad").status_code == 400
