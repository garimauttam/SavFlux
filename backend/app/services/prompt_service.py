"""
prompt_service.py — Prompt Library storage ($0, local file).

Stores saved prompts + usage history in chroma_data/prompt_library.json:
  { "prompts": [...], "history": [...] }

Saved prompt: {id, title, text, kind, tags, use_count, created_at}
History row:  {id, text, kind, ts}   (every prompt actually sent to chat)

Activity feed reads this file directly, so the schema is a hard contract:
  - prompts[].created_at is epoch seconds (float)
  - history[].ts is epoch seconds (float)

All $0 — flat file, 500 cap each, thread-safe via Lock.
"""

from __future__ import annotations

import json
import time
import uuid
import threading
from pathlib import Path
from typing import Any

from app.core.config import get_settings

settings = get_settings()
_LOCK = threading.Lock()
_MAX_ITEMS = 500


def _library_path() -> Path:
    p = Path(settings.chroma_persist_directory) / "prompt_library.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def _load() -> dict[str, Any]:
    try:
        raw = _library_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("prompts", [])
            data.setdefault("history", [])
            return data
    except Exception:
        pass
    return {"prompts": [], "history": []}


def _save(data: dict[str, Any]) -> None:
    _library_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Saved prompts ─────────────────────────────────────────────────────────────

def list_prompts(limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
    data = _load()
    prompts = data.get("prompts", [])
    if kind:
        prompts = [p for p in prompts if p.get("kind") == kind]
    prompts = sorted(prompts, key=lambda p: p.get("created_at", 0), reverse=True)
    return prompts[: max(1, min(limit, _MAX_ITEMS))]


def create_prompt(
    text: str,
    title: str | None = None,
    kind: str = "general",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required")
    if len(text) > 4000:
        raise ValueError("text too long (max 4000 chars)")
    prompt = {
        "id": uuid.uuid4().hex[:12],
        "title": (title or "").strip() or text[:60],
        "text": text,
        "kind": (kind or "general").strip() or "general",
        "tags": [t for t in (tags or []) if isinstance(t, str)][:10],
        "use_count": 0,
        "created_at": time.time(),
    }
    with _LOCK:
        data = _load()
        data["prompts"].append(prompt)
        data["prompts"] = data["prompts"][-_MAX_ITEMS:]
        _save(data)
    return prompt


def delete_prompt(prompt_id: str) -> bool:
    with _LOCK:
        data = _load()
        before = len(data["prompts"])
        data["prompts"] = [p for p in data["prompts"] if p.get("id") != prompt_id]
        if len(data["prompts"]) == before:
            return False
        _save(data)
        return True


def use_prompt(prompt_id: str) -> dict[str, Any] | None:
    """Bump use_count and append a history row. Returns the prompt or None."""
    with _LOCK:
        data = _load()
        for p in data["prompts"]:
            if p.get("id") == prompt_id:
                p["use_count"] = int(p.get("use_count", 0)) + 1
                data["history"].append({
                    "id": uuid.uuid4().hex[:12],
                    "text": p.get("text", ""),
                    "kind": p.get("kind", "general"),
                    "ts": time.time(),
                })
                data["history"] = data["history"][-_MAX_ITEMS:]
                _save(data)
                return p
        return None


def record_history(text: str, kind: str = "chat") -> dict[str, Any]:
    """Record an ad-hoc (unsaved) prompt send. Used by the activity feed."""
    row = {
        "id": uuid.uuid4().hex[:12],
        "text": (text or "")[:500],
        "kind": kind or "chat",
        "ts": time.time(),
    }
    with _LOCK:
        data = _load()
        data["history"].append(row)
        data["history"] = data["history"][-_MAX_ITEMS:]
        _save(data)
    return row


def list_history(limit: int = 50) -> list[dict[str, Any]]:
    data = _load()
    rows = sorted(data.get("history", []), key=lambda h: h.get("ts", 0), reverse=True)
    return rows[: max(1, min(limit, _MAX_ITEMS))]


def clear_history() -> int:
    with _LOCK:
        data = _load()
        n = len(data.get("history", []))
        data["history"] = []
        _save(data)
        return n


def clear_prompts() -> int:
    with _LOCK:
        data = _load()
        n = len(data.get("prompts", []))
        data["prompts"] = []
        _save(data)
        return n
