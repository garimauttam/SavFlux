"""
analytics_service.py — Request-latency analytics ($0, local JSONL).

main.py's analytics middleware calls record_latency() on every request
(except /health and /metrics/history). Rows are appended to
chroma_data/analytics_history.jsonl — flat file, no DB, no deps.

Each row: {ts, path, latency_ms, status, total_tokens, llm_calls,
           avg_latency_ms, health_avg}

The token/health fields are point-in-time snapshots from token_counter so
the Analytics tab can draw sparklines without a second data source.

Thread-safety: appends take the lock; the file is trimmed to the last
2000 rows on write so it can't grow unbounded.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.core.paths import data_file

_LOCK = threading.Lock()
_MAX_ROWS = 2000


def _history_path() -> Path:
    """Resolved lazily so a changed data dir takes effect immediately."""
    return data_file("analytics_history.jsonl")


def _read_rows(limit: int = 500) -> list[dict[str, Any]]:
    try:
        path = _history_path()
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue
        return rows
    except Exception:
        return []


def record_latency(path: str, latency_ms: float, status_code: int = 200) -> None:
    """Append one request sample. Called from middleware — never raises."""
    try:
        total_tokens = 0
        llm_calls = 0
        try:
            from app.services.token_counter import get_totals
            totals = get_totals() or {}
            total_tokens = int(totals.get("total_tokens", 0))
            llm_calls = int(totals.get("llm_calls", 0))
        except Exception:
            pass

        row = {
            "ts": time.time(),
            "path": (path or "")[:120],
            "latency_ms": round(float(latency_ms), 2),
            "status": int(status_code),
            "total_tokens": total_tokens,
            "llm_calls": llm_calls,
            "avg_latency_ms": 0,  # backfilled below from recent rows
            "health_avg": "—",
        }
        with _LOCK:
            recent = _read_rows(100)
            latencies = [r.get("latency_ms", 0) for r in recent[-20:]]
            latencies.append(row["latency_ms"])
            row["avg_latency_ms"] = round(sum(latencies) / len(latencies), 2)
            with open(_history_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            # Trim: keep the file bounded without a rewrite on every request.
            # Only trim when it grows 10% past the cap (amortised O(1)).
            try:
                if _history_path().stat().st_size > 0:
                    all_lines = _history_path().read_text(encoding="utf-8").splitlines()
                    if len(all_lines) > _MAX_ROWS + 200:
                        _history_path().write_text(
                            "\n".join(all_lines[-_MAX_ROWS:]) + "\n", encoding="utf-8"
                        )
            except Exception:
                pass
    except Exception:
        pass


def get_history(limit: int = 200, path: str | None = None) -> list[dict[str, Any]]:
    rows = _read_rows(max(1, min(limit, _MAX_ROWS)))
    if path:
        rows = [r for r in rows if r.get("path") == path]
    return rows


def get_summary() -> dict[str, Any]:
    """Per-endpoint aggregates for the Analytics tab: counts + avg/p95 latency."""
    rows = _read_rows(_MAX_ROWS)
    by_path: dict[str, list[float]] = defaultdict(list)
    errors = 0
    for r in rows:
        by_path[r.get("path", "?")].append(float(r.get("latency_ms", 0)))
        if int(r.get("status", 200)) >= 400:
            errors += 1

    endpoints = []
    for p, lat in sorted(by_path.items(), key=lambda kv: len(kv[1]), reverse=True)[:30]:
        ordered = sorted(lat)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        endpoints.append({
            "path": p,
            "count": len(lat),
            "avg_latency_ms": round(sum(lat) / len(lat), 2),
            "p95_latency_ms": round(p95, 2),
            "max_latency_ms": round(max(lat), 2),
        })
    return {
        "total_requests": len(rows),
        "errors": errors,
        "endpoints": endpoints,
        "recent": rows[-60:],
    }


def clear_history() -> int:
    with _LOCK:
        path = _history_path()
        try:
            if not path.is_file():
                return 0
            n = len(path.read_text(encoding="utf-8").splitlines())
            path.unlink()
            return n
        except Exception:
            return 0
