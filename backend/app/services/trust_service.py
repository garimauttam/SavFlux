"""
trust_service.py — Commit-verification ledger ($0, local file + git CLI).

Every GitHub ingest records the exact upstream commit (HEAD sha) the index
was built from. get_entry() compares it against the live upstream HEAD via
`git ls-remote` (read-only, 10s timeout) and reports:

  verified — indexed sha == upstream HEAD (answers cite current code)
  stale    — upstream moved on (re-index recommended)
  unknown  — no sha recorded or upstream unreachable (offline?)

Storage: chroma_data/trust_ledger.json {repos: {url: {indexed_sha,
indexed_at, files_indexed}}}. Thread-safe via Lock. Never raises to callers
in the ingest path — verification must not break indexing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings

settings = get_settings()
_LOCK = threading.Lock()
_LS_REMOTE_TIMEOUT = 10


def _ledger_path() -> Path:
    p = Path(settings.chroma_persist_directory) / "trust_ledger.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def _load() -> dict[str, Any]:
    try:
        raw = _ledger_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("repos", {})
            return data
    except Exception:
        pass
    return {"repos": {}}


def _save(data: dict[str, Any]) -> None:
    _ledger_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_index(repo_url: str, sha: str | None, files_indexed: int = 0) -> None:
    """Persist the commit an ingest was built from. Never raises."""
    try:
        url = (repo_url or "").strip()
        if not url:
            return
        with _LOCK:
            data = _load()
            data["repos"][url] = {
                "indexed_sha": (sha or "").strip() or None,
                "indexed_at": time.time(),
                "files_indexed": int(files_indexed),
            }
            _save(data)
    except Exception:
        pass


def upstream_head(repo_url: str) -> str | None:
    """Read-only upstream HEAD sha via `git ls-remote`. None on any failure.

    SECURITY: argv list form (no shell), URL scheme allowlist, hard timeout.
    """
    url = (repo_url or "").strip()
    if not (url.startswith("https://") or url.startswith("git@")):
        return None
    git_bin = shutil.which("git")
    if not git_bin:
        return None
    try:
        proc = subprocess.run(
            [git_bin, "ls-remote", url, "HEAD"],
            capture_output=True, text=True, timeout=_LS_REMOTE_TIMEOUT,
        )
        if proc.returncode != 0:
            return None
        out = (proc.stdout or "").strip().split()
        sha = out[0] if out else ""
        return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None
    except Exception:
        return None


def get_entry(repo_url: str, check_upstream: bool = True) -> dict[str, Any] | None:
    """Verification entry for a repo, or None when never recorded."""
    url = (repo_url or "").strip()
    row = _load().get("repos", {}).get(url)
    if not row:
        return None
    indexed_sha = row.get("indexed_sha")
    upstream = upstream_head(url) if check_upstream else None
    if not indexed_sha:
        status, detail = "unknown", "no commit recorded for this index"
    elif upstream is None:
        status, detail = "unknown", "upstream unreachable (offline?) — index usable as-is"
    elif upstream == indexed_sha:
        status, detail = "verified", "index matches upstream HEAD"
    else:
        status, detail = "stale", "upstream has new commits — re-index recommended"
    return {
        "repo_url": url,
        "indexed_sha": indexed_sha,
        "indexed_at": row.get("indexed_at"),
        "upstream_sha": upstream,
        "status": status,
        "files_indexed": int(row.get("files_indexed", 0)),
        "detail": detail,
    }
