"""
activity_service.py — P2 Activity Feed ($0, local aggregation)

Aggregates recent activity from existing local files (no DB, no LLM):
  - Prompt history (prompt_library.json history)
  - Saved prompts (prompt_library.json prompts)
  - Snippets (snippet_vault.json)
  - Shares (share_service)
  - Analytics snapshots (analytics_history.jsonl)
  - Ingested repos/files (via ingestion_service / chroma)
  - Health history (if present)

Returns unified feed sorted by ts desc, with kind filter and limit.
All $0 — reads local files, no deps.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings

settings = get_settings()

def _safe_load_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _safe_read_jsonl(path: Path, limit: int = 20) -> list[dict]:
    try:
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]
    except Exception:
        return []

def get_activity(limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    kind = (kind or "").strip().lower() or None
    base = Path(settings.chroma_persist_directory)
    items: list[dict[str, Any]] = []

    # Prompts history + saved prompts
    prompt_path = base / "prompt_library.json"
    prompt_data = _safe_load_json(prompt_path, {"prompts": [], "history": []})
    for h in prompt_data.get("history", [])[-50:]:
        items.append({
            "id": f"prompt-hist-{h.get('id')}",
            "kind": "prompt_history",
            "title": h.get("text", "")[:80],
            "detail": h.get("text", "")[:200],
            "sub_kind": h.get("kind", "chat"),
            "ts": h.get("ts", 0),
            "meta": {"kind": h.get("kind")},
        })
    for p in prompt_data.get("prompts", [])[-50:]:
        items.append({
            "id": f"prompt-saved-{p.get('id')}",
            "kind": "prompt_saved",
            "title": p.get("title", "")[:80] or p.get("text", "")[:80],
            "detail": p.get("text", "")[:200],
            "sub_kind": p.get("kind", "general"),
            "ts": p.get("created_at", 0),
            "meta": {"tags": p.get("tags", []), "use_count": p.get("use_count", 0)},
        })

    # Snippets
    snippet_path = base / "snippet_vault.json"
    snippet_data = _safe_load_json(snippet_path, {"snippets": []})
    for s in snippet_data.get("snippets", [])[-50:]:
        items.append({
            "id": f"snippet-{s.get('id')}",
            "kind": "snippet",
            "title": s.get("title", "")[:80],
            "detail": s.get("code", "")[:200],
            "sub_kind": s.get("language", "text"),
            "ts": s.get("created_at", 0),
            "meta": {"language": s.get("language"), "tags": s.get("tags", []), "starred": s.get("starred")},
        })

    # Shares
    try:
        from app.services.share_service import list_shares as _list_shares
        shares = _list_shares(limit=20, repo_url=None)
        for sh in shares:
            # share_service stores {id, question, repo_url, created_at}
            ts = sh.get("created_at", 0)
            # created_at may be iso string; convert if needed
            if isinstance(ts, str):
                try:
                    # try parse int from string or iso
                    ts = int(time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")))
                except Exception:
                    ts = 0
            items.append({
                "id": f"share-{sh.get('id')}",
                "kind": "share",
                "title": sh.get("question", "")[:80] or "Shared chat",
                "detail": sh.get("answer", "")[:200] if "answer" in sh else sh.get("question", "")[:200],
                "sub_kind": "share",
                "ts": ts,
                "meta": {"url": sh.get("url"), "repo_url": sh.get("repo_url")},
            })
    except Exception:
        pass

    # Analytics snapshots (as activity)
    analytics_path = base / "analytics_history.jsonl"
    for snap in _safe_read_jsonl(analytics_path, limit=20):
        items.append({
            "id": f"analytics-{snap.get('ts')}",
            "kind": "analytics",
            "title": f"Analytics snapshot — {snap.get('total_tokens', 0)} tokens, {snap.get('llm_calls', 0)} calls",
            "detail": f"latency {snap.get('avg_latency_ms', 0)}ms · health {snap.get('health_avg', '—')}",
            "sub_kind": "snapshot",
            "ts": snap.get("ts", 0),
            "meta": snap,
        })

    # Ingested repos (from repos file if exists, else via service)
    try:
        # Try to get repos via ingestion service's list
        import asyncio
        from app.services.ingestion_service import get_indexed_repos  # type: ignore
        try:
            repos = asyncio.run(get_indexed_repos())  # type: ignore
        except Exception:
            repos = []
        for r in repos[-20:]:
            # r may have repo_url, file_count, last_indexed
            ts = r.get("last_indexed") or r.get("indexed_at") or 0
            if isinstance(ts, str):
                try:
                    ts = int(time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")))
                except Exception:
                    ts = 0
            items.append({
                "id": f"ingest-{r.get('repo_url', '')[:30]}",
                "kind": "ingest",
                "title": r.get("repo_url", "Repo indexed")[:80],
                "detail": f"{r.get('file_count', r.get('chunk_count', 0))} files/chunks",
                "sub_kind": "repo",
                "ts": ts or int(time.time()) - 10,  # fallback to now-10 if ts missing
                "meta": r,
            })
    except Exception:
        pass

    # Sort by ts desc, filter kind if requested
    items = [it for it in items if it.get("ts")]
    if kind:
        items = [it for it in items if it.get("kind") == kind]
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return items[:limit]

def clear_activity(kind: str | None = None) -> int:
    """Clear activity by kind (or all if kind is None). Returns deleted count."""
    # For now, clear by delegating to underlying services where possible
    n = 0
    kind = (kind or "").strip().lower() or None
    base = Path(settings.chroma_persist_directory)
    if kind is None or kind == "prompt_history":
        try:
            from app.services.prompt_service import clear_history
            n += clear_history()
        except Exception:
            pass
    if kind is None or kind == "prompt_saved":
        try:
            from app.services.prompt_service import clear_prompts
            n += clear_prompts()
        except Exception:
            pass
    if kind is None or kind == "snippet":
        try:
            from app.services.snippet_service import clear_snippets
            n += clear_snippets()
        except Exception:
            pass
    if kind is None or kind == "share":
        try:
            from app.services.share_service import list_shares, delete_share
            shares = list_shares(limit=100, repo_url=None)
            for sh in shares:
                try:
                    delete_share(sh.get("id"))
                    n += 1
                except Exception:
                    pass
        except Exception:
            pass
    if kind is None or kind == "analytics":
        try:
            from app.services.analytics_service import clear_history
            n += clear_history()
        except Exception:
            pass
    if kind is None or kind == "ingest":
        # Don't delete ingest by default — just count
        pass
    return n
