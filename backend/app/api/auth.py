"""Authentication endpoints for the single-owner SavFlux instance.

There is intentionally no registration endpoint: this version is single-tenant
and has no user database. The operator's random owner key is the sign-in
credential. A successful session check never returns or echoes the key.
"""

from fastapi import APIRouter, Depends

from app.api.deps import require_api_key

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/session")
async def validate_session(_: None = Depends(require_api_key)) -> dict[str, bool]:
    """Validate the supplied X-API-Key and establish the frontend session."""
    return {"authenticated": True}
