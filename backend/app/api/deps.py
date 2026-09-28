"""
deps.py — Shared FastAPI authentication dependency.

SavFlux is single-owner in this release. The owner credential comes from
`API_KEY` in the environment, or from the random 256-bit key generated on first
startup and saved in the configured data directory. There is deliberately no
public-registration flow or user database.

The dependency uses `X-API-Key` because the backend remains usable by scripts
and local tools. The browser sign-in page holds that credential in
`sessionStorage` for the current tab and sends it in the header; it is never
returned by an endpoint.

This fails closed: if the owner key is missing, *every* protected route refuses
the request, including localhost. A loopback exception is unsafe behind a local
reverse proxy, where a remote browser's socket peer can appear to be 127.0.0.1.
`/health` remains intentionally public for platform health checks, and
capability-based `GET /api/v1/share/{id}` remains public by design.
"""

import hmac

from fastapi import Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader

from app.services.owner_key import effective_owner_key

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key_header: str | None = Security(_api_key_header)) -> None:
    """Require the configured single-instance owner key."""
    expected = effective_owner_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "SavFlux owner authentication is not configured. Set API_KEY, "
                "or restart with a writable persistent data directory so the "
                "first-run owner key can be created."
            ),
        )

    # Constant-time comparison avoids leaking matching prefixes through timing.
    provided = api_key_header or ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Invalid or missing owner key. Send it as the X-API-Key header, "
                "or paste it into the sign-in screen. The key is in the backend "
                "log from first run, or in the configured data directory's owner_key file."
            ),
        )
