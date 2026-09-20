"""
test_health.py — Tests for the /health endpoint.

The health endpoint is the most important endpoint:
  - Railway uses it to decide if your instance is healthy
  - It's the first thing you check when a deploy fails
  - It validates that the configured LLM provider and ChromaDB are reachable

These tests verify that:
  1. A healthy system returns 200 with status "ok"
  2. A degraded system (bad API key) returns 503 with status "degraded"

WHY "llm" NOT "openai"?
After the LLM_PROVIDER refactor the health endpoint uses a provider-neutral
"llm" key so the same check works for both OpenAI and Gemini providers.
"""

from unittest.mock import AsyncMock, patch, MagicMock
import pytest


def test_health_returns_200_when_all_checks_pass(client):
    """
    Happy path: both the LLM provider (OpenAI by default) and ChromaDB respond.
    Expected: HTTP 200, status "ok", checks["llm"] starts with "ok".
    """
    mock_openai_client = MagicMock()
    mock_openai_client.models.list = AsyncMock(return_value=[])

    mock_chroma_client = MagicMock()
    mock_chroma_client.heartbeat.return_value = True

    with patch("openai.AsyncOpenAI", return_value=mock_openai_client), \
         patch("chromadb.PersistentClient", return_value=mock_chroma_client):

        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Provider-neutral key — works for both openai and gemini providers
    assert body["checks"]["llm"].startswith("ok")
    assert body["checks"]["chromadb"] == "ok"


def test_health_returns_503_when_openai_fails(client):
    """
    Degraded path: OpenAI throws an AuthenticationError (wrong API key).
    Expected: HTTP 503, status "degraded", checks["llm"] contains "error".

    WHY THIS MATTERS:
    A shallow health check (return {"status": "ok"}) would return 200 here.
    Our deep check catches the bad API key and returns 503, so Railway
    won't route traffic to a misconfigured instance.
    """
    import openai
    from app.core.config import Settings

    fake_openai_settings = Settings(llm_provider="openai", openai_api_key="sk-invalid")

    mock_openai_client = MagicMock()
    mock_openai_client.models.list = AsyncMock(
        side_effect=openai.AuthenticationError(
            "Incorrect API key", response=MagicMock(), body={}
        )
    )

    mock_chroma_client = MagicMock()
    mock_chroma_client.heartbeat.return_value = True

    with patch("main.settings", fake_openai_settings), \
         patch("openai.AsyncOpenAI", return_value=mock_openai_client), \
         patch("chromadb.PersistentClient", return_value=mock_chroma_client):

        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    # Provider-neutral key — was "openai" before the LLM_PROVIDER refactor
    assert "error" in body["checks"]["llm"]
    assert body["checks"]["chromadb"] == "ok"


def test_health_covers_every_configured_provider(client):
    """
    Regression guard: /health must understand every value LLM_PROVIDER accepts.

    The endpoint previously branched on a provider literal ("openai_compatible")
    that config.py does not define. Every real provider therefore fell through to
    the `else` arm, which read settings fields that do not exist — raising
    AttributeError and returning 500 instead of 200/503. A deployment health
    check that always 500s means Railway/Render never routes traffic at all.

    This test walks the Literal in Settings so adding a provider without wiring
    it into /health fails here rather than in production.
    """
    import typing
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.core.config import Settings

    providers = typing.get_args(Settings.model_fields["llm_provider"].annotation)
    assert providers, "llm_provider is expected to be a Literal of provider names"

    mock_openai_client = MagicMock()
    mock_openai_client.models.list = AsyncMock(return_value=[])
    mock_chroma_client = MagicMock()
    mock_chroma_client.heartbeat.return_value = True

    for provider in providers:
        provider_settings = Settings(
            llm_provider=provider,
            openai_api_key="sk-test",
            deepseek_api_key="sk-test",
        )
        mock_ollama_response = MagicMock()
        mock_ollama_response.raise_for_status.return_value = None
        mock_httpx = MagicMock()
        mock_httpx.__aenter__ = AsyncMock(return_value=mock_httpx)
        mock_httpx.__aexit__ = AsyncMock(return_value=False)
        mock_httpx.get = AsyncMock(return_value=mock_ollama_response)

        with patch("main.settings", provider_settings), \
             patch("app.core.config.get_settings", return_value=provider_settings), \
             patch("openai.AsyncOpenAI", return_value=mock_openai_client), \
             patch("httpx.AsyncClient", return_value=mock_httpx), \
             patch("chromadb.PersistentClient", return_value=mock_chroma_client):
            response = client.get("/health")

        assert response.status_code == 200, (
            f"provider {provider!r} is not handled by /health: {response.text}"
        )
        body = response.json()
        assert body["checks"]["llm"].startswith("ok"), provider
        # The label must name the vendor, never leak a raw base URL / API key.
        assert "://" not in body["checks"]["llm"], provider
