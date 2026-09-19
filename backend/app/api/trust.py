"""
trust.py — Commit-verification ledger API ($0).

Endpoints:
  GET  /trust/ledger?repo_url=  — verification entry (404 when never recorded)
  POST /trust/verify            — force a fresh upstream re-check {repo_url}
"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_api_key

router = APIRouter(prefix="/trust", tags=["trust"])


@router.get("/ledger")
async def get_ledger(repo_url: str, _: None = Depends(require_api_key)):
    try:
        import asyncio
        from app.services.trust_service import get_entry
        entry = await asyncio.to_thread(get_entry, repo_url, True)
        if not entry:
            raise HTTPException(status_code=404, detail="No verification record for this repo")
        return entry
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verify")
async def verify_repo(body: dict, _: None = Depends(require_api_key)):
    repo_url = (body.get("repo_url") or "").strip()
    if not repo_url:
        raise HTTPException(status_code=400, detail="repo_url is required")
    try:
        import asyncio
        from app.services.trust_service import get_entry
        entry = await asyncio.to_thread(get_entry, repo_url, True)
        if not entry:
            raise HTTPException(status_code=404, detail="No verification record for this repo")
        return entry
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
