"""
file_tree.py — P2 File Tree Explorer API ($0, local)

Endpoints:
  GET /file-tree         — full tree + flat list + stats
  GET /file-tree/search  — search files ?q=&limit=
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/file-tree", tags=["file-tree"])


@router.get("")
async def get_file_tree(_: None = Depends(require_api_key)):
    try:
        from app.services.file_tree_service import build_file_tree
        import asyncio
        data = await asyncio.to_thread(build_file_tree)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
async def search_file_tree(
    q: str = Query("", min_length=0, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(require_api_key),
):
    try:
        from app.services.file_tree_service import search_files
        import asyncio
        results = await asyncio.to_thread(search_files, q, limit)
        return {"results": results, "total": len(results), "query": q}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
