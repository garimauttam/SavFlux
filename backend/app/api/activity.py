"""
activity.py — P2 Activity Feed API ($0, aggregated local)

Endpoints:
  GET    /activity              — unified feed ?limit=&kind=
  DELETE /activity              — clear ?kind= (or all)
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/activity", tags=["activity"])

ALLOWED_KINDS = {"prompt_history", "prompt_saved", "snippet", "share", "analytics", "ingest"}


@router.get("")
async def get_activity(
    limit: int = Query(50, ge=1, le=200),
    kind: str | None = Query(None, description="Filter by kind: prompt_history|prompt_saved|snippet|share|analytics|ingest"),
    _: None = Depends(require_api_key),
):
    if kind and kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind. Allowed: {', '.join(sorted(ALLOWED_KINDS))}")
    try:
        from app.services.activity_service import get_activity as _get
        items = _get(limit=limit, kind=kind)
        return {"items": items, "total": len(items)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/file")
async def get_file_timeline(
    file: str = Query(..., description="Indexed source id, rel path, or basename"),
    repo_url: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    _: None = Depends(require_api_key),
):
    """Per-file timeline: index record + git history (Time Machine)."""
    try:
        import asyncio
        from app.services.activity_service import file_timeline
        return await asyncio.to_thread(file_timeline, repo_url or "", file, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("")
async def clear_activity(
    kind: str | None = Query(None, description="Kind to clear, or omit for all"),
    _: None = Depends(require_api_key),
):
    if kind and kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind. Allowed: {', '.join(sorted(ALLOWED_KINDS))}")
    try:
        from app.services.activity_service import clear_activity as _clear
        n = _clear(kind=kind)
        return {"status": "cleared", "deleted": n, "kind": kind or "all"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
