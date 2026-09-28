"""
share_service.py — Share-link storage ($0, local file).

Stores shared Q&A snapshots in chroma_data/shared_links.json (local file).
Each share: {id, question, answer, sources, repo_url, created_at, url}

The frontend serves them at /s/{id} (see ShareView.tsx + App.tsx routing).
activity_service reads shares via list_shares(), so that signature and the
{id, question, answer, repo_url, created_at, url} shape are a hard contract.

All $0 — flat file, 500 cap, thread-safe via Lock.
"""

from __future__ import annotations

import json
import time
import uuid
import threading
from pathlib import Path
from typing import Any

from app.core.paths import data_file
from app.core.config import get_settings
from app.core.tenant import current_user_id

_LOCK = threading.Lock()
_MAX_SHARES = 500


def _share_path() -> Path:
    """Resolved lazily so a changed data dir takes effect immediately."""
    return data_file("shared_links.json")


def _load() -> dict[str, Any]:
    try:
        raw = _share_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("shares", [])
            return data
    except Exception:
        pass
    return {"shares": []}


def _save(data: dict[str, Any]) -> None:
    _share_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def create_share(
    question: str,
    answer: str = "",
    sources: list[dict] | None = None,
    repo_url: str | None = None,
    ledger: dict | None = None,
    chat_history: list[dict] | None = None,
) -> dict[str, Any]:
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")
    if len(question) > 2000:
        raise ValueError("question too long (max 2000 chars)")
    if len(answer or "") > 50000:
        raise ValueError("answer too long (max 50000 chars)")
    share_id = uuid.uuid4().hex[:10]
    share = {
        "id": share_id,
        "question": question,
        "answer": answer or "",
        "sources": [s for s in (sources or []) if isinstance(s, dict)][:20],
        "repo_url": repo_url,
        "ledger": ledger if isinstance(ledger, dict) else None,
        "chat_history": [m for m in (chat_history or []) if isinstance(m, dict)][:20],
        "created_at": time.time(),
        "url": f"/s/{share_id}",
    }
    with _LOCK:
        data = _load()
        data["shares"].append(share)
        data["shares"] = data["shares"][-_MAX_SHARES:]
        _save(data)
    return share


def get_share(share_id: str) -> dict[str, Any] | None:
    """Resolve a capability URL within its owner, without exposing share lists."""
    if not isinstance(share_id, str) or not share_id.isalnum() or len(share_id) != 10:
        return None
    if current_user_id():
        directories = [data_file("shared_links.json").parent]
    else:
        # Public links intentionally cross the auth boundary, but only the
        # unguessable share ID is queried. Private list/delete endpoints remain
        # scoped to the authenticated owner's data directory.
        root = Path(get_settings().chroma_persist_directory) / "users"
        try:
            directories = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            directories = []
    for directory in directories:
        try:
            payload = json.loads((directory / "shared_links.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for share in payload.get("shares", []) if isinstance(payload, dict) else []:
            if share.get("id") == share_id:
                return share
    return None


def list_shares(limit: int = 20, repo_url: str | None = None) -> list[dict[str, Any]]:
    shares = _load().get("shares", [])
    if repo_url:
        shares = [s for s in shares if s.get("repo_url") == repo_url]
    shares = sorted(shares, key=lambda s: s.get("created_at", 0), reverse=True)
    return shares[: max(1, min(limit, _MAX_SHARES))]


def delete_share(share_id: str) -> bool:
    with _LOCK:
        data = _load()
        before = len(data["shares"])
        data["shares"] = [s for s in data["shares"] if s.get("id") != share_id]
        if len(data["shares"]) == before:
            return False
        _save(data)
        return True
