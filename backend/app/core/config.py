"""
config.py — Single source of truth for all environment variables.

Why Pydantic BaseSettings?
- Reads from .env file automatically
- Validates types at startup (fail fast, not at 3am in production)
- Gives you autocomplete in your IDE

PROVIDER SYSTEM:
  LLM_PROVIDER=deepseek → DeepSeek API primary, Ollama local fallback (recommended)
  LLM_PROVIDER=ollama   → fully local, no API key needed
  LLM_PROVIDER=openai   → GPT-4o, paid

  Switch with a single env var. No code changes required.
"""
import json
import os
from pathlib import Path
from typing import List, Optional, Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache


def _load_json_configs() -> dict:
    """Load configuration from configs.json if present in workspace root or backend dir."""
    possible_paths = [
        Path("configs.json"),
        Path(__file__).resolve().parent.parent.parent / "configs.json",
        Path(__file__).resolve().parent.parent.parent.parent / "configs.json",
    ]
    for p in possible_paths:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


class Settings(BaseSettings):
    # --- LLM Provider ---
    # "deepseek" → DeepSeek hosted API (primary), Ollama local (fallback)
    # "ollama"   → fully local, no API key, no quota
    # "openai"   → GPT-4o, paid
    llm_provider: Literal["openai", "ollama", "deepseek"] = "deepseek"

    # --- OpenAI (used when llm_provider=openai) ---
    openai_api_key: Optional[str] = None
    openai_chat_model: str = "gpt-4o"
    openai_embedding_model: str = "text-embedding-3-small"

    # --- Ollama (self-hosted local models) ---
    # Requires Ollama running at ollama_base_url with the model already pulled.
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen2.5-coder:14b"
    # ollama_review_model: dedicated model for code reviews (Tier 2 — full reasoning).
    # deepseek-r1:14b has strong reasoning for code. Falls back to ollama_chat_model if empty.
    ollama_review_model: str = "deepseek-r1:14b"
    # ollama_fast_model: cheap/fast model for Tier-1 files (config, yaml, small utils).
    # Set to "" to disable Tier-1 routing (all LLM files use the full model).
    # Example: "qwen2.5-coder:7b" — smaller, faster, good enough for low-risk files.
    ollama_fast_model: str = ""
    # summary_mixture_models: list of Ollama model names to use for MoA repo summary.
    # If empty (default), the repo summary uses the single configured provider as before.
    # Example: "qwen2.5-coder:7b,deepseek-r1:1.5b" (comma-separated)
    # Each model gets the same prompt in parallel; an aggregator synthesises their drafts.
    summary_mixture_models: str = ""

    # --- DeepSeek API (OpenAI-compatible, primary provider) ---
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_chat_model: str = "deepseek-chat"

    # --- ChromaDB ---
    chroma_persist_directory: str = "./chroma_data"
    chroma_collection_name: str = "codesage"

    # --- Chunking ---
    chunk_size: int = 700
    chunk_overlap: int = 150

    # --- Retrieval ---
    top_k_results: int = 5
    max_context_chars: int = 14000
    query_expansion_enabled: bool = True
    review_mode: Literal["fast", "agentic"] = "fast"
    # review_max_full_files: max files that get a full LLM review per batch.
    # Remaining files get fast deterministic static analysis.
    # Set to 61 to cover full-repo reviews (CodeSage itself has 61 indexed files).
    # Raise further for larger repos; the only cost is wall-clock time at
    # review_concurrency=3 concurrent LLM calls.
    review_max_full_files: int = 61
    review_concurrency: int = 3

    # --- LangSmith Observability ---
    # LangSmith traces every LangChain call automatically when these vars are set.
    # Without them, nothing changes — tracing is purely opt-in.
    # Set LANGCHAIN_TRACING_V2=true + LANGCHAIN_API_KEY to enable.
    # Free tier at smith.langchain.com — no credit card needed.
    langchain_tracing_v2: Optional[str] = None       # "true" to enable
    langchain_api_key: Optional[str] = None           # from smith.langchain.com
    langchain_project: str = "codesage"               # project name in LangSmith UI

    # --- Authentication ---
    # API key that protects all write/query endpoints.
    # If unset (default), the server runs open — safe for local dev.
    # In production (Railway), set API_KEY to a random secret so only your
    # frontend can call the API.
    # Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
    api_key: Optional[str] = None

    # --- App ---
    # PORT: Railway injects the PORT env var and routes external traffic to it.
    # We read it here so uvicorn can bind to the right port in production.
    # Default 8000 keeps local dev working without any .env change.
    port: int = 8000

    # Accept Union of str or List[str] so pydantic_settings doesn't treat it as complex JSON decode
    cors_origins: str | List[str] = "http://localhost:3000,http://localhost:5173"

    @field_validator("cors_origins", mode="after")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [str(o).rstrip("/") for o in parsed]
                except Exception:
                    pass
            return [origin.strip().rstrip("/") for origin in v.split(",") if origin.strip()]
        if isinstance(v, list):
            return [origin.rstrip("/") if isinstance(origin, str) else origin for origin in v]
        return v

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


# lru_cache means this is only created once — the same Settings object
# is reused across all requests (singleton pattern)
@lru_cache()
def get_settings() -> Settings:
    json_config = _load_json_configs()
    if not json_config:
        return Settings()

    # configs.json provides portable defaults, while environment/.env values
    # must win for deployment-specific choices such as the LLM provider and key.
    # Passing the JSON directly to Settings would make it override .env.
    dotenv_values = {}
    try:
        from dotenv import dotenv_values as read_dotenv
        dotenv_values = {
            key.lower(): value
            for key, value in read_dotenv(".env").items()
            if value is not None
        }
    except Exception:
        pass

    env_overrides = {
        key: value
        for key, value in json_config.items()
        if os.getenv(key.upper()) is None and key.lower() not in dotenv_values
    }
    return Settings(**env_overrides)
