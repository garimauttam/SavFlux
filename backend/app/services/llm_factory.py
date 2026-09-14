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


@lru_cache(maxsize=1)
def get_embedding_fn() -> Any:
    """
    Return a cached embedding function.

    IMPORTANT: the model used at index time must match the model used at query time.
    Mixing embedding models produces garbage retrieval. Re-index after switching.

    deepseek / ollama providers use local HuggingFace all-MiniLM-L6-v2 (free, CPU).
    openai uses text-embedding-3-small (paid, 1536 dims).
    """
    s = _settings()
    if s.llm_provider in ("deepseek", "ollama"):
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        return HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True, "batch_size": 1},
        )
    else:
        _require_key("OPENAI_API_KEY", s.openai_api_key)
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(
            model=s.openai_embedding_model,
            openai_api_key=s.openai_api_key,
        )


def get_provider_name() -> str:
    """Human-readable provider name for logging and health checks."""
    s = _settings()
    if s.llm_provider == "ollama":
        return f"Ollama ({s.ollama_chat_model}) + local MiniLM embeddings"
    if s.llm_provider == "deepseek":
        review_model = s.ollama_review_model or s.ollama_chat_model
        return f"DeepSeek ({s.deepseek_chat_model}) → Ollama ({review_model}) fallback + local MiniLM"
    return f"OpenAI ({s.openai_chat_model}) + {s.openai_embedding_model}"


def _require_key(name: str, value: Any) -> None:
    """Raise early if a required API key is missing."""
    if not value:
        raise ValueError(
            f"{name} is required when LLM_PROVIDER={settings.llm_provider}. "
            "Set it in your .env file."
        )
