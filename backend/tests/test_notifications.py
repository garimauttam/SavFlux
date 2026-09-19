"""
test_notifications.py — P2 Notifications Center ($0) tests.

Covers:
  - notification_service CRUD + read/unread + clear
  - API GET/POST/PATCH/DELETE /notifications
"""

from unittest.mock import patch

import pytest


def test_notification_service(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.notification_service as svc
    fake_path = tmp_path / "notifications.json"
    with patch.object(svc, "_notif_path", return_value=fake_path):
        assert svc.list_notifications() == []
        assert svc.get_unread_count() == 0
        # Create
        n1 = svc.create_notification("Test", message="hello", kind="ingest", level="success", meta={"id": 1})
        assert n1["id"]
        assert n1["read"] is False
        assert n1["kind"] == "ingest"
        assert n1["level"] == "success"
        # Create with invalid kind/level -> fallback
        n2 = svc.create_notification("Bad kind", kind="badkind", level="badlevel")
        assert n2["kind"] == "general"
        assert n2["level"] == "info"
        # List
        lst = svc.list_notifications()
        assert len(lst) == 2
        assert svc.get_unread_count() == 2
        # Filter kind
        only_ingest = svc.list_notifications(kind="ingest")
        assert len(only_ingest) == 1
        # Unread only
        svc.mark_read(n1["id"], read=True)
        assert svc.get_unread_count() == 1
        assert len(svc.list_notifications(unread_only=True)) == 1
        # Mark all read
        n = svc.mark_all_read()
        assert n == 1
        assert svc.get_unread_count() == 0
        # Delete
        assert svc.delete_notification(n1["id"]) is True
        assert len(svc.list_notifications()) == 1
        assert svc.delete_notification("notfound") is False
        # Clear by kind
        svc.create_notification("Another", kind="review")
        assert svc.clear_notifications(kind="review") == 1
        # Clear all
        assert svc.clear_notifications() == 1
        assert svc.list_notifications() == []
        # Validation
        try:
            svc.create_notification("", message="empty title")
            assert False, "should raise"
        except ValueError:
            pass


def test_notifications_api(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIRECTORY", str(tmp_path))
    import app.services.notification_service as svc
    fake_path = tmp_path / "notif_api.json"
    with patch.object(svc, "_notif_path", return_value=fake_path):
        # Create
        resp = client.post("/api/v1/notifications", json={"title": "Hello", "message": "world", "kind": "chat", "level": "info"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["title"] == "Hello"
        nid = data["id"]
        # Bad create
        resp2 = client.post("/api/v1/notifications", json={"title": ""})
        assert resp2.status_code == 400
        resp3 = client.post("/api/v1/notifications", json={"title": "Hi", "meta": "notobject"})
        assert resp3.status_code == 400
        # List
        resp4 = client.get("/api/v1/notifications?limit=10")
        assert resp4.status_code == 200
        assert resp4.json()["total"] == 1
        # Filter unread
        resp5 = client.get("/api/v1/notifications?unread_only=true")
        assert resp5.status_code == 200
        assert resp5.json()["total"] == 1
        # Unread count
        resp6 = client.get("/api/v1/notifications/unread-count")
        assert resp6.status_code == 200
        assert resp6.json()["unread"] == 1
        # Mark read
        resp7 = client.patch(f"/api/v1/notifications/{nid}/read", json={"read": True})
        assert resp7.status_code == 200
        assert resp7.json()["read"] is True
        # Bad read body
        resp8 = client.patch(f"/api/v1/notifications/{nid}/read", json={"read": "notbool"})
        assert resp8.status_code == 400
        # Not found
        resp9 = client.patch("/api/v1/notifications/notfound/read", json={"read": True})
        assert resp9.status_code == 404
        # Mark all read
        resp10 = client.post("/api/v1/notifications/read-all")
        assert resp10.status_code == 200
        # Delete
        resp11 = client.delete(f"/api/v1/notifications/{nid}")
        assert resp11.status_code == 200
        resp12 = client.delete("/api/v1/notifications/notfound")
        assert resp12.status_code == 404
        # Clear
        client.post("/api/v1/notifications", json={"title": "To clear", "kind": "general"})
        resp13 = client.delete("/api/v1/notifications")
        assert resp13.status_code == 200
        assert resp13.json()["deleted"] >= 1
