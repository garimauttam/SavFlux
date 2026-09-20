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
    items: list[dict[str, Any]] = []

    # Prompts history + saved prompts.
    #
    # WHY ASK prompt_service FOR THE PATH instead of rebuilding
    # `data_dir() / "prompt_library.json"` here?
    # The filename is prompt_service's private detail. Duplicating it meant the
    # two modules could drift (and made the path impossible to redirect in
    # tests, which patch `_library_path`). Delegating keeps exactly one
    # definition of "where do prompts live".
    from app.services.prompt_service import _library_path
    prompt_data = _safe_load_json(_library_path(), {"prompts": [], "history": []})
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

    # Snippets — path owned by snippet_service (see note above).
    from app.services.snippet_service import _snippet_path
    snippet_data = _safe_load_json(_snippet_path(), {"snippets": []})
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

    # Analytics snapshots (as activity) — path owned by analytics_service.
    from app.services.analytics_service import _history_path
    for snap in _safe_read_jsonl(_history_path(), limit=20):
        items.append({
            "id": f"analytics-{snap.get('ts')}",
            "kind": "analytics",
            "title": f"Analytics snapshot — {snap.get('total_tokens', 0)} tokens, {snap.get('llm_calls', 0)} calls",
            "detail": f"latency {snap.get('avg_latency_ms', 0)}ms · health {snap.get('health_avg', '—')}",
            "sub_kind": "snapshot",
            "ts": snap.get("ts", 0),
            "meta": snap,
        })

    # Ingested repos.
    #
    # Uses the *sync* entry point: get_activity() is called from a worker thread
    # (via asyncio.to_thread in the route), where an event loop is already
    # running on the main thread. The previous `asyncio.run(...)` call raised
    # RuntimeError in that context and the error was swallowed, so indexed repos
    # never showed up in the feed outside of tests.
    try:
        from app.services.ingestion_service import get_indexed_repos_sync
        try:
            repos = get_indexed_repos_sync()
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
    """Clear activity by kind (or all if kind is None). Returns deleted count.

    Every branch delegates to the service that owns the data, so clearing stays
    correct even when a service changes where or how it persists.
    """
    n = 0
    kind = (kind or "").strip().lower() or None
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


def file_timeline(repo_url: str, file: str, limit: int = 30) -> dict[str, Any]:
    """Per-file timeline for the Time Machine: index record + git history.

    Merges the trust-ledger index entry (when this file's repo was indexed,
    from which commit) with the git commit timeline for the file itself.
    Git failures degrade to index-only data — never raise for missing history.
    """
    from app.services.history_service import split_source
    url, rel = split_source(file)
    url = url or (repo_url or "").strip()

    index_record: dict[str, Any] | None = None
    try:
        from app.services.trust_service import _load as _trust_load
        row = _trust_load().get("repos", {}).get(url, {})
        if row:
            index_record = {
                "indexed_sha": row.get("indexed_sha"),
                "indexed_at": row.get("indexed_at"),
                "files_indexed": row.get("files_indexed", 0),
            }
    except Exception:
        pass

    commits: list[dict[str, Any]] = []
    resolved_path = rel
    try:
        from app.services.history_service import file_timeline as _git_timeline
        data = _git_timeline(url, rel, limit)
        commits = data.get("commits", [])
        resolved_path = data.get("path", rel)
    except Exception:
        pass

    return {
        "repo_url": url,
        "file": file,
        "path": resolved_path,
        "index": index_record,
        "commits": commits,
        "total": len(commits),
    }
