"""
indexed_content.py — Read a file back out of the vector index.

WHY THIS EXISTS
---------------
The vector store is frequently the *only* copy of a file. Repos are cloned into
a temp directory, indexed, and the clone is deleted — so an hour later the
backing file is gone and ChromaDB still has every chunk. Three modules had each
grown their own version of "find the chunks for this file and rebuild it", and
they disagreed on how to find them:

  * `review.py` scanned the **entire** collection and filtered in Python. That
    is O(collection) per lookup — on a 10k-file index it is a multi-second scan
    to rebuild one 200-line file.
  * `agent.py` asked for `where={"source": path}`, which never matches, because
    source ids are `f"{repo_url}::{relative_path}"`.

This module is the one implementation: resolve the source id cheaply when it can
(metadata-filtered, index-backed) and fall back to a full scan only when the
cheap paths miss, which is rare and always correct.

The lookup ladder, cheapest first:
  1. exact source id — `repo_url::path` (or a bare path for local ingests)
  2. `file_name` metadata filter — narrows thousands of chunks to a handful
  3. full collection scan with a suffix match — the old behaviour, kept as a
     correctness floor so no caller ever gets *less* than it used to
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _source_candidates(path: str, source: str | None, repo_url: str | None) -> list[str]:
    """
    Source ids worth asking ChromaDB for, in priority order.

    Ingestion writes `source = f"{normalized_repo_url}::{relative_path}"`, and a
    local directory ingest writes the absolute path. `source` (when the caller
    already holds the exact id, as the review UI does) beats every guess, so it
    goes first.
    """
    candidates: list[str] = []

    def add(value: str | None) -> None:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    add(source)
    add(path)

    if repo_url:
        base = repo_url.strip().rstrip("/")
        if base:
            add(f"{base}::{path}")
            # Tolerate a `.git` suffix and a pasted clone URL: the normaliser in
            # ingestion strips both, and the caller may not have.
            bare = base[:-4] if base.endswith(".git") else base
            add(f"{bare}::{path}")
            add(f"https://github.com/{bare.removeprefix('https://github.com/')}::{path}" if bare else None)

    return candidates


def _pairs(result: dict) -> list[tuple[dict, str]]:
    """Zip a ChromaDB `get()` result into (metadata, document) pairs."""
    return list(zip(result.get("metadatas") or [], result.get("documents") or []))


def _matches(pairs: list[tuple[dict, str]], path: str) -> list[tuple[dict, str]]:
    """
    Keep the pairs whose source id *ends* with the requested path.

    `path` is repo-relative ("src/auth.py") while source ids carry a repo
    prefix, so a suffix match on the path segment is the correct comparison —
    an exact string compare would reject every cloned-repo file.
    """
    tail = path.lstrip("/")
    kept = []
    for meta, doc in pairs:
        source = str(meta.get("source", ""))
        if source.endswith(tail) or source.split("::")[-1] == tail:
            kept.append((meta, doc))
    return kept


def read_indexed_file(
    path: str,
    *,
    source: str | None = None,
    repo_url: str | None = None,
) -> str:
    """
    Reconstruct one indexed file from its chunks. Returns "" when unavailable.

    Empty string (rather than an exception) is the contract because every caller
    treats "not in the index" as an expected state — a temp clone was cleaned up,
    or the user reviewed a file that was never indexed — not as an error.
    """
    cleaned = (path or "").strip()
    exact = (source or "").strip()
    if not cleaned and not exact:
        return ""

    try:
        from app.services.chunk_reconstruction import reconstruct_chunks
        from app.services.ingestion_service import _get_vectorstore

        collection = _get_vectorstore()._collection
    except Exception as exc:  # noqa: BLE001 — no index, no content; callers degrade
        logger.debug("vector store unavailable for %s: %s", cleaned or exact, exc)
        return ""

    # ── 1. exact source id ────────────────────────────────────────────────────
    for candidate in _source_candidates(cleaned, exact, repo_url):
        try:
            hit = collection.get(where={"source": candidate},
                                 include=["documents", "metadatas"])
            pairs = _pairs(hit)
        except Exception:  # noqa: BLE001 — a bad filter must not abort the ladder
            continue
        if pairs:
            return reconstruct_chunks(pairs)

    # ── 2. metadata-filtered by file name ─────────────────────────────────────
    name = Path(cleaned or exact).name
    if name:
        try:
            hit = collection.get(where={"file_name": name},
                                 include=["documents", "metadatas"])
            selected = _matches(_pairs(hit), cleaned) if cleaned else []
            if selected:
                return reconstruct_chunks(selected)
        except Exception:  # noqa: BLE001
            pass

    # ── 3. full scan (the original behaviour) ─────────────────────────────────
    try:
        hit = collection.get(include=["documents", "metadatas"])
        selected = _matches(_pairs(hit), cleaned) if cleaned else []
        if selected:
            return reconstruct_chunks(selected)
    except Exception as exc:  # noqa: BLE001 — empty/mocked stores land here
        logger.debug("indexed lookup failed for %s: %s", cleaned, exc)

    return ""
