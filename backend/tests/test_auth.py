"""
test_auth.py — Tests for the API key authentication dependency.

Verifies:
  1. When API_KEY is not set, all requests pass through (open/dev mode).
  2. When API_KEY is set, requests without the header get 401.
  3. When API_KEY is set, requests with the wrong key get 401.
  4. When API_KEY is set, requests with the correct key pass through.

We test via the /api/v1/chat/indexed-files endpoint (cheap GET, no mocking needed
beyond ChromaDB) so we exercise the full middleware stack.
"""

import pytest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _make_client(api_key: str | None):
    """
    Spin up a fresh TestClient with the given API_KEY setting.
    We need a new client per test because Settings is cached via lru_cache —
    we patch get_settings in deps.py to return the desired key value.
    """
    from app.core.config import Settings

    fake_settings = Settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        api_key=api_key,
    )

    mock_chroma = MagicMock()
    mock_chroma._collection.get.return_value = {"metadatas": None}

    with patch("app.core.config.get_settings", return_value=fake_settings), \
         patch("app.api.deps.get_settings", return_value=fake_settings), \
         patch("app.services.ingestion_service.settings", fake_settings), \
         patch("openai.AsyncOpenAI"), \
         patch("chromadb.PersistentClient", return_value=mock_chroma), \
         patch("app.services.reranker._get_cross_encoder"), \
         patch("app.services.retrieval_service._get_vectorstore", return_value=mock_chroma):

        from main import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ── Test 1: Open mode (no API_KEY configured) ─────────────────────────────────

def test_no_api_key_configured_allows_all_requests():
    """
    When API_KEY is not set in settings, requests without a header pass through.
    This is the default dev-mode behaviour — zero friction locally.
    """
    for client in _make_client(api_key=None):
        response = client.get("/api/v1/chat/indexed-files")
        assert response.status_code == 200


# ── Test 2: Protected mode — missing header ───────────────────────────────────

def test_missing_header_returns_401_when_key_is_set():
    """
    When API_KEY is configured, a request without X-API-Key must return 401.
    """
    for client in _make_client(api_key="supersecret"):
        response = client.get("/api/v1/chat/indexed-files")
        assert response.status_code == 401
        assert "API key" in response.json()["detail"]


# ── Test 3: Protected mode — wrong key ────────────────────────────────────────

def test_wrong_key_returns_401():
    """
    When API_KEY is configured, a request with the wrong key must return 401.
    """
    for client in _make_client(api_key="supersecret"):
        response = client.get(
            "/api/v1/chat/indexed-files",
            headers={"X-API-Key": "wrongkey"},
        )
        assert response.status_code == 401


# ── Test 4: Protected mode — correct key ─────────────────────────────────────

def test_correct_key_passes_through():
    """
    When API_KEY is configured and the correct key is sent, the request succeeds.
    """
    for client in _make_client(api_key="supersecret"):
        response = client.get(
            "/api/v1/chat/indexed-files",
            headers={"X-API-Key": "supersecret"},
        )
        assert response.status_code == 200


# ── Test 5: /health is always unprotected ─────────────────────────────────────

def test_health_always_passes_without_key():
    """
    /health must never require an API key — Railway health checks don't send headers.
    """
    for client in _make_client(api_key="supersecret"):
        response = client.get("/health")
        # Health may return 200 or 503 depending on mock state — never 401
        assert response.status_code != 401
