"""Shared dependency for routes that require a Supabase-authenticated user."""

from fastapi import HTTPException, Request, status


def require_api_key(request: Request) -> dict[str, str]:
    """Backward-compatible dependency name; now returns the verified account."""
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in with Google or email to access this SavFlux workspace.",
        )
    return user


# New code should use this descriptive name. Existing route declarations keep
# require_api_key temporarily so the migration remains a small API diff.
require_user = require_api_key
