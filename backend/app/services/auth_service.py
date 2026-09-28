"""Supabase Auth token verification; no passwords or refresh tokens are stored here."""
from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from app.core.config import get_settings


class AuthNotConfigured(RuntimeError):
    pass


class InvalidAccessToken(ValueError):
    pass


# Supabase's user endpoint is authoritative but remote. Reuse only successful,
# already-verified identities briefly to avoid adding a network round trip to every
# API request. Keys are token digests, never bearer tokens; expiry bounds revocation
# lag to at most 15 seconds.
_AUTH_CACHE_TTL_SECONDS = 15.0
_AUTH_CACHE_MAX_ENTRIES = 4096
_verified_users: dict[str, tuple[float, dict[str, str]]] = {}


def clear_auth_cache() -> None:
    """Clear verified identities; exposed for tests and operational key rotation."""
    _verified_users.clear()


async def verify_access_token(access_token: str) -> dict[str, Any]:
    """Ask Supabase Auth to validate a bearer token and return its trusted user.

    The public anon key is used only as the project identifier. The user's
    bearer token is never logged or persisted by SavFlux.
    """
    settings = get_settings()
    base_url = (settings.supabase_url or "").strip().rstrip("/")
    anon_key = (settings.supabase_anon_key or "").strip()
    if not base_url or not anon_key:
        raise AuthNotConfigured("Supabase Auth is not configured on this SavFlux server.")

    cache_key = hashlib.sha256(f"{base_url}:{access_token}".encode("utf-8")).hexdigest()
    cached = _verified_users.get(cache_key)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return dict(cached[1])
    if cached is not None:
        _verified_users.pop(cache_key, None)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            response = await client.get(
                urljoin(base_url + "/", "auth/v1/user"),
                headers={"apikey": anon_key, "Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise AuthNotConfigured("Could not reach Supabase Auth to verify your session.") from exc

    if response.status_code in (401, 403):
        raise InvalidAccessToken("Your sign-in session is invalid or expired. Please sign in again.")
    if response.status_code != 200:
        raise AuthNotConfigured("Supabase Auth could not verify the session right now.")

    try:
        user = response.json()
        user_id = str(user["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidAccessToken("Supabase returned an invalid user session.") from exc
    if not user_id or len(user_id) > 64:
        raise InvalidAccessToken("Supabase returned an invalid user session.")
    verified = {"id": user_id, "email": str(user.get("email") or "")}
    if len(_verified_users) >= _AUTH_CACHE_MAX_ENTRIES:
        expired = [key for key, (expiry, _) in _verified_users.items() if expiry <= now]
        for key in expired:
            _verified_users.pop(key, None)
        if len(_verified_users) >= _AUTH_CACHE_MAX_ENTRIES:
            _verified_users.pop(next(iter(_verified_users)))
    _verified_users[cache_key] = (now + _AUTH_CACHE_TTL_SECONDS, verified)
    return dict(verified)
