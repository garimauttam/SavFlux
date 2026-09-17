"""
job_store.py — In-process job status store for long-running ingestion tasks.

WHY A JOB STORE?
The GitHub ingestion endpoint already fires work off as an asyncio task and
streams progress via SSE. But if the SSE connection drops (browser refresh,
network blip, Railway 60s timeout), the job keeps running and the client has
no way to check what happened.

This module gives every ingestion job a UUID. The client gets the job_id
immediately on POST, then can poll  GET /ingest/status/{job_id}  to see the
current status without requiring a persistent SSE connection.

DESIGN:
  - Pure in-process dict — no Redis, no SQLite. Keeps the deploy simple.
  - Jobs auto-expire after 1 hour to avoid unbounded memory growth.
  - Thread/async safe: only asyncio tasks write to it (single-threaded event loop).
"""

import time
import uuid
from typing import Literal

# Job status type
JobStatus = Literal["pending", "running", "success", "error"]

# In-process store: job_id → job dict
# Each job: {"status", "message", "progress", "result", "created_at"}
_jobs: dict[str, dict] = {}

# Jobs older than this are evicted on the next write (lazy cleanup)
_TTL_SECONDS = 3600  # 1 hour


def create_job() -> str:
    """Create a new job entry and return its ID."""
    _evict_expired()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "pending",
        "message": "Queued...",
        "progress": [],        # list of progress event dicts
        "result": None,        # final result dict on completion
        "created_at": time.time(),
    }
    return job_id


def update_job(job_id: str, status: JobStatus, message: str) -> None:
    """Update job status and append a progress message."""
    if job_id not in _jobs:
        return
    _jobs[job_id]["status"] = status
    _jobs[job_id]["message"] = message
    _jobs[job_id]["progress"].append({"status": status, "message": message})


def finish_job(job_id: str, result: dict) -> None:
    """Mark the job as finished (success or error) and store the final result."""
    if job_id not in _jobs:
        return
    status: JobStatus = "success" if result.get("status") == "success" else "error"
    _jobs[job_id]["status"] = status
    _jobs[job_id]["message"] = result.get("message", "Done.")
    _jobs[job_id]["result"] = result


def get_job(job_id: str) -> dict | None:
    """Return the job dict or None if it doesn't exist / has expired."""
    return _jobs.get(job_id)


def _evict_expired() -> None:
    """Remove jobs older than TTL. Called lazily on each create_job()."""
    now = time.time()
    expired = [jid for jid, job in _jobs.items() if now - job["created_at"] > _TTL_SECONDS]
    for jid in expired:
        del _jobs[jid]
