"""
review_cache.py — Content-addressed results cache for the review pipeline.

WHY THIS FILE EXISTS
--------------------
Reviewing a file is expensive in exactly one dimension: the LLM call. On a 67-file
repo at ~2.7s per file, three concurrent, that is ~60s of wall clock for a full
pass — and a full pass used to happen every time, including for the 60 files that
had not changed since the last one. The static analyzer had already proven their
findings in 4.5ms each; the model was being asked to re-describe work that was
finished and whose answer was already known.

So results are cached against a key derived from everything that can change them:

    sha256(content) · file_name · language · provider · model · mode · planner_version

(see `file_key` — the exact bytes matter, and so does `planner_version`, because a
new planner routes differently and an old answer would then be attributed to the
wrong decision.)

WHAT A HIT MEANS
----------------
A hit means: *the same model was asked the same question about the same bytes
before, and this is what it said.* It is evidence, not a shortcut — so the review
carries a provenance note naming the digest and the date, and the file is counted
as reviewed (because it was) rather than as reviewed-again. A cached result that
silently looked like a fresh one would be exactly the kind of dishonesty this
codebase is built to avoid.

WHY NOT CACHE THE SUMMARIES OR THE PATCHES?
The repo summary depends on the *set* of findings, and a patch depends on the
working copy. Both are cheap relative to the model calls, and caching either
would mean reasoning about staleness for no measurable gain. This module caches
per-file and per-batch review text only.

STORAGE
-------
`chroma_data/review_cache.json`, one entry per key, LRU-evicted by count and by
bytes. Thread-safe, atomic writes, and every operation degrades to "miss" rather
than raising — a review must never fail because a cache file is corrupt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

from app.core.paths import data_file

logger = logging.getLogger(__name__)

#: Bump when the *question* asked of the model changes (prompt, temperature,
#: section list). Old entries then simply stop matching instead of being served
#: as if they answered the new question.
#:
#: 1 — initial: per-file fast review, `stream_fast_code_review`
PROMPT_VERSION = 1

#: Entries and bytes are both capped: 500 entries of ~4 KB is ~2 MB, and a repo's
#: worth of LLM reviews is a few hundred KB. The byte cap is what actually binds
#: when a handful of large files are cached.
MAX_ENTRIES = 500
MAX_BYTES = 8 * 1024 * 1024

_LOCK = threading.Lock()
_WRITE_COUNTER = 0
#: Rewriting a ~2 MB JSON file on every put is wasteful, so disk writes are
#: batched. The *in-memory* state is authoritative between saves — an earlier
#: version re-read the file on every access and therefore lost up to four of
#: every five writes, including the one a caller had just made.
_SAVE_EVERY = 5

#: The loaded cache, tagged with the path it came from. Keyed by path so that a
#: relocated data directory (tests, a second tenant) is picked up immediately
#: instead of serving entries from the previous location.
_STATE: dict[str, Any] = {"path": "", "data": None}

#: In-process hit/miss counters for the stats endpoint. Persisted `stats` are
#: cumulative across processes; these are this process's contribution.
_session = {"hits": 0, "misses": 0, "writes": 0, "evictions": 0}


# ── Keys ──────────────────────────────────────────────────────────────────────


def hash_content(text: str) -> str:
    """Stable short digest of file content. The cache's whole identity is this."""
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def file_key(
    content: str,
    file_name: str = "",
    language: str = "",
    *,
    provider: str = "",
    model: str = "",
    mode: str = "fast",
) -> str:
    """
    Cache key for one file's review.

    `content` is hashed rather than mixed in so the key stays a fixed size; the
    remaining fields are part of the question (a YAML file and a Python file with
    identical bytes are different questions, and a different model is a different
    answer), so they are joined in plain text before hashing.

    `mode` is "fast" or "agentic" — the two ask for very different reviews, and
    serving one for the other would be a correctness bug, not a cache hit.
    """
    parts = [
        f"v{PROMPT_VERSION}",
        f"sha:{hash_content(content)}",
        f"name:{file_name}",
        f"lang:{language}",
        f"provider:{provider}",
        f"model:{model or 'default'}",
        f"mode:{mode}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def batch_key(
    chunk_hashes: list[str],
    *,
    provider: str = "",
    model: str = "",
    mode: str = "fast",
) -> str:
    """
    Cache key for a batched review of several files.

    The order of `chunk_hashes` is meaningful — the prompt numbers the files — so
    they are joined in order rather than sorted.
    """
    parts = [
        f"v{PROMPT_VERSION}",
        f"batch:{len(chunk_hashes)}",
        "|".join(chunk_hashes),
        f"provider:{provider}",
        f"model:{model or 'default'}",
        f"mode:{mode}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


# ── Storage ───────────────────────────────────────────────────────────────────


def _cache_path():
    """Resolved lazily so a changed data directory takes effect immediately."""
    return data_file("review_cache.json")


def _load() -> dict[str, Any]:
    """
    The in-memory cache, re-read from disk only when the file is not already held.

    Reads dominate writes (every file in every review), and parsing a multi-
    megabyte JSON on each one would cost more than the lookup saves, so the parsed
    document is kept and only replaced when the data directory changes.
    """
    try:
        path = str(_cache_path())
    except Exception:  # noqa: BLE001 — no data dir: an in-memory-only cache
        path = ""

    if _STATE["data"] is not None and _STATE["path"] == path:
        return _STATE["data"]

    data: dict[str, Any] = {"version": PROMPT_VERSION, "stats": {}, "entries": {}}
    try:
        loaded = json.loads(_cache_path().read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
            loaded.setdefault("stats", {})
            data = loaded
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 — a corrupt cache is a cold cache
        logger.warning("review cache unreadable, starting empty: %s", exc)

    _STATE["path"], _STATE["data"] = path, data
    return data


def _save(data: dict[str, Any]) -> None:
    """
    Write the cache atomically.

    A half-written cache file would be read back as corrupt on the next start and
    silently throw away every entry, so the write goes to a temp file in the same
    directory and is then renamed over the target.
    """
    path = _cache_path()
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".review_cache", delete=False
        ) as handle:
            json.dump(data, handle)
            temp_name = handle.name
        os.replace(temp_name, path)
    except Exception as exc:  # noqa: BLE001 — caching must never break a review
        logger.warning("could not persist review cache: %s", exc)


def _prune(entries: dict[str, Any]) -> int:
    """
    Evict least-recently-used entries until both caps are satisfied.

    LRU by `last_hit` (falling back to `created`) rather than by creation time,
    so a file that keeps being reviewed but was written long ago is kept over one
    that was written yesterday and never read.
    """
    evicted = 0
    if len(entries) > MAX_ENTRIES:
        ordered = sorted(
            entries.items(), key=lambda kv: kv[1].get("last_hit") or kv[1].get("created", 0)
        )
        for key, _ in ordered[: len(entries) - MAX_ENTRIES]:
            entries.pop(key, None)
            evicted += 1

    total = sum(int(entry.get("bytes", 0)) for entry in entries.values())
    if total > MAX_BYTES:
        ordered = sorted(
            entries.items(), key=lambda kv: kv[1].get("last_hit") or kv[1].get("created", 0)
        )
        for key, entry in ordered:
            if total <= MAX_BYTES:
                break
            entries.pop(key, None)
            total -= int(entry.get("bytes", 0))
            evicted += 1
    return evicted


# ── Public API ────────────────────────────────────────────────────────────────


def get(key: str) -> dict[str, Any] | None:
    """
    Return `{"value", "meta", "created", "hits"}` for a key, or None on a miss.

    Records the hit (for LRU and for the stats endpoint) on the way out, so the
    entry that is being served is also the entry that survives eviction.
    """
    if not key:
        return None
    with _LOCK:
        data = _load()
        entry = data["entries"].get(key)
        if not entry:
            _session["misses"] += 1
            return None

        entry["hits"] = int(entry.get("hits", 0)) + 1
        entry["last_hit"] = time.time()
        data["stats"]["hits"] = int(data["stats"].get("hits", 0)) + 1
        _session["hits"] += 1
        _save(data)

        return {
            "value": entry.get("value", ""),
            "meta": entry.get("meta", {}) or {},
            "created": entry.get("created"),
            "hits": entry["hits"],
        }


def put(key: str, value: str, kind: str, meta: dict[str, Any] | None = None) -> None:
    """Store one result. Never raises; a failed write is a future miss."""
    if not key or not value:
        return
    global _WRITE_COUNTER
    try:
        with _LOCK:
            data = _load()
            data["entries"][key] = {
                "kind": kind,
                "value": value,
                "bytes": len(value.encode("utf-8")),
                "created": time.time(),
                "last_hit": time.time(),
                "hits": 0,
                "meta": meta or {},
            }
            data["stats"]["writes"] = int(data["stats"].get("writes", 0)) + 1
            _session["writes"] += 1
            _session["evictions"] += _prune(data["entries"])
            _WRITE_COUNTER += 1
            if _WRITE_COUNTER % _SAVE_EVERY == 0:
                _save(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("review cache put failed (non-fatal): %s", exc)


def flush() -> None:
    """Persist any writes still buffered by `_SAVE_EVERY`."""
    try:
        with _LOCK:
            _save(_load())
    except Exception:  # noqa: BLE001
        pass


def clear() -> int:
    """Drop every entry. Returns how many were removed."""
    with _LOCK:
        data = _load()
        removed = len(data["entries"])
        data["entries"] = {}
        _save(data)
        return removed


def reset() -> None:
    """
    Forget the in-memory copy, forcing the next access to re-read from disk.

    Used by tests that point the data directory somewhere new mid-session; in
    production the path tag on `_STATE` makes this unnecessary.
    """
    with _LOCK:
        _STATE["path"], _STATE["data"] = "", None


def stats() -> dict[str, Any]:
    """Cache size and cumulative hit/miss counters, for `GET /review/cache`."""
    with _LOCK:
        data = _load()
        entries = data["entries"]
        total_bytes = sum(int(entry.get("bytes", 0)) for entry in entries.values())
        persisted = data.get("stats", {})
        hits = int(persisted.get("hits", 0))
        writes = int(persisted.get("writes", 0))
        return {
            "entries": len(entries),
            "bytes": total_bytes,
            "max_entries": MAX_ENTRIES,
            "max_bytes": MAX_BYTES,
            "prompt_version": PROMPT_VERSION,
            "hits": hits,
            "writes": writes,
            # Every write is a model call that will not be repeated; every hit is
            # one that was not made at all.
            "hit_rate": round(hits / (hits + writes), 3) if (hits + writes) else 0.0,
            "session": dict(_session),
        }
