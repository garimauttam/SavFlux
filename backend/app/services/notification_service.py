"""
notification_service.py — P2 Notifications Center ($0, local file)

Stores notifications in chroma_data/notifications.json (local file, no DB).
Each notification: {id, title, message, kind, level, read, ts, meta}

Kinds: ingest, review, write, chat, system, health, watcher, bulk, prompt, snippet, activity, file_tree, diff, general
Levels: info, success, warning, error

All $0 — flat file, 500 cap, thread-safe.
"""

from __future__ import annotations

import json
import time
import uuid
import threading
from pathlib import Path
from typing import Any

from app.core.paths import data_file

_LOCK = threading.Lock()

ALLOWED_KINDS = {"ingest", "review", "write", "chat", "system", "health", "watcher", "bulk", "prompt", "snippet", "activity", "file_tree", "diff", "explorer", "general"}
ALLOWED_LEVELS = {"info", "success", "warning", "error"}

def _notif_path() -> Path:
    """Resolved lazily so a changed data dir takes effect immediately."""
    return data_file("notifications.json")

def _load() -> dict[str, Any]:
    path = _notif_path()
    if not path.is_file():
        return {"notifications": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "notifications" not in data:
            data["notifications"] = []
        return data
    except Exception:
        return {"notifications": []}

def _save(data: dict[str, Any]) -> None:
    path = _notif_path()
    with _LOCK:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def list_notifications(limit: int = 100, kind: str | None = None, unread_only: bool = False) -> list[dict[str, Any]]:
    data = _load()
    notifs = data.get("notifications", [])
    if kind:
        kind_lower = kind.lower()
        notifs = [n for n in notifs if n.get("kind", "").lower() == kind_lower]
    if unread_only:
        notifs = [n for n in notifs if not n.get("read", False)]
    notifs = sorted(notifs, key=lambda x: x.get("ts", 0), reverse=True)
    return notifs[: max(1, min(limit, 500))]

def create_notification(title: str, message: str = "", kind: str = "general", level: str = "info", meta: dict | None = None) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    if len(title) > 200:
        title = title[:200]
    message = (message or "").strip()[:2000]
    kind = (kind or "general").strip().lower()
    if kind not in ALLOWED_KINDS:
        kind = "general"
    level = (level or "info").strip().lower()
    if level not in ALLOWED_LEVELS:
        level = "info"
    notif = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "message": message,
        "kind": kind,
        "level": level,
        "read": False,
        "ts": int(time.time()),
        "meta": meta or {},
    }
    data = _load()
    data["notifications"].append(notif)
    if len(data["notifications"]) > 500:
        # Keep unread + newest
        data["notifications"] = sorted(data["notifications"], key=lambda x: (not x.get("read", False), x.get("ts", 0)), reverse=True)[:500]
    _save(data)
    return notif

def mark_read(notif_id: str, read: bool = True) -> dict[str, Any] | None:
    data = _load()
    for n in data["notifications"]:
        if n["id"] == notif_id:
            n["read"] = bool(read)
            _save(data)
            return n
    return None

def mark_all_read() -> int:
    data = _load()
    n = 0
    for notif in data["notifications"]:
        if not notif.get("read", False):
            notif["read"] = True
            n += 1
    if n:
        _save(data)
    return n

def delete_notification(notif_id: str) -> bool:
    data = _load()
    before = len(data["notifications"])
    data["notifications"] = [n for n in data["notifications"] if n["id"] != notif_id]
    if len(data["notifications"]) == before:
        return False
    _save(data)
    return True

def clear_notifications(kind: str | None = None) -> int:
    data = _load()
    if kind:
        kind_lower = kind.lower()
        before = len(data["notifications"])
        data["notifications"] = [n for n in data["notifications"] if n.get("kind", "").lower() != kind_lower]
        deleted = before - len(data["notifications"])
    else:
        deleted = len(data["notifications"])
        data["notifications"] = []
    _save(data)
    return deleted

def get_unread_count() -> int:
    data = _load()
    return sum(1 for n in data.get("notifications", []) if not n.get("read", False))
