"""
llm_factory.py — Builds LLM and embedding instances based on LLM_PROVIDER.

WHY A FACTORY?
Every service (retrieval, review agent) needs an LLM. Without this, switching
providers means finding every ChatOpenAI() call across multiple files.
With this factory, the entire provider swap is one env var + this one file.

SUPPORTED PROVIDERS:
  openai   → ChatOpenAI (GPT-4o) + OpenAIEmbeddings (text-embedding-3-small)
             Requires: OPENAI_API_KEY

  deepseek → OpenAI-compatible DeepSeek API + local HuggingFaceEmbeddings
             Requires: DEEPSEEK_API_KEY; embeddings remain local and free
             Fallback: Ollama (local) if the API call fails

  ollama   → local ChatOllama (any model pulled via `ollama pull`)
             Requires: Ollama running at OLLAMA_BASE_URL; no API key, no quota
             Embeddings: all-MiniLM-L6-v2 runs locally

ADDING A NEW PROVIDER:
  1. Add it to the Literal type in config.py
  2. Add an elif branch in _build_chat_llm()
  3. No other files need to change
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


@lru_cache(maxsize=4)  # keys: (streaming=True/False) × (review=True/False)
def get_chat_llm(streaming: bool = False, review: bool = False) -> Any:
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
    s = _settings()
    primary = _build_chat_llm(s.llm_provider, streaming, review=review)
    fallbacks: list[Any] = []

    if s.llm_provider == "deepseek":
        # If the hosted DeepSeek API fails (402, 429, network), fall back to
        # a local Ollama model so reviews still complete offline.
        try:
            fallbacks.append(_build_chat_llm("ollama", streaming, review=review))
        except Exception:
            pass

    return primary.with_fallbacks(fallbacks) if fallbacks else primary


def _build_chat_llm(provider: str, streaming: bool = False, *, review: bool = False) -> Any:
    """Build one provider instance — no fallback chain attached."""
    s = _settings()
    if provider == "ollama":
        from langchain_community.chat_models import ChatOllama
        model = (
            (s.ollama_review_model or s.ollama_chat_model)
            if review
            else s.ollama_chat_model
        )
        return ChatOllama(
            model=model,
            base_url=s.ollama_base_url,
            temperature=0.1,
            # num_predict caps the response length — avoids runaway generation on
            # smaller local models while still leaving room for a full review.
            num_predict=4096,
        )

    elif provider == "deepseek":
        _require_key("DEEPSEEK_API_KEY", s.deepseek_api_key)
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=s.deepseek_chat_model,
            api_key=s.deepseek_api_key,
            base_url=s.deepseek_base_url,
            temperature=0.1,
            streaming=streaming,
            # max_retries=1: one retry is enough for transient 5xx; 402/429 won't
            # recover on retry so we let the fallback chain handle them quickly.
            max_retries=1,
        )

    elif provider == "openai":
        _require_key("OPENAI_API_KEY", s.openai_api_key)
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=s.openai_chat_model,
            openai_api_key=s.openai_api_key,
            temperature=0.1,
            streaming=streaming,
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
LOCAL_EMBEDDING_PROVIDERS = frozenset({"deepseek", "ollama"})


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


@lru_cache(maxsize=1)
def get_embedding_fn() -> Any:
    """
    Return a cached embedding function.

    IMPORTANT: the model used at index time must match the model used at query time.
    Mixing embedding models produces garbage retrieval. Re-index after switching.

    deepseek / ollama providers use a local HuggingFace model — see
    `embedding_model`, `embedding_batch_size` and `embedding_device` in config.
    openai uses text-embedding-3-small (paid, 1536 dims).
    """
    s = _settings()
    if s.llm_provider in LOCAL_EMBEDDING_PROVIDERS:
        return build_local_embeddings(s.embedding_model)
    _require_key("OPENAI_API_KEY", s.openai_api_key)
    from langchain_openai import OpenAIEmbeddings
    return OpenAIEmbeddings(
        model=s.openai_embedding_model,
        openai_api_key=s.openai_api_key,
    )


def get_provider_name() -> str:
    """
    Human-readable provider name for logging and health checks.

    Reports the *configured* embedding model rather than the name of the model
    that used to be hardcoded. This string is what `/health` shows and what the
    benchmark output is stamped with, and both are useless for diagnosing a
    retrieval problem if they name a model that is not the one running.
    """
    s = _settings()
    if s.llm_provider == "ollama":
        return f"Ollama ({s.ollama_chat_model}) + {s.embedding_model} embeddings"
    if s.llm_provider == "deepseek":
        review_model = s.ollama_review_model or s.ollama_chat_model
        return (
            f"DeepSeek ({s.deepseek_chat_model}) → Ollama ({review_model}) fallback "
            f"+ {s.embedding_model}"
        )
    return f"OpenAI ({s.openai_chat_model}) + {s.openai_embedding_model}"


def get_hosted_display_name() -> str:
    """
    Short vendor label for the hosted (non-Ollama) provider.

    Used by the /health endpoint so the payload reads
    `ok (DeepSeek — deepseek-chat)` rather than leaking a base URL.
    Returns "Local" when the active provider has no hosted component.
    """
    s = _settings()
    if s.llm_provider == "deepseek":
        return "DeepSeek"
    if s.llm_provider == "openai":
        return "OpenAI"
    return "Local"


def get_hosted_client_kwargs() -> dict[str, Any] | None:
    """
    Connection kwargs for probing the hosted provider with `openai.AsyncOpenAI`.

    Returns None for providers with no hosted endpoint (Ollama), so callers can
    branch without re-implementing the provider matrix. Keeping this here means
    /health never has to know which settings field holds which key.
    """
    s = _settings()
    if s.llm_provider == "deepseek":
        return {"api_key": s.deepseek_api_key, "base_url": s.deepseek_base_url}
    if s.llm_provider == "openai":
        return {"api_key": s.openai_api_key}
    return None


def get_hosted_model_name() -> str:
    """Model name for the active hosted provider ("" when fully local)."""
    s = _settings()
    if s.llm_provider == "deepseek":
        return s.deepseek_chat_model
    if s.llm_provider == "openai":
        return s.openai_chat_model
    return ""


def _require_key(name: str, value: Any) -> None:
    """Raise early if a required API key is missing."""
    if not value:
        raise ValueError(
            f"{name} is required when LLM_PROVIDER={_settings().llm_provider}. "
            "Set it in your .env file."
        )
