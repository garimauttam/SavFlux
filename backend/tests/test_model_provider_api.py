from __future__ import annotations


def test_provider_key_api_stores_secret_server_side_and_never_returns_it(
    client, isolated_data_dir, monkeypatch
):
    from app.services import model_service

    installed = [
        {"name": "qwen2.5-coder:7b", "size": 4_700_000_000, "details": {"parameter_size": "7B"}},
    ]

    async def tags():
        return {"models": installed}

    monkeypatch.setattr(model_service, "_async_tags", tags)
    monkeypatch.setattr(model_service, "_sync_tags", lambda: {"models": installed})
    monkeypatch.setattr(model_service, "_ping", lambda: True)

    secret = "sk-user-owned-provider-key-123"
    saved = client.post(
        "/api/v1/models/provider",
        json={"provider": "openai", "api_key": secret, "model": "gpt-4o-mini"},
    )
    assert saved.status_code == 200
    payload = saved.json()
    assert payload["provider"] == "openai"
    assert payload["provider_model"] == "gpt-4o-mini"
    assert payload["provider_key_configured"] is True
    assert payload["provider_key_source"] == "app"
    assert secret not in saved.text

    path = model_service._provider_keys_path()
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert secret in path.read_text(encoding="utf-8")

    deleted = client.delete("/api/v1/models/provider/key/openai")
    assert deleted.status_code == 200
    assert deleted.json()["removed"] is True
    assert secret not in deleted.text
    assert not path.exists()


def test_provider_key_api_requires_owner_authentication(client):
    denied = client.post(
        "/api/v1/models/provider",
        headers={"Authorization": "Bearer invalid-test-access-token"},
        json={"provider": "openai", "api_key": "sk-not-real-key"},
    )
    assert denied.status_code == 401
