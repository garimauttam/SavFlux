"""
share.py — Share-link API ($0, local file).

Endpoints:
  POST   /share        — snapshot a Q&A pair {question, answer?, sources?, repo_url?}
  GET    /share        — list recent ?limit=&repo_url=
  GET    /share/{id}   — fetch one (public: no API key — links are shareable)
  DELETE /share/{id}   — delete one

WHY IS GET /share/{id} PUBLIC?
Share links are opened by people who don't have your API key (teammates,
interviewers). The 10-hex-char id is unguessable, so the link itself is
the capability — same model as GitHub gists and Google Docs "anyone with
the link". Creation/deletion stay key-protected.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/share", tags=["share"])


@router.post("")
async def create_share(body: dict, _: None = Depends(require_api_key)):
    try:
        from app.services.share_service import create_share as _create
        return _create(
            question=body.get("question", ""),
            answer=body.get("answer", ""),
            sources=body.get("sources"),
            repo_url=body.get("repo_url"),
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_shares(
    limit: int = Query(20, ge=1, le=100),
    repo_url: str | None = None,
    _: None = Depends(require_api_key),
):
    try:
        from app.services.share_service import list_shares as _list
        shares = _list(limit=limit, repo_url=repo_url)
        return {"shares": shares, "total": len(shares)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{share_id}")
async def get_share(share_id: str):
    """Public — the link id is the capability (see module docstring)."""
    try:
        from app.services.share_service import get_share as _get
        share = _get(share_id)
        if not share:
            raise HTTPException(status_code=404, detail="Share link not found or expired")
        return share
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{share_id}")
async def delete_share(share_id: str, _: None = Depends(require_api_key)):
    try:
        from app.services.share_service import delete_share as _delete
        if not _delete(share_id):
            raise HTTPException(status_code=404, detail="Share link not found")
        return {"status": "deleted", "id": share_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
