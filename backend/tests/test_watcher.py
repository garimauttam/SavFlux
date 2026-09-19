"""
test_watcher.py — Unit tests for the upstream watcher (stubbed network).
"""

import pytest

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture()
def _isolated(tmp_path, monkeypatch):
    import app.services.watcher_service as ws
    import app.services.trust_service as ts
    import app.services.notification_service as ns
    for mod in (ws, ts, ns):
        monkeypatch.setattr(mod.settings, "chroma_persist_directory", str(tmp_path))
    return tmp_path


def test_poll_emits_event_once_per_sha(_isolated, monkeypatch):
    import app.services.watcher_service as ws
    import app.services.trust_service as ts
    ts.record_index("https://github.com/x/y", SHA_A)

    monkeypatch.setattr(ts, "upstream_head", lambda url: SHA_A)
    assert ws.poll_once() == []  # in sync → no event

    monkeypatch.setattr(ts, "upstream_head", lambda url: SHA_B)
    events = ws.poll_once()
    assert len(events) == 1
    assert events[0]["upstream_sha"] == SHA_B

    # Same sha again → no duplicate notification
    assert ws.poll_once() == []

    from app.services.notification_service import list_notifications
    notifs = list_notifications(kind="watcher")
    assert len(notifs) == 1
    assert "Upstream moved" in notifs[0]["title"]


def test_poll_handles_unreachable_upstream(_isolated, monkeypatch):
    import app.services.watcher_service as ws
    import app.services.trust_service as ts
    ts.record_index("https://github.com/x/y", SHA_A)
    monkeypatch.setattr(ts, "upstream_head", lambda url: None)
    assert ws.poll_once() == []
    status = ws.get_status()
    assert status["polls"] == 1
    assert status["repos"][0]["upstream"] is None


def test_status_shape(_isolated):
    import app.services.watcher_service as ws
    status = ws.get_status()
    assert status["running"] is False
    assert status["interval_s"] >= 5
