"""Account session endpoint for the Google/email Supabase Auth flow."""
from fastapi import APIRouter, Depends

from app.api.deps import require_user

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/session")
async def session(user: dict[str, str] = Depends(require_user)) -> dict[str, object]:
    """Return only the verified account identity; never echo access tokens."""
    return {"authenticated": True, "user": {"id": user["id"], "email": user.get("email", "")}}
