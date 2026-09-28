"""
llm_factory.py — Builds LLM and embedding instances based on LLM_PROVIDER.

WHY A FACTORY?
Every service (retrieval, review agent) needs an LLM. Without this, switching
providers means finding every ChatOpenAI() call across multiple files.
With this factory, the entire provider swap is one env var + this one file.

SUPPORTED PROVIDERS:
  openai     → ChatOpenAI + OpenAIEmbeddings (text-embedding-3-small)
               Requires: OPENAI_API_KEY; hosted and metered

  deepseek   → OpenAI-compatible DeepSeek API + local HuggingFaceEmbeddings
               Requires: DEEPSEEK_API_KEY; local fallback if the API call fails

  openrouter → OpenAI-compatible model catalog + local HuggingFaceEmbeddings
               Requires: OPENROUTER_API_KEY; model selected in Settings

  ollama     → local ChatOllama (any model pulled via `ollama pull`)
               Requires: Ollama running at OLLAMA_BASE_URL; no API key or quota

Hosted providers use the selected local Ollama model as a fallback when available.
Provider selection, key lookup and model names are centralized in model_service.py.
"""

from functools import lru_cache
from typing import Any

from app.core.config import get_settings


def _settings():
    """
    Lazy accessor for settings — avoids reading .env at module import time.

    The original `settings = get_settings()` at module level caused an ImportError
    in environments where the .env file is absent (CI build steps, Docker layer cache)
    because pydantic_settings raises a ValidationError before any function is called.

    Calling get_settings() inside functions that need it is the correct pattern:
    lru_cache on get_settings() means it still only reads .env once per process.
    """
    return get_settings()


def get_review_llm(model_name: str = "", streaming: bool = True) -> Any:
    """
    Return an LLM instance routed to a specific model.

    Used by the LLM router in multi_review_agent to send each file to the
    right model tier without disrupting the cached get_chat_llm instances.

    model_name=""   → use the configured provider (same as get_chat_llm)
    model_name="X"  → force Ollama model X (e.g. "qwen2.5-coder:7b")

    Never cached — the router builds one instance per review task; LangChain
    itself is lightweight to construct so this is not a perf concern.
    """
    if not model_name:
        return get_chat_llm(streaming=streaming, review=True)
    # Explicit model name → Ollama instance on that model
    from langchain_community.chat_models import ChatOllama
    return ChatOllama(
        model=model_name,
        base_url=_settings().ollama_base_url,
        temperature=0.1,
        num_predict=4096,
    )


@lru_cache(maxsize=128)
def _get_chat_llm_cached(streaming: bool, review: bool, tenant_key: str) -> Any:
    """
    Return a cached chat LLM for the configured provider.

    streaming=True  → used for token-by-token streaming (retrieval, final review write-up)
    streaming=False → used for tool-calling ReAct phase (needs complete JSON responses)
    review=True     → use OLLAMA_REVIEW_MODEL instead of OLLAMA_CHAT_MODEL when on Ollama,
                       so the coding-specialist model can be different from the chat model.

    Fallback chain (DeepSeek primary):
      DeepSeek API → Ollama local
    Fallback chain (Ollama primary):
      Ollama (no hosted fallback — fully local)
    """
    from app.services.model_service import active_provider, provider_api_key

    provider = active_provider()
    fallback = None
    if provider != "ollama":
        # Hosted providers always have an internal-model fallback. The fallback
        # is explicit in Settings; a missing key routes straight to local instead
        # of constructing a provider client that cannot authenticate.
        try:
            fallback = _build_chat_llm("ollama", streaming, review=review)
        except Exception:
            pass
        if not provider_api_key(provider):
            if fallback is not None:
                return fallback
            raise ValueError(f"No {provider.title()} key is configured and the local fallback could not be initialized.")

    primary = _build_chat_llm(provider, streaming, review=review)
    fallbacks = [fallback] if fallback is not None else []
    return primary.with_fallbacks(fallbacks) if fallbacks else primary


def get_chat_llm(streaming: bool = False, review: bool = False) -> Any:
    """Return a model cached by user, so one account can never receive another's key."""
    from app.core.tenant import current_user_id
    tenant_key = current_user_id() or "__local__"
    return _get_chat_llm_cached(streaming, review, tenant_key)


# Preserve the cache controls used by settings changes and unit tests.
get_chat_llm.cache_clear = _get_chat_llm_cached.cache_clear  # type: ignore[attr-defined]


def _build_chat_llm(provider: str, streaming: bool = False, *, review: bool = False) -> Any:
    """Build one provider instance — no fallback chain attached."""
    s = _settings()
    if provider == "ollama":
        from langchain_community.chat_models import ChatOllama
        # The model comes from `model_service`, not from settings directly: the
        # app's model picker writes a selection file, and this is what honours
        # it. Falling back to the .env value inside that module means every other
        # caller (health, review, the model router) sees the same model the UI
        # shows, instead of the one named at process start.
        from app.services.model_service import active_chat_model, active_review_model

        model = active_review_model() if review else active_chat_model()
        return ChatOllama(
            model=model,
            base_url=s.ollama_base_url,
            temperature=0.1,
            # num_predict caps the response length — avoids runaway generation on
            # smaller local models while still leaving room for a full review.
            num_predict=4096,
        )

    elif provider in {"deepseek", "openai", "openrouter"}:
        from app.services.model_service import active_provider_model, provider_api_key
        api_key = provider_api_key(provider)
        if not api_key:
            raise ValueError(f"A {provider.title()} API key is required. Add one in Settings or use Ollama.")
        from langchain_openai import ChatOpenAI
        if provider == "deepseek":
            model = active_provider_model(provider)
            base_url = s.deepseek_base_url
        elif provider == "openrouter":
            model = active_provider_model(provider)
            base_url = s.openrouter_base_url
        else:
            model = active_provider_model(provider)
            base_url = None
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0.1,
            streaming=streaming,
            max_retries=1,
        )

    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}")


def _resolve_embedding_device(configured: str) -> str:
    """
    Turn the `auto` setting into a concrete device.

    `auto` exists so one shipped default is right on a CPU-only CI runner and on a
    workstation with a GPU. torch is imported lazily and defensively: this function
    runs while *building* the embedding function, and a torch that cannot
    initialise CUDA must degrade to CPU, not take the request down with it.
    """
    if configured != "auto":
        return configured
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # pragma: no cover - depends on the host's torch build
        pass
    return "cpu"


#: Providers whose embedding path is a local model, i.e. free at $0. Everything else
#: embeds through a paid API — and a corpus is 1,800-odd calls, not one, which is why
#: "the benchmark uses whatever the product uses" is not a safe rule. The set lives here,
#: next to the branch that implements it, so that adding a provider cannot silently leave
#: a caller's allowlist behind.
LOCAL_EMBEDDING_PROVIDERS = frozenset({"deepseek", "ollama", "openrouter"})


def build_local_embeddings(model_name: str) -> Any:
    """
    The one construction of a local, free embedding model: device, normalisation, batch.

    Split out of `get_embedding_fn` so a caller that only knows a model NAME — the
    benchmark, which must be able to measure a candidate embedder without being the
    product's configured one — gets exactly what production gets. A second copy of these
    three settings is how a benchmark starts measuring an embedding pipeline that ships
    to nobody: different normalisation and the cosine scores, and the retrieval numbers,
    are not the same quantity.

    The matching rule still governs: the model used at index time must be the model used
    at query time, so naming a model here is a decision about what to index with, not a
    way to run two at once. Uncached on purpose — `get_embedding_fn` keeps its own cache,
    and a name-keyed cache for a benchmark pin would be a second lifecycle to reason
    about for one construction per run.
    """
    s = _settings()
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError:
        from langchain_community.embeddings import HuggingFaceEmbeddings
    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs={"device": _resolve_embedding_device(s.embedding_device)},
        encode_kwargs={
            "normalize_embeddings": True,
            "batch_size": s.embedding_batch_size,
        },
    )


@lru_cache(maxsize=128)
def _get_embedding_fn_cached(tenant_key: str) -> Any:
    """Per-account embedder cache; hosted credentials must never cross tenants."""
    s = _settings()
    from app.services.model_service import active_provider, provider_api_key
    provider = active_provider()
    if provider in LOCAL_EMBEDDING_PROVIDERS:
        return build_local_embeddings(s.embedding_model)
    if provider == "openai":
        api_key = provider_api_key(provider)
        if not api_key:
            raise ValueError("An OpenAI key is required for OpenAI embeddings. Add one in Settings or switch to a local-embedding provider.")
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=s.openai_embedding_model, openai_api_key=api_key)
    raise ValueError(f"Provider {provider!r} has no supported embedding route.")


def get_embedding_fn() -> Any:
    """Return the active account's embedder, never another account's cached client."""
    from app.core.tenant import current_user_id
    return _get_embedding_fn_cached(current_user_id() or "__local__")


get_embedding_fn.cache_clear = _get_embedding_fn_cached.cache_clear  # type: ignore[attr-defined]


def get_provider_name() -> str:
    """
    Human-readable provider name for logging and health checks.

    Reports the *configured* embedding model rather than the name of the model
    that used to be hardcoded. This string is what `/health` shows and what the
    benchmark output is stamped with, and both are useless for diagnosing a
    retrieval problem if they name a model that is not the one running.
    """
    s = _settings()
    from app.services.model_service import (
        active_chat_model,
        active_provider_model,
        read_selection,
    )

    selected_provider = read_selection().get("provider")
    provider = selected_provider if selected_provider in {"ollama", "deepseek", "openai", "openrouter"} else s.llm_provider
    if provider == "ollama":
        return f"Ollama ({active_chat_model()}) + {s.embedding_model} embeddings"
    if provider in {"deepseek", "openai", "openrouter"}:
        return f"{provider.title()} ({active_provider_model(provider)}) → Ollama ({active_chat_model()}) fallback"
    return f"Unknown provider ({provider})"


def get_hosted_display_name() -> str:
    """
    Short vendor label for the hosted (non-Ollama) provider.

    Used by the /health endpoint so the payload reads
    `ok (DeepSeek — deepseek-chat)` rather than leaking a base URL.
    Returns "Local" when the active provider has no hosted component.
    """
    from app.services.model_service import active_provider

    return {"deepseek": "DeepSeek", "openai": "OpenAI", "openrouter": "OpenRouter"}.get(active_provider(), "Local")


def get_hosted_client_kwargs() -> dict[str, Any] | None:
    """
    Connection kwargs for probing the hosted provider with `openai.AsyncOpenAI`.

    Returns None for providers with no hosted endpoint (Ollama), so callers can
    branch without re-implementing the provider matrix. Keeping this here means
    /health never has to know which settings field holds which key.
    """
    s = _settings()
    from app.services.model_service import active_provider, provider_api_key

    provider = active_provider()
    if provider == "deepseek":
        return {"api_key": provider_api_key(provider), "base_url": s.deepseek_base_url}
    if provider == "openai":
        return {"api_key": provider_api_key(provider)}
    if provider == "openrouter":
        return {"api_key": provider_api_key(provider), "base_url": s.openrouter_base_url}
    return None


def get_hosted_model_name() -> str:
    """Model name for the active hosted provider ("" when fully local)."""
    from app.services.model_service import active_provider, active_provider_model

    provider = active_provider()
    return active_provider_model(provider) if provider != "ollama" else ""


def _require_key(name: str, value: Any) -> None:
    """Raise early if a required API key is missing."""
    if not value:
        raise ValueError(
            f"{name} is required when LLM_PROVIDER={_settings().llm_provider}. "
            "Set it in your .env file."
        )
