"""
limiter.py — Single shared rate limiter instance.

WHY A SEPARATE MODULE?
main.py creates the FastAPI app and imports routers (chat, review).
Those routers need the limiter for @limiter.limit() decorators.
If chat.py imported from main.py it would be a circular import.

Solution: put the limiter in its own module that neither main.py nor
the routers depend on transitively. Both import from here instead.

PROXY-AWARE IP EXTRACTION:
On Railway, Fly.io, or any Nginx/Caddy deployment, request.client.host
is always the reverse-proxy IP — all users share one rate-limit bucket.
_get_real_ip() reads X-Forwarded-For first (set by the proxy to the
true client IP) and falls back to get_remote_address for local dev where
no proxy is present.
"""

from starlette.requests import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _get_real_ip(request: Request) -> str:
    """
    Return the true client IP for rate-limit keying.

    Priority:
      1. X-Forwarded-For header — set by Railway / Nginx to the real client IP.
         We take only the first (leftmost) address; proxies append their own IP
         to the right, so the leftmost value is always the original client.
      2. request.client.host — used in local dev where no proxy is present.

    NOTE: only trust X-Forwarded-For if your deployment guarantees the proxy
    sets it. Railway and most PaaS providers do. If you ever run without a
    trusted proxy, disable this by reverting to get_remote_address.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # "1.2.3.4, 10.0.0.1" → take the leftmost (original client) address
        return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)


# One instance, shared across all routes.
# Each Limiter has its own in-memory counter store — if you create two,
# they track requests independently and rate limits can be bypassed by
# alternating between endpoints.
limiter = Limiter(key_func=_get_real_ip)
