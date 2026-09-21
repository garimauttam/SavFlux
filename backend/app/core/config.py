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
    # "ollama"   → fully local via Ollama, no API key, no quota, no network.
    #              THE DEFAULT, because the README promises "$0, no credit card"
    #              and a default that needs a key makes that promise false.
    # "deepseek" → hosted DeepSeek API (cheap, paid), falls back to local Ollama
    # "openai"   → GPT-4o, paid
    #
    # Every provider in this Literal must have a branch in llm_factory's
    # `_build_chat_llm`, `get_provider_name`, `get_hosted_client_kwargs` and
    # `get_embedding_fn`. A provider named in docs but absent here is not a
    # missing feature, it is a startup crash: pydantic rejects the value before
    # any route runs. That is exactly what `.env.example` used to instruct users
    # to do, so test_provider_config.py now asserts the two agree.
    llm_provider: Literal["ollama", "deepseek", "openai"] = "ollama"

    # --- OpenAI (used when llm_provider=openai) ---
    openai_api_key: Optional[str] = None
    openai_chat_model: str = "gpt-4o"
    openai_embedding_model: str = "text-embedding-3-small"

    # --- Ollama (self-hosted local models) ---
    # Requires Ollama running at ollama_base_url with the model already pulled.
    ollama_base_url: str = "http://localhost:11434"
    #
    # SIZES MATTER, because a default nobody can run is not a default. A fresh
    # clone pulls exactly one model, so `ollama_chat_model` is chosen to run on an
    # 8 GB laptop on CPU:
    #   qwen2.5-coder:7b  ~4.7 GB   the default — runs on 8 GB, Apache-2.0
    #   qwen2.5-coder:14b ~9.0 GB   better, needs ~16 GB or a GPU
    #   qwen2.5-coder:32b ~20 GB    best local, needs ~32 GB
    ollama_chat_model: str = "qwen2.5-coder:7b"
    # ollama_review_model: dedicated model for code reviews (Tier 2 — full reasoning).
    #
    # Empty by default ON PURPOSE, and it falls back to `ollama_chat_model`. Setting
    # it to a second model means a fresh install downloads twice before a review can
    # run at all, which is how a "$0 in 5 minutes" quickstart becomes a 9 GB wait.
    # One model that works beats two that might not start.
    #
    # To spend more of your machine on review reasoning — genuinely worth it if you
    # have the RAM — set either of these. Both are open-weight:
    #   OLLAMA_REVIEW_MODEL=deepseek-r1:7b    ~4.7 GB, MIT, reasoning traces
    #   OLLAMA_REVIEW_MODEL=deepseek-r1:14b   ~9.0 GB, MIT, strongest of the two
    ollama_review_model: str = ""
    # ollama_fast_model: cheap/fast model for Tier-1 files (config, yaml, small utils).
    # Set to "" to disable Tier-1 routing (all LLM files use the full model).
    # Example: "qwen2.5-coder:1.5b" — smaller, faster, good enough for low-risk files.
    ollama_fast_model: str = ""
    # summary_mixture_models: list of Ollama model names to use for MoA repo summary.
    # If empty (default), the repo summary uses the single configured provider as before.
    # Example: "qwen2.5-coder:7b,deepseek-r1:1.5b" (comma-separated)
    # Each model gets the same prompt in parallel; an aggregator synthesises their drafts.
    summary_mixture_models: str = ""

    # --- Embeddings (local, free) — used by every provider except openai ---
    # This is the model that decides WHAT the LLM is allowed to read. Retrieval
    # quality is a hard ceiling on answer quality: a 7B code model handed the
    # wrong three chunks still answers wrong. So it is a setting, not a literal.
    #
    #   all-MiniLM-L6-v2               384d   ~80 MB   Apache-2.0   ← default
    #       General-purpose English sentences. Fast, runs anywhere — and has never
    #       seen a code corpus. It also reads only 256 tokens and silently drops
    #       the rest, which the chunker's 3000-char ceiling guarantees will happen.
    #
    #   jinaai/jina-embeddings-v2-base-code   768d   ~640 MB  Apache-2.0
    #       Trained on github-code + 150M code Q&A pairs. 8K context, 30 languages.
    #       Better retrieval on code; costs a re-index because the dims differ.
    #
    # Switching this while a populated chroma_data/ exists mixes 384-dim vectors
    # with 768-dim ones and returns noise. Wipe the collection and re-index.
    # `tests/test_embedding_config.py` pins the dimensions and window of each
    # documented option so the numbers in this comment cannot quietly go stale.
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # sentence-transformers encodes a batch at a time. The old hardcoded 1 left the
    # hardware almost entirely idle. This changes how long indexing takes, not the
    # vectors: each row is encoded independently and padding is attention-masked.
    embedding_batch_size: int = 32
    # "auto" | "cpu" | "cuda" | "mps". "auto" uses CUDA when torch can see a GPU
    # and falls back to CPU otherwise, so the same default works on a laptop CI box
    # and a workstation. Override to "cpu" if a GPU is present but you want the CPU
    # left free for something else.
    embedding_device: str = "auto"

    # --- DeepSeek API (OpenAI-compatible, primary provider) ---
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_chat_model: str = "deepseek-chat"

    # --- ChromaDB ---
    chroma_persist_directory: str = "./chroma_data"
    # Deliberately still "codesage" after the rename to SavFlux. This is a
    # storage key, not branding: every already-indexed repository lives in a
    # collection with this name. Changing it does not migrate anything — it
    # silently points the app at a new, empty collection, so existing users
    # would see their whole index vanish with no error. Override it with
    # CHROMA_COLLECTION_NAME if you want a fresh namespace.
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
    # Set to 80 to cover full-repo reviews (SavFlux itself has ~79 indexable files).
    # Raise further for larger repos; the only cost is wall-clock time at
    # review_concurrency=3 concurrent LLM calls.
    review_max_full_files: int = 80
    review_concurrency: int = 3
    # review_llm_budget: how many *single-file* model reviews one batch may make.
    # This is the knob that trades review depth for wall-clock time, and it is
    # separate from review_max_full_files because the pipeline no longer sends
    # every file to the model: most files are answered by the static analyzer
    # (4.5ms each) and the remainder share batched calls. 12 single reviews plus
    # batching covers a 70-file repo in a fraction of the calls.
    review_llm_budget: int = 12
    # review_cache_enabled: reuse a review when the file's content hash, language,
    # model and prompt version all match a previous run. Off = every review is a
    # fresh model call.
    review_cache_enabled: bool = True

    # --- Risk policy gate ---
    # risk_gate_enabled: require approval (or refuse) when a change is risky
    # enough. The score is deterministic — parsed findings, blast radius, path
    # sensitivity, verification — so this can be reasoned about and argued with,
    # unlike a model's opinion of "risky".
    risk_gate_enabled: bool = True
    # risk_approval_threshold: at or above this score (0–10), a change needs a
    # token bound to that exact change plus a written reason before it is pushed.
    # 6 is reachable by one severe, parsed finding in a security-relevant file
    # with real blast radius — i.e. "someone should look at this" — and is not
    # reachable by ordinary work.
    risk_approval_threshold: int = 6
    # risk_block_threshold: at or above this, SavFlux refuses to push at all and
    # returns the manual `gh` command instead. Approval cannot override this.
    # The default is 10 — the maximum — because the approval gate is the one the
    # roadmap asked for, and an unapprovable band should be reserved for a change
    # that is dangerous on every axis at once. Separately from the score, a patch
    # the verifier proves does not apply is never pushed, at any score: that is an
    # integrity rule, not a judgement about risk.
    risk_block_threshold: int = 10

    # --- LangSmith Observability ---
    # LangSmith traces every LangChain call automatically when these vars are set.
    # Without them, nothing changes — tracing is purely opt-in.
    # Set LANGCHAIN_TRACING_V2=true + LANGCHAIN_API_KEY to enable.
    # Free tier at smith.langchain.com — no credit card needed.
    langchain_tracing_v2: Optional[str] = None       # "true" to enable
    langchain_api_key: Optional[str] = None           # from smith.langchain.com
    langchain_project: str = "savflux"               # project name in LangSmith UI

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

    # --- Embedding settings: reject values that fail late and confusingly ---

    @field_validator("embedding_batch_size", mode="after")
    @classmethod
    def _validate_embedding_batch_size(cls, v: int) -> int:
        """
        A batch size of 0 reaches sentence-transformers as an empty batch and
        surfaces much later as an IndexError from inside torch, pointing at a
        library rather than at the config line that caused it. Fail here instead.
        """
        if v < 1:
            raise ValueError(
                f"EMBEDDING_BATCH_SIZE must be at least 1, got {v}. "
                "A batch size of 0 encodes nothing and fails inside torch."
            )
        return v

    @field_validator("embedding_device", mode="after")
    @classmethod
    def _validate_embedding_device(cls, v: str) -> str:
        """
        Validate against the devices torch actually exposes.

        The cost of not doing this: a typo like "gpu" is accepted by pydantic,
        passed to torch, and fails on the first embedding call — during indexing,
        after the clone, the parse and the chunking have already run.
        """
        allowed = {"auto", "cpu", "cuda", "mps"}
        normalised = v.strip().lower()
        if normalised not in allowed:
            raise ValueError(
                f"EMBEDDING_DEVICE must be one of {sorted(allowed)}, got {v!r}. "
                "Use 'auto' to pick CUDA when available and otherwise fall back."
            )
        return normalised

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
