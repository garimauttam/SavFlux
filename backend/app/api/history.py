"""
history.py — Time-machine API: per-file git timeline + blame ($0).

Endpoints:
  GET /history/timeline?repo_url=&file=&limit=  — commit history (newest first)
  GET /history/blame?repo_url=&file=&rev=       — line-level blame (rev=HEAD|sha)

`file` accepts an indexed source id ("{repo_url}::{rel_path}"), a
repo-relative path, or a basename. Reads come from a local bare mirror
(see history_service) — no per-request cloning.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/history", tags=["history"])


def _resolve_ref(repo_url: str | None, file: str) -> tuple[str, str]:
    from app.services.history_service import split_source
    url, rel = split_source(file)
    url = url or (repo_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="repo_url is required (or pass a full indexed source id as file)")
    if not rel:
        raise HTTPException(status_code=400, detail="file is required")
    return url, rel


@router.get("/timeline")
async def get_timeline(
    file: str = Query(..., description="Indexed source id, rel path, or basename"),
    repo_url: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    _: None = Depends(require_api_key),
):
    url, rel = _resolve_ref(repo_url, file)
    try:
        from app.services.history_service import file_timeline
        return await asyncio.to_thread(file_timeline, url, rel, limit)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/blame")
async def get_blame(
    file: str = Query(..., description="Indexed source id, rel path, or basename"),
    repo_url: str | None = None,
    rev: str = Query("HEAD", description="HEAD or a full 40-hex commit sha"),
    _: None = Depends(require_api_key),
):
    url, rel = _resolve_ref(repo_url, file)
    try:
        from app.services.history_service import file_blame
        return await asyncio.to_thread(file_blame, url, rel, rev)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
