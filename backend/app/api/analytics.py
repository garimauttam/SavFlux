"""
analytics.py — Request-latency analytics API ($0, local JSONL).

Endpoints:
  GET    /analytics/summary  — per-endpoint counts + avg/p95 latency
  GET    /analytics/history  — recent samples ?limit=&path=
  DELETE /analytics/history  — clear samples

Data is written by the analytics middleware in main.py on every request.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def analytics_summary(_: None = Depends(require_api_key)):
    try:
        from app.services.analytics_service import get_summary
        return get_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def analytics_history(
    limit: int = Query(200, ge=1, le=2000),
    path: str | None = None,
    _: None = Depends(require_api_key),
):
    try:
        from app.services.analytics_service import get_history
        rows = get_history(limit=limit, path=path)
        return {"history": rows, "total": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/history")
async def clear_analytics(_: None = Depends(require_api_key)):
    try:
        from app.services.analytics_service import clear_history
        return {"status": "cleared", "deleted": clear_history()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
