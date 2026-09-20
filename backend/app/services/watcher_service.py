"""
watcher_service.py — Background upstream watcher ($0, 30s polling).

Every WATCHER_INTERVAL_S seconds (default 30), each repo recorded in the
trust ledger is checked for new upstream commits via a read-only
`git ls-remote`. When upstream moves past the indexed commit, a `watcher`
notification is emitted (visible in the Inbox tab) — once per new sha.

main.py's lifespan starts/stops the loop; POST /watcher/poll forces an
immediate check. All state lives in chroma_data/watcher_state.json.

Costs nothing: no LLM, no API keys, one tiny git stdin/stdout call per repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from app.core.paths import data_file

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


def poll_interval() -> int:
    try:
        return max(5, int(os.getenv("WATCHER_INTERVAL_S", "30")))
    except Exception:
        return 30


def _state_path() -> Path:
    """Resolved lazily so a changed data dir takes effect immediately."""
    return data_file("watcher_state.json")


def _load_state() -> dict[str, Any]:
    try:
        raw = _state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("repos", {})
            return data
    except Exception:
        pass
    return {"repos": {}, "last_poll": None, "polls": 0}


def _save_state(data: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def poll_once() -> list[dict[str, Any]]:
    """One synchronous poll across all ledger repos. Returns change events."""
    from app.services.trust_service import _load as _trust_load, upstream_head

    events: list[dict[str, Any]] = []
    with _LOCK:
        state = _load_state()
        repos = _trust_load().get("repos", {})
        for url, row in repos.items():
            indexed_sha = row.get("indexed_sha")
            if not indexed_sha:
                continue
            try:
                head = upstream_head(url)
            except Exception:
                head = None
            entry = state["repos"].setdefault(url, {})
            entry["last_checked"] = time.time()
            if not head:
                entry["upstream"] = None
                continue
            entry["upstream"] = head
            if head != indexed_sha and entry.get("last_notified") != head:
                entry["last_notified"] = head
                events.append({
                    "repo_url": url,
                    "indexed_sha": indexed_sha,
                    "upstream_sha": head,
                    "ts": time.time(),
                })
        state["last_poll"] = time.time()
        state["polls"] = int(state.get("polls", 0)) + 1
        try:
            _save_state(state)
        except Exception:
            pass

    # Notify outside the lock (notification service has its own lock)
    for ev in events:
        try:
            from app.services.notification_service import create_notification
            create_notification(
                title=f"Upstream moved: {ev['repo_url']}",
                message=(f"Indexed {ev['indexed_sha'][:7]} → upstream {ev['upstream_sha'][:7]}. "
                         f"Re-index to keep answers current."),
                kind="watcher",
                level="warning",
                meta={"repo_url": ev["repo_url"], "upstream_sha": ev["upstream_sha"]},
            )
        except Exception as e:
            logger.warning("watcher notification failed: %s", e)
    return events


def get_status() -> dict[str, Any]:
    state = _load_state()
    return {
        "running": _task is not None and not _task.done(),
        "interval_s": poll_interval(),
        "polls": state.get("polls", 0),
        "last_poll": state.get("last_poll"),
        "repos": [
            {"repo_url": url, **entry}
            for url, entry in state.get("repos", {}).items()
        ],
    }


async def _loop() -> None:
    global _stop_event
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await asyncio.to_thread(poll_once)
        except Exception as e:
            logger.warning("watcher poll failed: %s", e)
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=poll_interval())
        except asyncio.TimeoutError:
            continue


async def start_watcher_background() -> None:
    """Start the 30s poll loop (idempotent). Called by main.py lifespan."""
    global _task, _stop_event
    if _task is not None and not _task.done():
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop())
    logger.info("Repo watcher started (every %ss).", poll_interval())


async def stop_watcher_background() -> None:
    """Stop the poll loop. Called by main.py lifespan on shutdown."""
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(_task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
        except Exception:
            pass
    _task = None
    _stop_event = None
