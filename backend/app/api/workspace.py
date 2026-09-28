"""Authenticated APIs for the isolated, persistent Agent Git worktree."""
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key
from app.services import worktree_service

router = APIRouter(prefix="/workspace", tags=["workspace"])


def _workspace_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc) or "Agent workspace is unavailable.")


@router.get("/status")
def workspace_status(
    repo: str = Query(..., min_length=3, max_length=300),
    _: None = Depends(require_api_key),
):
    """Read the current local diff for this repo's saved Agent branch."""
    try:
        return worktree_service.workspace_status(repo)
    except (RuntimeError, ValueError) as exc:
        raise _workspace_error(exc) from exc


@router.post("/reset")
def reset_workspace(
    repo: str = Query(..., min_length=3, max_length=300),
    _: None = Depends(require_api_key),
):
    """Discard local Agent edits in the isolated worktree; never touches GitHub."""
    try:
        return worktree_service.reset_workspace(repo)
    except (RuntimeError, ValueError) as exc:
        raise _workspace_error(exc) from exc
