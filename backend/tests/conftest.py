"""
conftest.py — Shared pytest fixtures.

WHY CONFTEST?
pytest automatically loads conftest.py before any test file.
Fixtures defined here are available in all test files in the same directory
and below — no import needed.

We mock out external dependencies (OpenAI, ChromaDB) so tests:
  1. Run without a real API key
  2. Run without a running ChromaDB instance
  3. Run fast (no network calls)
  4. Are deterministic (no flaky LLM responses)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


def _make_fake_settings():
    """Return a Settings-like object with dummy values for all required fields."""
    from app.core.config import Settings
    return Settings(llm_provider="openai", openai_api_key="sk-test-fake-key-for-tests")


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """
    Redirect ALL local SavFlux state (ledger, prompts, snippets, notifications,
    analytics, watcher state, share links, git mirrors) into a per-test tmp dir.

    WHY ONE FIXTURE INSTEAD OF PATCHING EACH SERVICE?
    Tests used to do `monkeypatch.setattr(mod.settings, "chroma_persist_directory", ...)`
    once per service module. That only worked because each service happened to
    snapshot `settings` at import time, and it silently missed any service the
    test forgot to list — a test could pass while writing into the developer's
    real `chroma_data/`.

    Every service now resolves its path through `app.core.paths.data_file()`, so
    patching `data_dir` in that one module isolates all of them at once.
    """
    import app.core.paths as paths

    target = tmp_path / "data"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "data_dir", lambda: target)
    return target


@pytest.fixture(scope="session")
def client():
    """
    FastAPI TestClient — spins up the app in-process.

    WHY scope="session"?
    The app startup (reranker warmup, LangSmith setup) is expensive.
    Session scope means it runs once for the entire test suite, not once per test.

    We patch external dependencies at the session level so no real API calls happen.
    """
    fake_settings = _make_fake_settings()

    # Patch get_settings FIRST — ingestion_service calls it at module import time,
    # before any other patch can take effect. Without this, Settings() fails
    # because OPENAI_API_KEY is not set in the test environment.
    with patch("app.core.config.get_settings", return_value=fake_settings), \
         patch("app.services.ingestion_service.settings", fake_settings), \
         patch("openai.AsyncOpenAI"), \
         patch("chromadb.PersistentClient") as mock_chroma, \
         patch("app.services.reranker._get_cross_encoder"):

        # Make heartbeat() a no-op so the health check's ChromaDB check passes
        mock_chroma.return_value.heartbeat.return_value = True

        # NOW import main — patches are already in place
        from main import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
