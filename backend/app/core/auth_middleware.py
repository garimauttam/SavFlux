"""Authenticate API calls with Supabase and keep tenant context through streaming."""
from __future__ import annotations

from starlette.responses import JSONResponse

from app.core.tenant import reset_current_user_id, set_current_user_id
from app.services.auth_service import AuthNotConfigured, InvalidAccessToken, verify_access_token


class SupabaseAuthMiddleware:
    """ASGI middleware keeps the verified identity active for the full response body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if not path.startswith("/api/v1/") or method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # A share URL is an explicit bearer capability. Its content is read from
        # the owning user's private share file, not from the current tenant.
        if path.startswith("/api/v1/share/") and method == "GET":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            response = JSONResponse(
                {"detail": "Sign in with Google or email to access this SavFlux workspace."},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        try:
            user = await verify_access_token(token.strip())
        except InvalidAccessToken as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=401)
            await response(scope, receive, send)
            return
        except AuthNotConfigured as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=503)
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = user
        context_token = set_current_user_id(user["id"])
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_user_id(context_token)
