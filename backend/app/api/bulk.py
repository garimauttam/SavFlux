"""
bulk.py — P2 Bulk Operations API ($0, local Chroma)

Endpoints:
  GET    /bulk/stats            — counts by language/repo + sample files
  POST   /bulk/delete           — bulk delete {sources: [...]} -> {deleted, not_found}
  POST   /bulk/export           — bulk export {sources: [...]} -> {markdown, filename}
"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_api_key

router = APIRouter(prefix="/bulk", tags=["bulk"])


@router.get("/stats")
async def bulk_stats(_: None = Depends(require_api_key)):
    try:
        from app.services.bulk_service import get_bulk_stats
        import asyncio
        stats = await asyncio.to_thread(get_bulk_stats)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete")
async def bulk_delete(body: dict, _: None = Depends(require_api_key)):
    sources = body.get("sources")
    if not isinstance(sources, list) or not sources:
        raise HTTPException(status_code=400, detail="sources must be non-empty array")
    if len(sources) > 100:
        raise HTTPException(status_code=400, detail="Too many sources (max 100)")
    try:
        from app.services.bulk_service import bulk_delete_sources
        import asyncio
        result = await asyncio.to_thread(bulk_delete_sources, sources)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/export")
async def bulk_export(body: dict, _: None = Depends(require_api_key)):
    sources = body.get("sources")
    if not isinstance(sources, list) or not sources:
        raise HTTPException(status_code=400, detail="sources must be non-empty array")
    if len(sources) > 100:
        raise HTTPException(status_code=400, detail="Too many sources (max 100)")
    try:
        from app.services.bulk_service import bulk_export_sources
        import asyncio
        result = await asyncio.to_thread(bulk_export_sources, sources)
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
