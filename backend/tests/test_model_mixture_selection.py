from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services import model_service


MODELS = [
    {"name": "qwen2.5-coder:7b", "size": 4_700_000_000, "details": {"parameter_size": "7B"}},
    {"name": "deepseek-r1:7b", "size": 4_700_000_000, "details": {"parameter_size": "7B"}},
]


@pytest.fixture
def model_state(tmp_path, monkeypatch):
    selection_path = tmp_path / "model_selection.json"
    settings = SimpleNamespace(
        summary_mixture_models="qwen2.5-coder:7b,deepseek-r1:7b",
        ollama_chat_model="qwen2.5-coder:7b",
        ollama_review_model="",
        ollama_base_url="http://ollama.test",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        llm_provider="ollama",
        openai_api_key=None,
        openai_chat_model="gpt-4o",
        openai_embedding_model="text-embedding-3-small",
        deepseek_api_key=None,
        deepseek_chat_model="deepseek-chat",
        deepseek_base_url="https://api.deepseek.com",
        openrouter_api_key=None,
        openrouter_chat_model="openai/gpt-4o-mini",
        openrouter_base_url="https://openrouter.ai/api/v1",
    )
    provider_keys_path = tmp_path / "provider_api_keys.json"

    async def tags():

        return {"models": MODELS}

    monkeypatch.setattr(model_service, "_selection_path", lambda: selection_path)
    monkeypatch.setattr(model_service, "_provider_keys_path", lambda: provider_keys_path)
    monkeypatch.setattr(model_service, "get_settings", lambda: settings)
    monkeypatch.setattr(model_service, "_sync_tags", lambda: {"models": MODELS})
    monkeypatch.setattr(model_service, "_async_tags", tags)
    monkeypatch.setattr(model_service, "reset_llm_cache", lambda: None)
    return selection_path


def test_moa_selection_defaults_to_config_and_app_empty_selection_disables(model_state):
    assert model_service.active_summary_mixture_models() == [
        "qwen2.5-coder:7b",
        "deepseek-r1:7b",
    ]

    model_service.write_selection(summary_mixture_models=[])
    assert model_service.active_summary_mixture_models() == []
    assert json.loads(model_state.read_text())["summary_mixture_models"] == []


@pytest.mark.asyncio
async def test_selecting_installed_moa_models_is_persisted_and_reported(model_state):
    status = await model_service.select_model(
        summary_mixture_models=["qwen2.5-coder:7b", "deepseek-r1:7b", "qwen2.5-coder:7b"]
    )

    assert status["summary_mixture_models"] == ["qwen2.5-coder:7b", "deepseek-r1:7b"]
    assert status["summary_mixture_active"] is True
    assert status["summary_mixture_missing_models"] == []
    assert json.loads(model_state.read_text())["summary_mixture_models"] == status["summary_mixture_models"]


@pytest.mark.asyncio
async def test_moa_rejects_single_or_uninstalled_model(model_state):
    with pytest.raises(model_service.ModelNotInstalled, match="at least two"):
        await model_service.select_model(summary_mixture_models=["qwen2.5-coder:7b"])

    with pytest.raises(model_service.ModelNotInstalled, match="not installed"):
        await model_service.select_model(
            summary_mixture_models=["qwen2.5-coder:7b", "not-pulled:latest"]
        )
    assert not model_state.exists()


def test_moa_app_selection_overrides_env_configuration(model_state):
    model_service.write_selection(summary_mixture_models=["deepseek-r1:7b", "qwen2.5-coder:7b"])
    assert model_service.active_summary_mixture_models() == [
        "deepseek-r1:7b",
        "qwen2.5-coder:7b",
    ]


@pytest.mark.asyncio
async def test_provider_key_is_saved_private_and_never_returned(model_state):
    model_service.save_provider_config("openai", "sk-test-credential-123", "gpt-4o-mini")
    status = await model_service.models_status()
    key_path = model_service._provider_keys_path()

    assert model_service.active_provider() == "openai"
    assert model_service.active_provider_model() == "gpt-4o-mini"
    assert model_service.provider_api_key() == "sk-test-credential-123"
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert status["provider_key_configured"] is True
    assert status["provider_key_source"] == "app"
    assert "sk-test-credential-123" not in json.dumps(status)

    assert model_service.forget_provider_key("openai") is True
    assert model_service.provider_api_key() == ""
    assert key_path.exists() is False


def test_selecting_hosted_provider_without_key_is_rejected(model_state):
    with pytest.raises(ValueError, match="Add an OpenAI API key"):
        model_service.save_provider_config("openai", model="gpt-4o")


def test_openrouter_factory_uses_the_server_key_and_selected_model(model_state, monkeypatch):
    from app.services import llm_factory

    model_service.save_provider_config("openrouter", "sk-or-server-only-key", "qwen/qwen3-coder")
    captured = {}

    def fake_chat_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", fake_chat_openai)
    monkeypatch.setattr(llm_factory, "_settings", model_service.get_settings)
    llm_factory._build_chat_llm("openrouter", streaming=True)

    assert captured["api_key"] == "sk-or-server-only-key"
    assert captured["model"] == "qwen/qwen3-coder"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
