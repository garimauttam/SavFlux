"""User-consented BYOK connections and explicit task routing. No review/prompt cache."""

import json
import os
import tempfile
import threading
from app.services import model_service as ms
from app.services.provider_catalog import PROVIDERS, discover

_LOCK = threading.RLock()
TASKS = ("chat", "coding", "review", "reasoning")


def _write_selection(selected: dict):
    path = ms._selection_path()
    fd, name = tempfile.mkstemp(prefix=".model-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(selected, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            name, path
        )  # mkstemp uses 0600; credentials remain in separate file.
    finally:
        if os.path.exists(name):
            os.unlink(name)


def status() -> dict:
    selected = ms.read_selection()
    connections = selected.get("connections") or {}
    keys = ms._read_provider_keys()
    return {
        "providers": [
            {
                "id": p,
                **meta,
                "key_configured": bool(ms.provider_api_key(p)),
                "key_source": (
                    "app" if p in keys else "env" if ms.provider_api_key(p) else None
                ),
                "selection": connections.get(p),
            }
            for p, meta in PROVIDERS.items()
        ],
        "routing": selected.get("routing")
        or {"mode": "single", "single": "ollama", "tasks": {}},
        "managed": bool(selected.get("routing")),
    }


def resolve(task: str) -> dict | None:
    selected = ms.read_selection()
    routing = selected.get("routing")
    if not routing:
        return None  # Keep existing deployment/provider configuration until explicit opt-in.
    provider = (
        routing.get("tasks", {}).get(task, routing["single"])
        if routing["mode"] == "tasks"
        else routing["single"]
    )
    if provider == "ollama":
        return {"provider": "ollama"}
    connection = (selected.get("connections") or {}).get(provider)
    if not connection:
        raise ValueError("The routed provider has no saved model configuration.")
    return {"provider": provider, **connection}


async def save(
    provider: str,
    key: str,
    model: str,
    reasoning: str,
    consent: bool,
    free_plan_confirmed: bool,
):
    if not consent:
        raise ValueError(
            "Consent is required to store this connection and send code context to the chosen provider."
        )
    if provider != "openrouter" and not free_plan_confirmed:
        raise ValueError(
            "Confirm your provider account uses its free tier. SavFlux cannot determine billing status from a key."
        )
    key = key.strip()
    if key and (len(key) < 8 or len(key) > 500 or any(c.isspace() for c in key)):
        raise ValueError(
            "The API key format is invalid. Paste the key without whitespace."
        )
    catalog = await discover(provider, key or ms.provider_api_key(provider))
    match = next((m for m in catalog if m["id"] == model), None)
    if not match:
        raise ValueError(
            "Choose a currently available model from the free-tier catalog."
        )
    if reasoning != "default" and reasoning not in match["reasoning"]:
        raise ValueError(
            "This reasoning level is not supported by the selected model. Use provider default."
        )
    # Network validation completes before any key/selection is changed. Lock only
    # the small read-modify-write transaction, never a network operation.
    with _LOCK:
        selected = ms.read_selection()
        if key:
            keys = ms._read_provider_keys()
            keys[provider] = key
            ms._write_provider_keys(keys)
        selected.setdefault("connections", {})[provider] = {
            "model": model,
            "reasoning": reasoning,
            "reasoning_options": match["reasoning"],
            "free_only": True,
            "consent": True,
            "free_plan_confirmed": free_plan_confirmed,
        }
        _write_selection(selected)
    ms.reset_llm_cache()
    return status()


def save_routing(mode: str, single: str, tasks: dict):
    if mode not in {"single", "tasks"} or set(tasks) - set(TASKS):
        raise ValueError(
            "Choose single-model or task-based routing for the supported tasks."
        )
    with _LOCK:
        selected = ms.read_selection()
        connections = selected.get("connections") or {}
        choices = [single, *tasks.values()]
        for p in choices:
            if p != "ollama" and (p not in connections or not ms.provider_api_key(p)):
                raise ValueError(
                    "Save a model and API key for each selected provider first."
                )
        selected["routing"] = {"mode": mode, "single": single, "tasks": tasks}
        selected["summary_mixture_models"] = []  # No surprise parallel-model fan-out.
        # Embedding space must not change when switching a chat/review route. Pin
        # the pre-routing embedding provider on opt-in (old indexes remain usable).
        selected.setdefault("embedding_provider", ms.active_provider())
        _write_selection(selected)
    ms.reset_llm_cache()
    return status()


def remove(provider: str):
    if provider not in PROVIDERS:
        raise ValueError("Unknown connection.")
    with _LOCK:
        selected = ms.read_selection()
        selected.get("connections", {}).pop(provider, None)
        routing = selected.get("routing") or {}
        if routing.get("single") == provider:
            routing["single"] = "ollama"
        for task, chosen in list(routing.get("tasks", {}).items()):
            if chosen == provider:
                routing["tasks"][task] = "ollama"
        ms.forget_provider_key(provider)
        _write_selection(selected)
    ms.reset_llm_cache()
    return status()
