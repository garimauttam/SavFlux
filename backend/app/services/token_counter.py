"""
token_counter.py — Thread-safe token usage counter + LangChain callback handler.

WHY A MODULE-LEVEL COUNTER?
Every request runs in an async context on a shared thread pool. A process-global
counter (guarded by a lock) accumulates totals across all requests without
needing a database. Counters reset when the server restarts — that's fine for a
dev dashboard; it shows "session" usage, not historical.

HOW IT HOOKS INTO LANGCHAIN:
LangChain's BaseCallbackHandler fires on_llm_end() after every LLM call with
a LLMResult object that includes token usage in response.llm_output["token_usage"].
We attach an instance of TokenUsageCallback to each LLM call via
  llm.with_config(callbacks=[get_token_callback()])
This is non-invasive — no changes to the main LLM logic, just a side-channel observer.

TOKEN KEY NAMES ACROSS PROVIDERS:
Every provider LangChain wraps reports usage under different keys —
OpenAI-style {"prompt_tokens", "completion_tokens", "total_tokens"}, and
others with *_count suffixes. We read whichever keys are present and fall back
to 0 for the missing ones, so adding a provider cannot make this raise.
"""

import threading
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


@dataclass
class _UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    chat_requests: int = 0
    review_requests: int = 0
    write_requests: int = 0


_lock = threading.Lock()
_totals_by_user: dict[str, _UsageTotals] = {}


def _tenant_key(user_id: str | None = None) -> str:
    if user_id is not None:
        return user_id
    from app.core.tenant import current_user_id
    return current_user_id() or "__local__"


def _totals_for(user_id: str | None = None) -> _UsageTotals:
    return _totals_by_user.setdefault(_tenant_key(user_id), _UsageTotals())


def get_totals() -> dict:
    """Return a snapshot of the current account's usage (safe across threads)."""
    with _lock:
        t = _totals_for()
        # Cost estimate: local Ollama calls are free (0 API cost).
        # When using OpenAI (GPT-4o-mini) these are approximate charges.
        # GPT-4o-mini pricing: $0.15/1M prompt, $0.60/1M completion (as of 2024).
        estimated_usd = (
            t.prompt_tokens * 0.00000015
            + t.completion_tokens * 0.00000060
        )
        return {
            "prompt_tokens": t.prompt_tokens,
            "completion_tokens": t.completion_tokens,
            "total_tokens": t.total_tokens,
            "llm_calls": t.llm_calls,
            "chat_requests": t.chat_requests,
            "review_requests": t.review_requests,
            "write_requests": t.write_requests,
            "estimated_cost_usd": round(estimated_usd, 6),
        }


def increment_request(kind: str) -> None:
    """Increment the per-kind request counter. kind = 'chat' | 'review' | 'write'."""
    with _lock:
        totals = _totals_for()
        if kind == "chat":
            totals.chat_requests += 1
        elif kind == "review":
            totals.review_requests += 1
        elif kind == "write":
            totals.write_requests += 1


def reset_totals() -> None:
    """Reset only the current account's counters."""
    with _lock:
        _totals_by_user[_tenant_key()] = _UsageTotals()


class TokenUsageCallback(BaseCallbackHandler):
    """Per-call callback with the account identity captured before model execution."""

    def __init__(self) -> None:
        super().__init__()
        from app.core.tenant import current_user_id
        self.user_id = current_user_id()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        usage = (response.llm_output or {}).get("token_usage") or {}
        if not usage:
            # Some providers put usage under a different key; try usage_metadata
            for gen_list in response.generations:
                for gen in gen_list:
                    meta = getattr(gen, "generation_info", {}) or {}
                    if meta.get("usage_metadata"):
                        usage = meta["usage_metadata"]
                        break
                if usage:
                    break

        # Support both OpenAI and Gemini field names
        prompt = (
            usage.get("prompt_tokens")
            or usage.get("prompt_token_count")
            or usage.get("input_tokens")
            or 0
        )
        completion = (
            usage.get("completion_tokens")
            or usage.get("candidates_token_count")
            or usage.get("output_tokens")
            or 0
        )
        total = usage.get("total_tokens") or usage.get("total_token_count") or (prompt + completion)

        with _lock:
            totals = _totals_for(self.user_id)
            totals.prompt_tokens += prompt
            totals.completion_tokens += completion
            totals.total_tokens += total
            totals.llm_calls += 1


def get_token_callback() -> TokenUsageCallback:
    """Returns a fresh TokenUsageCallback instance for use in a single LLM call."""
    return TokenUsageCallback()
