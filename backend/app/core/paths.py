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

from pathlib import Path

from app.core.config import get_settings


def data_dir() -> Path:
    """
    Return the directory holding all local SavFlux state, creating it if needed.

    Resolved on every call so a changed `CHROMA_PERSIST_DIRECTORY` takes effect
    immediately rather than at the next process restart.
    """
    directory = Path(get_settings().chroma_persist_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception:
        # A read-only or racing filesystem must never take the API down —
        # callers all degrade gracefully when the file cannot be read/written.
        pass
    return directory


def data_file(name: str) -> Path:
    """Path to a named state file inside the data directory."""
    return data_dir() / name
