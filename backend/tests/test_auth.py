"""Supabase bearer authentication and account-scoped API tests."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


@contextmanager
def _client():
    from app.core.config import Settings
    from app.services.auth_service import InvalidAccessToken

    settings = Settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        supabase_url="https://test-project.supabase.co",
        supabase_anon_key="test-anon-key",
    )

    async def verify(token: str):
        if token != "valid-access-token":
            raise InvalidAccessToken("Invalid or expired test session")
        return {"id": "00000000-0000-4000-8000-000000000001", "email": "owner@example.com"}

    mock_chroma = MagicMock()
    mock_chroma.heartbeat.return_value = True
    with patch("app.core.config.get_settings", return_value=settings), \
         patch("app.services.ingestion_service.settings", settings), \
         patch("app.core.auth_middleware.verify_access_token", new=verify), \
         patch("openai.AsyncOpenAI"), \
         patch("chromadb.PersistentClient", return_value=mock_chroma), \
         patch("app.services.reranker._get_cross_encoder"):
        from main import app
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def test_missing_bearer_is_rejected_with_google_and_email_guidance():
    with _client() as client:
        response = client.get("/api/v1/chat/indexed-files")
        assert response.status_code == 401
        assert "Google or email" in response.json()["detail"]


def test_invalid_bearer_is_rejected():
    with _client() as client:
        response = client.get("/api/v1/chat/indexed-files", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        assert "invalid" in response.json()["detail"].lower()


def test_verified_bearer_protects_routes_and_scopes_account_session():
    with _client() as client:
        headers = {"Authorization": "Bearer valid-access-token"}
        assert client.get("/api/v1/chat/indexed-files", headers=headers).status_code == 200
        # The app-level middleware protects routes even if a route omitted a dependency.
        assert client.get("/api/v1/agent/tools").status_code == 401
        tools = client.get("/api/v1/agent/tools", headers=headers)
        assert tools.status_code == 200

        session = client.get("/api/v1/auth/session", headers=headers)
        assert session.status_code == 200
        assert session.json() == {
            "authenticated": True,
            "user": {"id": "00000000-0000-4000-8000-000000000001", "email": "owner@example.com"},
        }
        assert "valid-access-token" not in session.text


def test_public_share_links_remain_capability_urls():
    with _client() as client:
        with patch("app.services.share_service.get_share", return_value={"id": "abc1234567"}):
            response = client.get("/api/v1/share/abc1234567")
        assert response.status_code == 200


def test_health_is_public_for_platform_probes():
    with _client() as client:
        response = client.get("/health")
        assert response.status_code != 401


def test_auth_service_reuses_recently_verified_identity_without_storing_bearer(monkeypatch):
    import asyncio
    from app.core.config import Settings
    from app.services import auth_service

    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "user-cache-test", "email": "cached@example.com"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            calls.append(True)
            return Response()

    auth_service.clear_auth_cache()
    monkeypatch.setattr(auth_service, "get_settings", lambda: Settings(
        _env_file=None,
        supabase_url="https://cache-test.supabase.co",
        supabase_anon_key="test-anon-key",
    ))
    monkeypatch.setattr(auth_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    try:
        first = asyncio.run(auth_service.verify_access_token("cache-test-token"))
        second = asyncio.run(auth_service.verify_access_token("cache-test-token"))
        assert first == second == {"id": "user-cache-test", "email": "cached@example.com"}
        assert len(calls) == 1
        assert "cache-test-token" not in repr(auth_service._verified_users)
    finally:
        auth_service.clear_auth_cache()


def test_auth_service_fails_closed_when_supabase_is_not_configured(monkeypatch):
    import asyncio
    import pytest
    from app.core.config import Settings
    from app.services import auth_service

    monkeypatch.setattr(auth_service, "get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(auth_service.AuthNotConfigured, match="not configured"):
        asyncio.run(auth_service.verify_access_token("not-a-real-token"))
