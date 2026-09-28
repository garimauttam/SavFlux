"""
paths.py — Single source of truth for where SavFlux keeps its local state.

WHY THIS MODULE EXISTS
Every `$0` feature in SavFlux (trust ledger, prompt library, snippet vault,
notifications, analytics, watcher state, share links) persists to a small JSON
file next to the ChromaDB directory. Before this module each service did:

    settings = get_settings()                     # ← snapshot at IMPORT time
    ...
    Path(settings.chroma_persist_directory) / "prompt_library.json"

That has two real defects:

1. **The data directory is frozen at import time.** `get_settings()` is
   `lru_cache`d, and the module-level `settings = get_settings()` captured the
   result into a module global. Changing `CHROMA_PERSIST_DIRECTORY` afterwards
   (tests, a relocated volume, a second tenant) had no effect — the service kept
   writing to the original path.

2. **Filenames were duplicated across modules.** `activity_service` re-derived
   `base / "prompt_library.json"` instead of asking `prompt_service` where its
   file lives, so the two could silently disagree and the aggregated feed would
   read an empty file.

Both are fixed by resolving the directory lazily, on every call, from one place.
`get_settings()` is still cached, so this costs a dict lookup — not a disk read.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.config import get_settings
from app.core.tenant import current_user_id


def user_data_dir(user_id: str, *, base: Path | None = None) -> Path:
    """Private persistent root for one verified Supabase user."""
    root = base or Path(get_settings().chroma_persist_directory)
    tenant = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return root / "users" / tenant


def data_dir() -> Path:
    """
    Return this request's private SavFlux data root, creating it if needed.

    Every account receives its own Chroma database, Git mirrors, provider keys,
    model selection, and app-state files. The process-level root is never used
    for authenticated user data.
    """
    base = Path(get_settings().chroma_persist_directory)
    user_id = current_user_id()
    directory = user_data_dir(user_id, base=base) if user_id else base
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except Exception:
        # A read-only or racing filesystem must never take the API down —
        # callers all degrade gracefully when the file cannot be read/written.
        pass
    return directory


def data_file(name: str) -> Path:
    """Path to a named state file inside the current user's private data root."""
    return data_dir() / name
