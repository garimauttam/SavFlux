"""
ingest.py — API routes for ingesting code into the vector store.

WHY SEPARATE ROUTES FROM SERVICES?
Routes are about HTTP: status codes, request parsing, response formatting.
Services are about business logic: cloning, splitting, embedding.
If you mix them, you can't test your logic without spinning up a web server.
Keeping them separate = testable, maintainable code.
"""

import json
import asyncio
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.services.ingestion_service import (
    ingest_github_repo,
    ingest_uploaded_files,
    clear_index,
    get_indexed_repos,
    ingestion_lock,
    normalize_repo_url,
)
from app.services.dep_graph import build_dependency_graph
from app.services.job_store import create_job, update_job, finish_job, get_job

router = APIRouter(prefix="/ingest", tags=["ingestion"])


class GitHubIngestRequest(BaseModel):
    repo_url: str            # e.g. "https://github.com/tiangolo/fastapi"
    branch: str = ""         # e.g. "dev", "feature/auth" — empty means default branch

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("repo_url cannot be empty.")
        # Accept https://github.com/... or git@ URLs
        if not (v.startswith("https://") or v.startswith("git@")):
            raise ValueError("repo_url must start with https:// or git@")
        return normalize_repo_url(v)

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, v: str) -> str:
        v = v.strip()
        # Reject obviously malicious branch names (shell injection guard)
        if any(c in v for c in ("&", "|", ";", "`", "$", ">", "<", "\n", "\r")):
            raise ValueError("Branch name contains invalid characters.")
        return v


@router.post("/github")
async def ingest_github(request: GitHubIngestRequest, _: None = Depends(require_api_key)):
    """
    Ingest a public GitHub repo.
    Returns a Server-Sent Events (SSE) stream of progress updates AND a job_id.

    WHY SSE + JOB ID?
    The SSE stream is the primary progress channel — it delivers real-time updates
    while the connection is live. The job_id lets the client poll
    GET /ingest/status/{job_id} if the SSE connection drops (browser refresh,
    network blip, Railway's 60s idle timeout). This way ingestion is never
    silently lost — the client can always check what happened.

    WHY SSE INSTEAD OF WEBSOCKETS FOR PROGRESS?
    SSE is one-directional (server → client), which is all we need here.
    It's simpler than WebSockets — no handshake, works over plain HTTP,
    and browsers reconnect automatically if the connection drops.
    Format: each event is a JSON line prefixed with "data: "
    """
    job_id = create_job()
    progress_events = []
    completed = asyncio.Event()
    final_result = {}

    async def progress_callback(event: dict):
        progress_events.append(event)
        # Keep the job store in sync so polling works even if SSE is disconnected
        update_job(job_id, status="running", message=event.get("message", ""))

    async def run_ingestion():
        nonlocal final_result
        try:
            async with ingestion_lock:
                final_result = await ingest_github_repo(
                    repo_url=request.repo_url,
                    branch=request.branch or None,
                    progress_callback=progress_callback,
                )
        except Exception as e:
            final_result = {"status": "error", "message": str(e)}
        finally:
            finish_job(job_id, final_result)
            completed.set()

    # Start ingestion in background — don't await it here
    asyncio.create_task(run_ingestion())

    async def event_generator():
        """
        Yields SSE-formatted progress events.

        First event is always {"step": "queued", "job_id": "<uuid>"} so the
        client can store the job_id before any work starts.

        RACE CONDITION FIX:
        Original code checked `completed.is_set()` and `len(progress_events)` in
        the same condition. If `completed` was set between the two checks, the loop
        could exit before flushing the last batch of events added by the callback.

        Fix: flush all pending events FIRST, then check completion.
        The final `complete` event is only emitted AFTER we know the queue is empty.
        """
        # Always emit job_id first — client stores this for fallback polling
        yield f"data: {json.dumps({'step': 'queued', 'job_id': job_id})}\n\n"

        sent = 0
        while True:
            # Drain any pending events first
            while sent < len(progress_events):
                event = progress_events[sent]
                # SSE format: "data: <json>\n\n"
                yield f"data: {json.dumps(event)}\n\n"
                sent += 1

            # Only exit if completed AND queue is fully drained
            if completed.is_set() and sent >= len(progress_events):
                break

            await asyncio.sleep(0.1)  # Poll every 100ms

        # Final result event — sent after all progress events
        yield f"data: {json.dumps({**final_result, 'step': 'complete', 'job_id': job_id})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering (important for SSE)
        },
    )


@router.get("/status/{job_id}")
async def get_ingestion_status(job_id: str, _: None = Depends(require_api_key)):
    """
    Poll the status of a running or completed ingestion job.

    WHY THIS EXISTS:
    The SSE stream in /ingest/github is the primary progress channel, but SSE
    connections can drop (browser refresh, network blip, Railway's idle timeout).
    This endpoint lets the client check the job outcome without needing the stream
    to stay alive for the full duration.

    Returns:
      200 with {"status": "pending"|"running"|"success"|"error", ...}
      404 if job_id is unknown or has expired (jobs expire after 1 hour)
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found or has expired (jobs are kept for 1 hour).",
        )
    return {
        "job_id": job_id,
        "status": job["status"],
        "message": job["message"],
        "result": job["result"],   # None until complete; full result dict on finish
    }


@router.post("/files")
async def ingest_files(files: List[UploadFile] = File(...), _: None = Depends(require_api_key)):
    """
    Ingest uploaded files directly.
    Accepts multiple files at once via multipart/form-data.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    files_content = []
    for f in files:
        content = await f.read()
        # f.filename can be None for certain HTTP clients / curl requests
        filename = f.filename or "uploaded_file"
        files_content.append((filename, content))

    async with ingestion_lock:
        result = await ingest_uploaded_files(files_content)

    if result["status"] == "error":
        raise HTTPException(status_code=422, detail=result["message"])

    return result


@router.delete("/clear")
async def clear_vector_store(repo_url: str | None = None, _: None = Depends(require_api_key)):
    """
    Clear the vector store.

    WHY IS THIS NEEDED?
    Without this, every new repo piles on top of old ones in the same ChromaDB
    collection. Asking "what does main.py do?" would pull results from ALL indexed
    repos mixed together — deeply confusing answers with no way to isolate context.

    Two modes:
    - DELETE /ingest/clear?repo_url=https://...  → clear one specific repo
    - DELETE /ingest/clear                        → clear everything (full reset)
    """
    async with ingestion_lock:
        result = await clear_index(repo_url=repo_url)
    return result


@router.get("/repos")
async def list_indexed_repos(_: None = Depends(require_api_key)):
    """
    Returns a list of all unique repo URLs that have been indexed.
    Used by the frontend to show which repos are active and let the user
    select which one to scope the chat to.
    """
    repos = await get_indexed_repos()
    return {"repos": repos, "total": len(repos)}


@router.get("/dependency-graph")
async def get_dependency_graph(repo_url: str | None = None, _: None = Depends(require_api_key)):
    """
    Return the file dependency graph for the indexed codebase.

    Extracts import relationships from stored chunk content using language-specific
    regex patterns. No re-reading from disk — everything comes from ChromaDB.

    repo_url: optional filter — when provided, restricts graph to one repo's files.

    Response shape:
      {
        "nodes": [{"id": "...", "label": "file.py", "language": "py", "val": 3}],
        "edges": [{"source": "...", "target": "..."}],
        "stats": {"files": N, "dependencies": M}
      }
    """
    try:
        graph = await asyncio.to_thread(build_dependency_graph, repo_url)
        return graph
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
