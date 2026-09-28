"""Single-owner API-key authentication tests.

The owner key protects every /api/v1 route, not only routes which remembered
an explicit FastAPI dependency. Local loopback remains zero-config only before
a stored key exists; generated keys and explicit API_KEYs require sign-in.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _make_client(api_key: str | None):
    from app.core.config import Settings

    fake_settings = Settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        api_key=api_key,
    )
    expected_key = api_key or ""
    mock_chroma = MagicMock()
    mock_chroma._collection.get.return_value = {"metadatas": None}

    with patch("app.core.config.get_settings", return_value=fake_settings), \
         patch("app.services.owner_key.get_settings", return_value=fake_settings), \
         patch("app.api.deps.effective_owner_key", return_value=expected_key), \
         patch("app.services.owner_key.effective_owner_key", return_value=expected_key), \
         patch("app.services.owner_key.load_or_create_owner_key", return_value=(expected_key, False)), \
         patch("app.services.ingestion_service.settings", fake_settings), \
         patch("openai.AsyncOpenAI"), \
         patch("chromadb.PersistentClient", return_value=mock_chroma), \
         patch("app.services.reranker._get_cross_encoder"), \
         patch("app.services.retrieval_service._get_vectorstore", return_value=mock_chroma):
        from main import app
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def test_unconfigured_owner_key_fails_closed_even_for_loopback():
    for client in _make_client(api_key=None):
        response = client.get("/api/v1/chat/indexed-files")
        assert response.status_code == 503
        assert "authentication is not configured" in response.json()["detail"].lower()


def test_missing_header_returns_401_when_key_is_set():
    for client in _make_client(api_key="supersecret"):
        response = client.get("/api/v1/chat/indexed-files")
        assert response.status_code == 401


def test_wrong_key_returns_401():
    for client in _make_client(api_key="supersecret"):
        response = client.get(
            "/api/v1/chat/indexed-files",
            headers={"X-API-Key": "wrongkey"},
        )
        assert response.status_code == 401


def test_correct_key_passes_through():
    for client in _make_client(api_key="supersecret"):
        response = client.get(
            "/api/v1/chat/indexed-files",
            headers={"X-API-Key": "supersecret"},
        )
        assert response.status_code == 200


def test_api_routes_without_route_dependency_are_also_protected():
    # /agent/tools did not previously declare Depends(require_api_key). The
    # app-level gate means a future route cannot accidentally become public.
    for client in _make_client(api_key="supersecret"):
        assert client.get("/api/v1/agent/tools").status_code == 401
        assert client.get(
            "/api/v1/agent/tools", headers={"X-API-Key": "supersecret"}
        ).status_code == 200


def test_signin_check_never_returns_the_supplied_key():
    for client in _make_client(api_key="supersecret"):
        response = client.post(
            "/api/v1/auth/session", headers={"X-API-Key": "supersecret"}
        )
        assert response.status_code == 200
        assert response.json() == {"authenticated": True}
        assert "supersecret" not in response.text


def test_public_share_links_remain_public():
    for client in _make_client(api_key="supersecret"):
        with patch("app.services.share_service.get_share", return_value={"id": "abc1234567"}):
            response = client.get("/api/v1/share/abc1234567")
        assert response.status_code == 200


def test_health_always_passes_without_key():
    for client in _make_client(api_key="supersecret"):
        response = client.get("/health")
        assert response.status_code != 401
