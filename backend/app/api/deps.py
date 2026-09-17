"""
deps.py — Shared FastAPI dependencies.

WHY A SEPARATE DEPS FILE?
FastAPI dependencies are reusable callables injected via Depends().
Keeping them here (not inside route files) means any router can import
them without circular imports, and tests can patch a single location.

AUTHENTICATION DESIGN:
  - API_KEY is optional in Settings. If unset, all requests pass through
    (safe for local dev — zero config needed).
  - If API_KEY is set (production / Railway), every protected endpoint
    requires the header:  X-API-Key: <your-key>
  - /health and GET /metrics are intentionally left unprotected so
    Railway health checks and monitoring tools always work.

HOW TO USE IN A ROUTE:
    from app.api.deps import require_api_key

    @router.post("/stream")
    async def my_route(request: Request, _: None = Depends(require_api_key)):
        ...

HOW TO GENERATE A KEY:
    python -c "import secrets; print(secrets.token_hex(32))"
"""

import hmac

from fastapi import Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader

from app.core.config import get_settings

# FastAPI's built-in API key header scheme.
# auto_error=False means we handle the missing-header case ourselves
# so we can give a clearer error message and support the "no key set = open" mode.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key_header: str | None = Security(_api_key_header)) -> None:
    """
    FastAPI dependency that enforces API key authentication.

    Behaviour:
      - API_KEY not configured in settings  → always passes (open / dev mode)
      - API_KEY configured, header present and matches  → passes
      - API_KEY configured, header missing or wrong     → 401 Unauthorized

    WHY 401 AND NOT 403?
    401 = "I don't know who you are" (unauthenticated).
    403 = "I know who you are but you're not allowed" (unauthorised).
    A missing or wrong API key is an authentication failure → 401.
    """
    settings = get_settings()

    # No key configured → open mode, always allow
    if not settings.api_key:
        return

    # Use hmac.compare_digest for constant-time comparison.
    # Plain string equality (!=) is not constant-time: Python short-circuits
    # at the first differing character, leaking key length via response latency.
    # An attacker can oracle individual characters by timing many requests.
    # compare_digest always takes the same time regardless of where strings differ.
    provided = api_key_header or ""
    if not hmac.compare_digest(provided, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Set the X-API-Key header.",
        )
