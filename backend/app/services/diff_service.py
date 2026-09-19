"""
diff_service.py — P2 Diff Viewer ($0, difflib)

Provides file content fetch from Chroma + unified diff via Python difflib.
No LLM, no extra deps, $0.

Used by:
  GET  /diff/file?source=   — raw file content (joined chunks)
  POST /diff/compare        — unified diff between two sources
"""

from __future__ import annotations

import difflib
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

def get_file_content(source: str) -> dict[str, Any]:
    """Fetch and join chunks for a source (exact match on metadata source)."""
    src = (source or "").strip()
    if not src:
        raise ValueError("source is required")
    if ".." in src or src.startswith("/") or "\\" in src:
        raise ValueError("Invalid source path")
    if any(c in src for c in ("&", "|", ";", "`", "$", ">", "<", "\n", "\r")):
        raise ValueError("Invalid source path")
    coll = _get_collection()
    # Try exact source match
    res = coll.get(where={"source": src}, include=["documents", "metadatas"])
    docs = res.get("documents", []) if isinstance(res, dict) else getattr(res, "documents", [])
    metas = res.get("metadatas", []) if isinstance(res, dict) else getattr(res, "metadatas", [])
    if not docs:
        # Fallback: try basename match or suffix
        try:
            all_res = coll.get(include=["documents", "metadatas"])
            all_docs = all_res.get("documents", []) if isinstance(all_res, dict) else getattr(all_res, "documents", [])
            all_metas = all_res.get("metadatas", []) if isinstance(all_res, dict) else getattr(all_res, "metadatas", [])
            # Filter by source containing src
            filtered = [(d, m) for d, m in zip(all_docs, all_metas) if src in (m or {}).get("source", "")]
            if filtered:
                docs, metas = zip(*filtered)
                docs, metas = list(docs), list(metas)
            else:
                raise ValueError(f"Source not found: {src}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Source not found: {src} ({str(e)[:80]})")
    if not docs:
        raise ValueError(f"Source not found: {src}")

    # Join docs in order (they are chunks; sort by metadata chunk_index if present)
    try:
        paired = list(zip(docs, metas))
        paired.sort(key=lambda x: (x[1] or {}).get("chunk_index", 0) if isinstance(x[1], dict) else 0)
        docs_sorted = [p[0] for p in paired]
        metas_sorted = [p[1] for p in paired]
    except Exception:
        docs_sorted, metas_sorted = docs, metas

    content = "\n".join(docs_sorted)
    meta = metas_sorted[0] if metas_sorted else {}
    return {
        "source": src,
        "content": content,
        "language": (meta or {}).get("language", "text"),
        "chunk_count": len(docs_sorted),
        "repo_url": (meta or {}).get("repo_url", ""),
    }

def compute_diff(source_a: str, source_b: str, context: int = 3) -> dict[str, Any]:
    """Compute unified diff between two sources' contents."""
    a = get_file_content(source_a)
    b = get_file_content(source_b)
    a_lines = a["content"].splitlines(keepends=True)
    b_lines = b["content"].splitlines(keepends=True)
    # difflib.unified_diff yields lines with \n
    diff_lines = list(difflib.unified_diff(
        a_lines, b_lines,
        fromfile=a["source"], tofile=b["source"],
        lineterm="", n=max(0, min(context, 10))
    ))
    unified = "\n".join(diff_lines)
    # Stats
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    # Also compute inline stats via SequenceMatcher
    sm = difflib.SequenceMatcher(None, a["content"], b["content"])
    ratio = round(sm.ratio(), 3)

    return {
        "source_a": a["source"],
        "source_b": b["source"],
        "language_a": a["language"],
        "language_b": b["language"],
        "unified_diff": unified,
        "added": added,
        "removed": removed,
        "similarity": ratio,
        "a_lines": len(a_lines),
        "b_lines": len(b_lines),
        "a_content": a["content"][:20000],  # cap for frontend preview
        "b_content": b["content"][:20000],
    }
