"""
watcher.py — Upstream watcher API ($0).

Endpoints:
  GET  /watcher/status  — loop state, poll count, per-repo last-checked
  POST /watcher/poll    — force one immediate poll, return change events
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_api_key

router = APIRouter(prefix="/watcher", tags=["watcher"])


@router.get("/status")
async def watcher_status(_: None = Depends(require_api_key)):
    try:
        from app.services.watcher_service import get_status
        return await asyncio.to_thread(get_status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/poll")
async def watcher_poll(_: None = Depends(require_api_key)):
    try:
        from app.services.watcher_service import poll_once
        events = await asyncio.to_thread(poll_once)
        return {"status": "ok", "events": events, "total": len(events)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
