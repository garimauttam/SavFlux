"""
bulk_service.py — P2 Bulk Operations ($0, local Chroma)

Provides bulk file operations on the indexed store:
  - bulk_delete_sources(sources: list[str]) -> {deleted, not_found, errors}
  - bulk_export_sources(sources: list[str]) -> markdown bundle
  - get_bulk_stats() -> counts by language/repo

All $0 — direct ChromaDB ops, no LLM.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings

settings = get_settings()

def _get_collection():
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    client = chromadb.PersistentClient(
        path=settings.chroma_persist_directory,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(settings.chroma_collection_name)

def bulk_delete_sources(sources: list[str]) -> dict[str, Any]:
    if not sources:
        return {"deleted": 0, "requested": 0, "not_found": [], "errors": []}
    # Sanitize sources
    cleaned: list[str] = []
    for s in sources:
        s = (s or "").strip()
        if not s:
            continue
        if ".." in s or s.startswith("/") or "\\" in s:
            continue
        if any(c in s for c in ("&", "|", ";", "`", "$", ">", "<", "\n", "\r")):
            continue
        cleaned.append(s)
    # Deduplicate
    cleaned = list(dict.fromkeys(cleaned))[:100]  # cap 100 per request
    if not cleaned:
        return {"deleted": 0, "requested": len(sources), "not_found": [], "errors": ["No valid sources"]}

    collection = _get_collection()
    deleted = 0
    not_found: list[str] = []
    errors: list[str] = []

    for src in cleaned:
        try:
            # Query by metadata source == src (exact) or source contains src basename?
            # Use where filter for exact match
            res = collection.get(where={"source": src}, include=["metadatas"])
            ids = res.get("ids", []) if isinstance(res, dict) else getattr(res, "ids", [])
            # Chromadb 0.5+ returns dict with ids, else object
            if not ids:
                # Try where with $eq explicitly
                try:
                    res2 = collection.get(where={"source": {"$eq": src}})
                    ids = res2.get("ids", []) if isinstance(res2, dict) else getattr(res2, "ids", [])
                except Exception:
                    ids = []
            if not ids:
                not_found.append(src)
                continue
            collection.delete(ids=ids)
            deleted += len(ids)
        except Exception as e:
            errors.append(f"{src}: {str(e)[:120]}")

    # Also need to handle BM25 cache invalidation — trigger rebuild on next query
    try:
        from app.services.retrieval_service import _BM25_INDEX, _BM25_DIRTY
        import app.services.retrieval_service as rs
        rs._BM25_DIRTY = True  # type: ignore
    except Exception:
        pass

    return {
        "deleted": deleted,
        "requested": len(cleaned),
        "not_found": not_found,
        "errors": errors,
        "sources": cleaned,
    }

def bulk_export_sources(sources: list[str]) -> dict[str, Any]:
    """Fetch snippets for given sources and produce markdown bundle (no LLM)."""
    if not sources:
        raise ValueError("No sources provided")
    cleaned = [s.strip() for s in sources if s and s.strip()][:100]
    if not cleaned:
        raise ValueError("No valid sources")
    collection = _get_collection()
    parts: list[str] = []
    found = 0
    not_found: list[str] = []
    for src in cleaned:
        try:
            res = collection.get(where={"source": src}, include=["documents", "metadatas"])
            docs = res.get("documents", []) if isinstance(res, dict) else getattr(res, "documents", [])
            metas = res.get("metadatas", []) if isinstance(res, dict) else getattr(res, "metadatas", [])
            if not docs:
                not_found.append(src)
                continue
            found += 1
            parts.append(f"## {src}\n")
            for i, (doc, meta) in enumerate(zip(docs, metas)):
                lang = (meta or {}).get("language", "") or ""
                parts.append(f"```{lang}\n{doc}\n```\n")
            parts.append("\n---\n")
        except Exception as e:
            not_found.append(f"{src} ({str(e)[:80]})")
    markdown = f"# Bulk Export — {found} files\n\n" + "\n".join(parts)
    return {"markdown": markdown, "found": found, "requested": len(cleaned), "not_found": not_found, "filename": "bulk-export.md"}

def get_bulk_stats() -> dict[str, Any]:
    """Return counts by language and repo for bulk UI filters."""
    try:
        from app.services.retrieval_service import get_indexed_files
        import asyncio
        try:
            files = asyncio.run(get_indexed_files())
        except RuntimeError:
            # If already in event loop, fallback to sync chroma read
            collection = _get_collection()
            res = collection.get(include=["metadatas"])
            metas = res.get("metadatas", []) if isinstance(res, dict) else getattr(res, "metadatas", [])
            files = [{"source": (m or {}).get("source", ""), "language": (m or {}).get("language", "text"), "repo_url": (m or {}).get("repo_url", "") } for m in metas]
        # Aggregate
        by_lang: dict[str, int] = {}
        by_repo: dict[str, int] = {}
        for f in files:
            lang = f.get("language") or "text"
            by_lang[lang] = by_lang.get(lang, 0) + 1
            repo = f.get("repo_url") or f.get("source", "").split("::")[0] if "::" in f.get("source", "") else "unknown"
            by_repo[repo] = by_repo.get(repo, 0) + 1
        return {"total_files": len(files), "by_language": by_lang, "by_repo": by_repo, "files": files[:200]}
    except Exception as e:
        return {"total_files": 0, "by_language": {}, "by_repo": {}, "files": [], "error": str(e)[:200]}
