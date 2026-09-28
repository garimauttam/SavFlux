"""
model_service.py — what model is answering, and what else is on the machine.

WHY THIS EXISTS
---------------
The product's promise is "free local models, no API key". What it actually
offered was a `.env` file: `OLLAMA_CHAT_MODEL=qwen2.5-coder:7b`, edited by hand,
effective only after a restart, and — if you got it wrong — indistinguishable
from a broken product, because the chat returned an empty answer and the header
said nothing.

Three real failures came out of that, and each one is what this module removes:

1. **You cannot tell which model you are on.** `GET /health` says the provider
   is degraded but not what it tried. This returns the model, its size, and
   whether it is actually installed.
2. **You cannot tell what is wrong.** "Ollama is not running" and "Ollama is
   running but has no model pulled" are different instructions, and the old
   surface rendered both as a failed request. The `hint` here is the literal
   command to run.
3. **You cannot change it without a restart and an editor.** `POST
   /models/select` writes the choice to the data directory and clears the LLM
   cache, so the next question uses it.

Discovery talks to Ollama's own `/api/tags`. No model list is hardcoded here on
purpose: a catalogue written into the source goes stale the day someone pulls a
new model, and then the product tells you a model you just installed does not
exist. What *is* hardcoded is the suggested set — recommendations, clearly
labelled as recommendations, used when the machine has nothing pulled yet.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.core.paths import data_file

_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
_SELECTION_FILE = "model_selection.json"
_PROVIDER_KEYS_FILE = "provider_api_keys.json"
_HOSTED_PROVIDERS = {"openai", "deepseek", "openrouter"}


class ModelNotInstalled(ValueError):
    """The requested model is not on this machine. Carries the command that
    would make it available, because "invalid model" is not actionable."""

#: What to pull when the machine has nothing. Recommendations, not an
#: inventory — the real list always comes from `/api/tags`.
SUGGESTED_MODELS = [
    {"name": "qwen2.5-coder:7b", "why": "Best balance for an 8 GB laptop on CPU", "size_gb": 4.7},
    {"name": "qwen2.5-coder:14b", "why": "Noticeably better reviews; needs ~16 GB or a GPU", "size_gb": 9.0},
    {"name": "qwen2.5-coder:32b", "why": "Strongest local code model; needs ~32 GB", "size_gb": 20.0},
    {"name": "llama3.1:8b", "why": "Good general reasoning if you already have it", "size_gb": 4.7},
    {"name": "deepseek-r1:14b", "why": "Reasoning traces — good for review depth", "size_gb": 9.0},
]


# ── Active-model override ────────────────────────────────────────────────────

def _selection_path():
    return data_file(_SELECTION_FILE)


def _provider_keys_path():
    return data_file(_PROVIDER_KEYS_FILE)


def _read_provider_keys() -> dict[str, str]:
    try:
        payload = json.loads(_provider_keys_path().read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items() if value}
    except (OSError, ValueError):
        pass
    return {}


def _write_provider_keys(keys: dict[str, str]) -> None:
    """Write provider secrets with restrictive permissions and atomic replace."""
    path = _provider_keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    raw = json.dumps(keys, indent=2).encode("utf-8")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            path.unlink(missing_ok=True)
            raise
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _environment_api_key(provider: str) -> str:
    settings = get_settings()
    field = {"openai": "openai_api_key", "deepseek": "deepseek_api_key", "openrouter": "openrouter_api_key"}.get(provider)
    return str(getattr(settings, field, "") or "") if field else ""


def active_provider() -> str:
    """Provider chosen in Settings, or the deployment's LLM_PROVIDER."""
    selected = read_selection().get("provider")
    if selected in {"ollama", *_HOSTED_PROVIDERS}:
        return selected
    return get_settings().llm_provider


def provider_api_key(provider: str | None = None) -> str:
    """The authenticated user's stored key takes precedence over environment defaults."""
    provider = provider or active_provider()
    return _read_provider_keys().get(provider) or _environment_api_key(provider)


def active_provider_model(provider: str | None = None) -> str:
    provider = provider or active_provider()
    selected = read_selection()
    if selected.get("provider") == provider and selected.get("provider_model"):
        return str(selected["provider_model"])
    settings = get_settings()
    field = {
        "openai": "openai_chat_model",
        "deepseek": "deepseek_chat_model",
        "openrouter": "openrouter_chat_model",
    }.get(provider)
    if field:
        return str(getattr(settings, field))
    return active_chat_model()


def save_provider_config(provider: str, api_key: str = "", model: str = "") -> dict:
    """Select a provider; a BYOK secret is only written server-side, never returned."""
    provider = (provider or "").strip().lower()
    if provider not in {"ollama", *_HOSTED_PROVIDERS}:
        raise ValueError("Choose Ollama, OpenAI, DeepSeek, or OpenRouter.")
    if provider in _HOSTED_PROVIDERS:
        api_key = (api_key or "").strip()
        if api_key and len(api_key) < 8:
            raise ValueError("That API key looks too short; check it and try again.")
        if len(api_key) > 500:
            raise ValueError("API keys must be 500 characters or fewer.")
        if api_key:
            keys = _read_provider_keys()
            keys[provider] = api_key
            _write_provider_keys(keys)
        if not provider_api_key(provider):
            label = {"openai": "OpenAI", "deepseek": "DeepSeek", "openrouter": "OpenRouter"}[provider]
            article = "an" if provider == "openai" else "a"
            raise ValueError(f"Add {article} {label} API key or configure its environment variable before selecting this provider.")
        if model and not model.strip():
            model = ""
    else:
        model = ""

    selected = read_selection()
    selected["provider"] = provider
    if model.strip():
        selected["provider_model"] = model.strip()[:200]
    else:
        selected.pop("provider_model", None)
    path = _selection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(selected, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)
    return {"provider": provider, "provider_model": active_provider_model(provider)}


def forget_provider_key(provider: str) -> bool:
    """Remove only the app-stored key; environment secrets are left untouched."""
    keys = _read_provider_keys()
    if provider not in keys:
        return False
    del keys[provider]
    if keys:
        _write_provider_keys(keys)
    else:
        _provider_keys_path().unlink(missing_ok=True)
    return True


def read_selection() -> dict:
    """The chat/review model the user picked in the app, if any."""
    try:
        return json.loads(_selection_path().read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def write_selection(
    chat: str = "",
    review: Optional[str] = None,
    summary_mixture_models: Optional[list[str]] = None,
) -> dict:
    current = read_selection()
    if chat:
        current["chat"] = chat.strip()
    if review is not None:
        current["review"] = review.strip()
    if summary_mixture_models is not None:
        current["summary_mixture_models"] = list(dict.fromkeys(
            name.strip() for name in summary_mixture_models if name.strip()
        ))
    try:
        _selection_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
        os.chmod(_selection_path(), 0o600)
    except OSError:
        # A read-only data directory must not make the product unusable: the
        # selection is a convenience, and the .env value still works.
        pass
    return current


def active_summary_mixture_models() -> list[str]:
    """MoA models selected in the UI, falling back to deployment configuration."""
    selected = read_selection().get("summary_mixture_models")
    if isinstance(selected, list):
        return list(dict.fromkeys(
            name.strip() for name in selected if isinstance(name, str) and name.strip()
        ))
    configured = get_settings().summary_mixture_models or ""
    return list(dict.fromkeys(name.strip() for name in configured.split(",") if name.strip()))


def active_chat_model() -> str:
    """The chat model to use right now: app selection, else .env."""
    return read_selection().get("chat") or get_settings().ollama_chat_model


def active_review_model() -> str:
    """The model that answers review requests.

    A review model that is configured but not installed is the worst kind of
    default: chat works, so the product looks fine, and only the review surface
    fails, minutes later, with a connection error that says nothing about which
    model was missing. So an un-installed review model falls back to the chat
    model here, and `models_status` reports the substitution instead of hiding
    it. (The committed `configs.json` in this repo names a 33B review model,
    which is exactly the case — most machines running the free path do not have
    it, and the failure was invisible.)
    """
    selection = read_selection().get("review")
    if selection:
        return selection
    env_review = get_settings().ollama_review_model
    if not env_review:
        return active_chat_model()
    installed = {_shape_model(m)["name"] for m in _sync_tags().get("models", [])}
    if installed and env_review not in installed:
        return active_chat_model()
    return env_review


def reset_llm_cache() -> None:
    """Drop the memoised provider instances after a model change.

    `get_chat_llm` is `lru_cache`d, so without this a selection made in the UI
    would keep serving the model named in `.env` until the process restarted —
    which is the "I changed it and nothing happened" bug, and worse than having
    no switch at all.
    """
    from app.services.llm_factory import get_chat_llm, get_embedding_fn

    get_chat_llm.cache_clear()
    get_embedding_fn.cache_clear()
    try:
        from app.services.retrieval_service import _get_vectorstore
        _get_vectorstore.cache_clear()
    except Exception:
        pass


# ── Ollama discovery ─────────────────────────────────────────────────────────

def _base_url() -> str:
    return (get_settings().ollama_base_url or "http://localhost:11434").rstrip("/")


def _sync_tags() -> dict:
    """Best-effort blocking read of `/api/tags`. Empty result = unreachable."""
    try:
        resp = httpx.get(f"{_base_url()}/api/tags", timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return {"models": data.get("models") or []}
    except Exception:
        return {"models": []}


async def _async_tags() -> dict:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{_base_url()}/api/tags")
            resp.raise_for_status()
            return {"models": (resp.json().get("models") or [])}
    except Exception:
        return {"models": []}


def _shape_model(raw: dict) -> dict:
    details = raw.get("details") or {}
    size = raw.get("size")
    return {
        "name": raw.get("name") or raw.get("model"),
        # Ollama reports a bare name here for a default tag; the tag matters,
        # because selecting `qwen2.5-coder` instead of `qwen2.5-coder:7b` is a
        # different model and the user should see which one they picked.
        "size_bytes": size,
        "size_gb": round(size / 1024**3, 2) if isinstance(size, int) else None,
        "parameter_size": details.get("parameter_size"),
        "family": details.get("family"),
        "quantization": details.get("quantization_level"),
        "modified_at": raw.get("modified_at"),
    }


async def models_status() -> dict:
    """Everything the model picker needs, in one call.

    `available` is false in three distinguishable situations, and each carries
    the sentence that fixes it — that distinction is the whole reason this
    endpoint exists:

      * Ollama is not running          → run `ollama serve`
      * Ollama is running, no models   → run `ollama pull <model>`
      * a model is selected but absent → run `ollama pull <that model>`
    """
    settings = get_settings()
    selected = read_selection()
    provider = active_provider()
    chat = active_chat_model()
    review = active_review_model()
    provider_model = active_provider_model(provider) if provider != "ollama" else chat
    provider_key = provider_api_key(provider) if provider in _HOSTED_PROVIDERS else ""
    mixture_models = active_summary_mixture_models()
    embedding = settings.embedding_model

    probe = await _async_tags()
    reachable = bool(probe.get("models")) or await _ping()
    installed = [_shape_model(m) for m in probe.get("models", [])]
    names = {m["name"] for m in installed}
    missing_mixture_models = [name for name in mixture_models if name not in names]
    stored_keys = _read_provider_keys()
    provider_key_source = "app" if provider in stored_keys else ("env" if provider_key else None)

    hint = None
    kind = None
    if provider == "ollama":
        if not reachable:
            kind = "not_running"
            hint = f"Ollama is not reachable. Start it with `ollama serve`, then `ollama pull {chat}`."
        elif not installed:
            kind = "no_models"
            hint = f"Ollama is running with no models. Pull one with `ollama pull {chat}`."
        elif chat not in names:
            kind = "model_missing"
            hint = f"The selected model '{chat}' is not installed. Run `ollama pull {chat}`."
        elif review != chat and review not in names:
            kind = "review_model_missing"
            hint = f"Review model '{review}' is not installed; reviews are using '{chat}'."
    elif not provider_key:
        kind = "provider_key_missing"
        hint = f"Add a {provider.title()} API key in Settings or configure it in the backend environment."

    provider_labels = {
        "ollama": "Local · Ollama",
        "deepseek": "Hosted · DeepSeek",
        "openai": "Hosted · OpenAI",
        "openrouter": "Hosted · OpenRouter",
    }
    return {
        "provider": provider,
        "provider_label": provider_labels.get(provider, provider),
        "provider_model": provider_model,
        "provider_source": "app" if selected.get("provider") else "env",
        "provider_key_configured": bool(provider_key),
        "provider_key_source": provider_key_source,
        "local_fallback_enabled": provider in _HOSTED_PROVIDERS,
        "local_fallback_available": reachable and chat in names,
        "free": provider == "ollama",
        "base_url": _base_url(),
        "available": (reachable and chat in names) if provider == "ollama" else bool(provider_key),
        "reachable": reachable,
        "kind": kind,
        "hint": hint,
        "chat_model": chat,
        "review_model": review,
        "summary_mixture_models": mixture_models,
        "summary_mixture_active": len(mixture_models) >= 2 and not missing_mixture_models,
        "summary_mixture_missing_models": missing_mixture_models,
        "summary_mixture_source": "app" if "summary_mixture_models" in selected else "env",
        "embedding_model": embedding,
        "embedding_local": provider != "openai",
        "models": installed,
        "suggested": [s for s in SUGGESTED_MODELS if s["name"] not in names],
        "selection_source": "app" if selected.get("chat") else "env",
    }


async def _ping() -> bool:
    """Is Ollama up? `/api/tags` is empty on a server with no models, so the
    endpoint alone cannot tell 'no models' from 'not running'."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{_base_url()}/api/tags")
            return resp.status_code < 500
    except Exception:
        return False


async def select_model(
    chat: str = "",
    review: Optional[str] = None,
    summary_mixture_models: Optional[list[str]] = None,
) -> dict:
    """Switch the active model or MoA summary set without restarting."""
    installed = {_shape_model(m)["name"] for m in (await _async_tags()).get("models", [])}
    if chat and installed and chat not in installed:
        # Rejecting here is the difference between "you picked a model that
        # is not on this machine" and a chat endpoint that returns nothing
        # for the next hour. The sentence is the command to fix it.
        raise ModelNotInstalled(
            f"'{chat}' is not installed. Run `ollama pull {chat}` first, "
            f"or pick one of the installed models."
        )

    normalized_mixture = None
    if summary_mixture_models is not None:
        normalized_mixture = list(dict.fromkeys(
            name.strip() for name in summary_mixture_models if name.strip()
        ))
        if normalized_mixture and len(normalized_mixture) < 2:
            raise ModelNotInstalled("Choose at least two installed models for MoA, or clear the selection to disable it.")
        if len(normalized_mixture) > 8:
            raise ModelNotInstalled("MoA is limited to eight models to keep latency and resource use bounded.")
        if normalized_mixture:
            if not installed:
                raise ModelNotInstalled("Ollama is not reachable or has no installed models. Start Ollama and pull the models first.")
            missing = [name for name in normalized_mixture if name not in installed]
            if missing:
                raise ModelNotInstalled(
                    "These MoA models are not installed: " + ", ".join(missing)
                    + ". Run `ollama pull <model>` or choose installed models."
                )

    write_selection(
        chat=chat,
        review=review,
        summary_mixture_models=normalized_mixture,
    )
    reset_llm_cache()
    return await models_status()


__all__ = [
    "models_status", "select_model", "active_chat_model", "active_review_model",
    "active_summary_mixture_models", "active_provider", "active_provider_model",
    "provider_api_key", "save_provider_config", "forget_provider_key",
    "read_selection", "write_selection", "reset_llm_cache", "SUGGESTED_MODELS",
    "ModelNotInstalled",
]
