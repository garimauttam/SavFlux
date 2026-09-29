import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from app.core.tenant import set_current_user_id, reset_current_user_id
from app.services import (
    model_service as ms,
    provider_connections as conn,
    llm_factory as factory,
)
from app.services.provider_catalog import (
    normalize_model,
    reasoning_options,
    safe_error,
    safe_stream_error,
)
from app.services.model_runtime import RoutedModel, clear, latest

MODELS = [dict(id="openai/gpt-oss-20b", reasoning=["low", "medium", "high"])]


@pytest.mark.asyncio
async def test_multiple_keys_save_without_switching_and_routes_are_explicit(
    isolated_data_dir, monkeypatch
):
    monkeypatch.setattr(conn, "discover", AsyncMock(return_value=MODELS))
    await conn.save("groq", "key-groq-secret", MODELS[0]["id"], "low", True, True)
    await conn.save(
        "openrouter", "key-router-secret", MODELS[0]["id"], "default", True, False
    )
    assert ms._read_provider_keys() == {
        "groq": "key-groq-secret",
        "openrouter": "key-router-secret",
    }
    assert conn.resolve("review") is None
    conn.save_routing("tasks", "groq", {"review": "openrouter", "reasoning": "ollama"})
    assert conn.resolve("chat")["provider"] == "groq"
    assert conn.resolve("review")["provider"] == "openrouter"
    assert conn.resolve("reasoning")["provider"] == "ollama"
    assert "key-groq-secret" not in json.dumps(conn.status())
    assert ms._provider_keys_path().stat().st_mode & 0o777 == 0o600
    conn.remove("groq")
    assert conn.resolve("chat")["provider"] == "ollama"
    assert "openrouter" in ms._read_provider_keys()


@pytest.mark.asyncio
async def test_consent_capability_and_catalog_validation_precede_secret_write(
    isolated_data_dir, monkeypatch
):
    discover = AsyncMock(return_value=MODELS)
    monkeypatch.setattr(conn, "discover", discover)
    for consent, free, model, effort in [
        (False, True, MODELS[0]["id"], "low"),
        (True, False, MODELS[0]["id"], "low"),
        (True, True, "retired", "default"),
        (True, True, MODELS[0]["id"], "none"),
    ]:
        with pytest.raises(ValueError):
            await conn.save("groq", "private-key", model, effort, consent, free)
        assert not ms._read_provider_keys()


def test_openrouter_rejects_paid_unknown_and_multimodal_output():
    base = dict(
        id="vendor/code:free",
        pricing={"prompt": "0", "completion": "0"},
        architecture={"output_modalities": ["text"]},
    )
    assert normalize_model("openrouter", base)
    for overrides in [
        dict(id="vendor/code"),
        dict(pricing={}),
        dict(pricing={"prompt": "0", "completion": "0.001"}),
        dict(pricing={"prompt": "0", "completion": "0", "request": "0.1"}),
        dict(pricing={"prompt": "-1"}),
    ]:
        assert normalize_model("openrouter", {**base, **overrides}) is None


def test_reasoning_is_provider_specific_and_unknown_models_get_no_fake_controls():
    assert reasoning_options("groq", "openai/gpt-oss-20b", {}) == [
        "low",
        "medium",
        "high",
    ]
    assert reasoning_options("groq", "new-model", {}) == []
    assert reasoning_options("gemini", "gemini-2.5-flash", {}) == [
        "none",
        "low",
        "medium",
        "high",
    ]
    assert (
        reasoning_options(
            "openrouter", "x:free", {"supported_parameters": ["reasoning"]}
        )
        == []
    )
    assert reasoning_options(
        "openrouter",
        "x:free",
        {"reasoning": {"supported_efforts": ["none", "high"], "mandatory": True}},
    ) == ["high"]


def test_safe_errors_never_echo_secrets_or_provider_bodies():
    error = httpx.HTTPStatusError(
        "key-SENSITIVE and code",
        request=httpx.Request("GET", "https://example.com"),
        response=httpx.Response(429),
    )
    assert "quota" in safe_error(error)
    assert "SENSITIVE" not in safe_error(error)
    assert "SENSITIVE" not in safe_stream_error("__ERROR__key-SENSITIVE__ERROR_END__")


@pytest.mark.asyncio
async def test_local_fallback_tracks_provenance_and_cools_down_429():
    clear()
    error = httpx.HTTPStatusError(
        "secret",
        request=httpx.Request("GET", "https://example.com"),
        response=httpx.Response(429),
    )
    primary = SimpleNamespace(ainvoke=AsyncMock(side_effect=error))
    fallback = SimpleNamespace(
        ainvoke=AsyncMock(return_value=AIMessage(content="local answer"))
    )
    model = RoutedModel(primary, fallback, "groq", "gpt-oss", "small:7b")
    assert (await model.ainvoke("hello")).content == "local answer"
    assert latest()["fallback"] and latest()["provider"] == "ollama"
    await model.ainvoke("again")
    primary.ainvoke.assert_awaited_once()
    assert "secret" not in json.dumps(latest())
    clear()


@pytest.mark.asyncio
async def test_partial_stream_is_not_spliced_with_local_model():
    clear()

    async def broken(*args, **kwargs):
        yield AIMessageChunk(content="partial cloud")
        raise RuntimeError("failure")

    fallback = Mock()
    model = RoutedModel(
        SimpleNamespace(astream=broken), fallback, "groq", "cloud", "local"
    )
    with pytest.raises(RuntimeError):
        _ = [c async for c in model.astream("hello")]
    fallback.astream.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_does_not_start_local_fallback():
    clear()
    primary = SimpleNamespace(ainvoke=AsyncMock(side_effect=asyncio.CancelledError()))
    fallback = SimpleNamespace(ainvoke=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await RoutedModel(primary, fallback, "groq", "cloud", "local").ainvoke("hello")
    fallback.ainvoke.assert_not_called()


def test_new_routes_preserve_embedding_provider(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(ms, "active_provider", lambda: "openai")
    conn.save_routing("single", "ollama", {})
    assert ms.read_selection()["embedding_provider"] == "openai"
    assert ms.read_selection()["summary_mixture_models"] == []


@pytest.mark.asyncio
async def test_connection_api_auth_consent_and_no_secret_echo(
    client, isolated_data_dir, monkeypatch
):
    monkeypatch.setattr(conn, "discover", AsyncMock(return_value=MODELS))
    body = dict(
        provider="groq",
        api_key="super-secret-123",
        model=MODELS[0]["id"],
        reasoning="low",
        consent=True,
        free_plan_confirmed=True,
    )
    assert (
        client.put(
            "/api/v1/models/connections", headers={"Authorization": ""}, json=body
        ).status_code
        == 401
    )
    res = client.put("/api/v1/models/connections", json={**body, "consent": False})
    assert res.status_code == 400 and body["api_key"] not in res.text, res.text
    res = client.put("/api/v1/models/connections", json=body)
    assert res.status_code == 200 and body["api_key"] not in res.text
    assert res.json()["providers"][0]["key_configured"]


def test_factory_uses_extra_body_for_pinned_sdk_and_local_only_fallback(
    isolated_data_dir, monkeypatch
):
    monkeypatch.setattr(
        conn,
        "resolve",
        lambda task: dict(
            provider="groq",
            model="openai/gpt-oss-20b",
            reasoning="low",
            reasoning_options=["low"],
        ),
    )
    monkeypatch.setattr(ms, "provider_api_key", lambda p: "private-key")
    monkeypatch.setattr(ms, "active_review_model", lambda: "review:7b")
    local = Mock()
    monkeypatch.setattr(factory, "_build_chat_llm", lambda *args, **kwargs: local)
    create = Mock()
    monkeypatch.setattr("langchain_openai.ChatOpenAI", create)
    factory.get_chat_llm.cache_clear()
    model = factory.get_chat_llm(task="coding")
    assert model.fallback is local
    assert create.call_args.kwargs["model_kwargs"]["extra_body"] == {
        "reasoning_effort": "low"
    }
    assert create.call_args.kwargs["max_retries"] == 0
    factory.get_chat_llm.cache_clear()


def test_keys_and_runtime_events_are_isolated_between_users(tmp_path, monkeypatch):
    import app.core.paths as paths

    monkeypatch.setattr(
        paths,
        "get_settings",
        lambda: SimpleNamespace(chroma_persist_directory=str(tmp_path)),
    )
    a = set_current_user_id("alice")
    try:
        ms._write_provider_keys({"groq": "alice-secret"})
        RoutedModel(None, None, "groq", "x", "y")._event("groq", "x")
        b = set_current_user_id("bob")
        try:
            assert ms._read_provider_keys() == {}
            assert latest() is None
        finally:
            reset_current_user_id(b)
        assert ms._read_provider_keys()["groq"] == "alice-secret"
    finally:
        reset_current_user_id(a)


def test_review_circuit_is_tenant_scoped(isolated_data_dir):
    from app.services.multi_review_agent import _TenantCircuit

    circuit = _TenantCircuit()
    a = set_current_user_id("alice")
    try:
        for _ in range(3):
            circuit.record_failure("failed")
        assert not circuit.allows_call()
        b = set_current_user_id("bob")
        try:
            assert circuit.allows_call()
        finally:
            reset_current_user_id(b)
    finally:
        reset_current_user_id(a)


@pytest.mark.asyncio
async def test_old_ollama_tool_binding_uses_fast_review_instead_of_failing(monkeypatch):
    from app.services import review_agent

    llm = Mock()
    llm.bind_tools.side_effect = NotImplementedError()
    monkeypatch.setattr(review_agent, "get_review_llm", lambda *args, **kwargs: llm)

    async def fast(*args, **kwargs):
        yield "model text review"

    monkeypatch.setattr(review_agent, "stream_fast_code_review", fast)
    text = "".join(
        [t async for t in review_agent.stream_code_review("a.py", "x = 1", "py")]
    )
    assert "model text review" in text
    assert "Tool binding is unavailable" in text


@pytest.mark.asyncio
async def test_pinned_sdk_serializes_reasoning_and_zero_price_at_top_level():
    from langchain_openai import ChatOpenAI
    from openai._client import AsyncOpenAI

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "model": "x:free",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        llm = ChatOpenAI(
            model="x:free",
            api_key="fake-test-key",
            base_url="https://openrouter.ai/api/v1",
            async_client=AsyncOpenAI(
                api_key="fake-test-key",
                base_url="https://openrouter.ai/api/v1",
                http_client=client,
            ).chat.completions,
            http_async_client=client,
            max_retries=0,
            model_kwargs={
                "extra_body": {
                    "reasoning": {"effort": "low", "exclude": True},
                    "provider": {
                        "max_price": {"prompt": 0, "completion": 0},
                        "allow_fallbacks": False,
                    },
                }
            },
        )
        assert (await llm.ainvoke("Reply OK")).content == "OK"
    assert requests[0]["provider"]["max_price"] == {"prompt": 0, "completion": 0}
    assert requests[0]["reasoning"]["effort"] == "low"
    assert "extra_body" not in requests[0]


def test_catalog_endpoint_is_body_validated_and_authenticated(client, monkeypatch):
    monkeypatch.setattr("app.api.models.discover", AsyncMock(return_value=MODELS))
    response = client.post(
        "/api/v1/models/catalog",
        json={"provider": "groq", "api_key": "sample-test-key"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["models"] == MODELS
    assert "sample-test-key" not in response.text
    assert (
        client.post(
            "/api/v1/models/catalog", json={"provider": "custom-host"}
        ).status_code
        == 422
    )


def test_probe_endpoint_never_sends_repository_context(
    client, isolated_data_dir, monkeypatch
):
    monkeypatch.setattr(ms, "active_review_model", lambda: "small:7b")
    calls = []
    original = httpx.AsyncClient

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "OK"})

    monkeypatch.setattr("app.api.models.httpx", httpx, raising=False)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    response = client.post("/api/v1/models/test", json={"provider": "ollama"})
    assert response.status_code == 200, response.text
    assert response.json()["ok"]
    assert calls == [
        {
            "model": "small:7b",
            "prompt": "Reply with OK.",
            "stream": False,
            "options": {"num_predict": 32},
        }
    ]
