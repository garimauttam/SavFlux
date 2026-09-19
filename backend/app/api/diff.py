"""
diff.py — P2 Diff Viewer API ($0, difflib)

Endpoints:
  GET  /diff/file?source=          — file content (joined chunks)
  POST /diff/compare               — unified diff {source_a, source_b, context?}
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/diff", tags=["diff"])


@router.get("/file")
async def get_diff_file(
    source: str = Query(..., min_length=1, max_length=800, description="Source path, e.g. src/auth.py or https://github.com/o/r::src/auth.py"),
    _: None = Depends(require_api_key),
):
    src = (source or "").strip()
    if not src:
        raise HTTPException(status_code=400, detail="source is required")
    if ".." in src or src.startswith("/") or "\\" in src:
        raise HTTPException(status_code=400, detail="Invalid source path")
    if any(c in src for c in ("&", "|", ";", "`", "$", ">", "<", "\n", "\r")):
        raise HTTPException(status_code=400, detail="Invalid source path")
    try:
        from app.services.diff_service import get_file_content
        import asyncio
        data = await asyncio.to_thread(get_file_content, src)
        return data
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/compare")
async def compare_diff(body: dict, _: None = Depends(require_api_key)):
    source_a = (body.get("source_a") or body.get("sourceA") or body.get("a") or "").strip()
    source_b = (body.get("source_b") or body.get("sourceB") or body.get("b") or "").strip()
    if not source_a or not source_b:
        raise HTTPException(status_code=400, detail="source_a and source_b are required")
    if source_a == source_b:
        raise HTTPException(status_code=400, detail="source_a and source_b must be different")
    context = body.get("context", 3)
    try:
        context = int(context)
        context = max(0, min(context, 10))
    except Exception:
        context = 3
    try:
        from app.services.diff_service import compute_diff
        import asyncio
        result = await asyncio.to_thread(compute_diff, source_a, source_b, context)
        return result
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
