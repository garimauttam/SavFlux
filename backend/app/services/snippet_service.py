"""
snippet_service.py — P2 Snippet Vault ($0, local file)

Stores code snippets in chroma_data/snippet_vault.json (local file, no DB).
Used for:
  - Saving code snippets from chat/review/write with language + tags
  - Star/favorite, search, filter by language/tag
  - Quick copy/use via API

All $0 — flat file, 20KB typical, no deps. Thread-safe via Lock.
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

def _snippet_path() -> Path:
    p = Path(settings.chroma_persist_directory) / "snippet_vault.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p

def _load() -> dict[str, Any]:
    path = _snippet_path()
    if not path.is_file():
        return {"snippets": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "snippets" not in data:
            data["snippets"] = []
        return data
    except Exception:
        return {"snippets": []}

def _save(data: dict[str, Any]) -> None:
    path = _snippet_path()
    with _LOCK:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def list_snippets(limit: int = 100, tag: str | None = None, language: str | None = None, starred: bool | None = None) -> list[dict[str, Any]]:
    data = _load()
    snippets = data.get("snippets", [])
    if tag:
        tag_lower = tag.lower()
        snippets = [s for s in snippets if any(t.lower() == tag_lower for t in s.get("tags", []))]
    if language:
        lang_lower = language.lower()
        snippets = [s for s in snippets if s.get("language", "").lower() == lang_lower]
    if starred is not None:
        snippets = [s for s in snippets if bool(s.get("starred")) == starred]
    snippets = sorted(snippets, key=lambda x: (x.get("starred", False), x.get("created_at", 0)), reverse=True)
    return snippets[: max(1, min(limit, 500))]

def create_snippet(code: str, title: str | None = None, language: str | None = None, tags: list[str] | None = None, source: str | None = None) -> dict[str, Any]:
    if not code or not code.strip():
        raise ValueError("Code is required")
    if len(code) > 20000:
        raise ValueError("Snippet too long (max 20000 chars)")
    code = code.strip("\n")
    title = (title or code.splitlines()[0][:60] if code else "Untitled").strip() or "Untitled"
    if len(title) > 120:
        title = title[:120]
    tags = [t.strip() for t in (tags or []) if t.strip()][:10]
    language = (language or "text").strip().lower()[:20]
    source = (source or "").strip()[:500]
    snippet = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "code": code,
        "language": language,
        "tags": tags,
        "source": source,
        "starred": False,
        "created_at": int(time.time()),
        "use_count": 0,
    }
    data = _load()
    data["snippets"].append(snippet)
    if len(data["snippets"]) > 500:
        # Keep starred + most used + newest
        data["snippets"] = sorted(data["snippets"], key=lambda x: (x.get("starred", False), x.get("use_count", 0), x.get("created_at", 0)), reverse=True)[:500]
    _save(data)
    return snippet

def delete_snippet(snippet_id: str) -> bool:
    data = _load()
    before = len(data["snippets"])
    data["snippets"] = [s for s in data["snippets"] if s["id"] != snippet_id]
    if len(data["snippets"]) == before:
        return False
    _save(data)
    return True

def toggle_star(snippet_id: str) -> dict[str, Any] | None:
    data = _load()
    for s in data["snippets"]:
        if s["id"] == snippet_id:
            s["starred"] = not bool(s.get("starred"))
            _save(data)
            return s
    return None

def use_snippet(snippet_id: str) -> dict[str, Any] | None:
    data = _load()
    for s in data["snippets"]:
        if s["id"] == snippet_id:
            s["use_count"] = s.get("use_count", 0) + 1
            s["last_used"] = int(time.time())
            _save(data)
            return s
    return None

def search_snippets(query: str, limit: int = 20) -> list[dict[str, Any]]:
    if not query or not query.strip():
        return list_snippets(limit=limit)
    q = query.lower().strip()
    data = _load()
    scored: list[tuple[int, dict]] = []
    for s in data.get("snippets", []):
        hay = f"{s.get('title','')} {s.get('code','')[:500]} {' '.join(s.get('tags',[]))} {s.get('language','')} {s.get('source','')}".lower()
        score = 0
        if q == hay.strip():
            score = 100
        elif hay.startswith(q):
            score = 80
        elif q in hay:
            score = 60
        else:
            qi = 0
            for ch in hay:
                if qi < len(q) and ch == q[qi]:
                    qi += 1
            if qi == len(q):
                score = 30
            else:
                continue
        score += min(s.get("use_count", 0), 10)
        if s.get("starred"):
            score += 5
        scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:limit]]

def clear_snippets() -> int:
    data = _load()
    n = len(data.get("snippets", []))
    data["snippets"] = []
    _save(data)
    return n
