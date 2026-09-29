"""Fixed-host, live model discovery. No chat calls, prompt storage or paid auto-routing."""

from decimal import Decimal, InvalidOperation
import httpx

# Free *account tiers* are not the same thing as zero-price models. Never infer billing
# status from an API key or promise that the server can disable a provider's billing.
PROVIDERS = {
    "groq": dict(
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        key_url="https://console.groq.com/keys",
        limits_url="https://console.groq.com/docs/rate-limits",
        note="Free plan: organization/model request and token quotas. Paid accounts can be billed.",
    ),
    "openrouter": dict(
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        key_url="https://openrouter.ai/settings/keys",
        limits_url="https://openrouter.ai/docs/api-reference/limits",
        note="Only zero-price :free models are offered here. Free requests have daily/minute limits.",
    ),
    "gemini": dict(
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_url="https://aistudio.google.com/apikey",
        limits_url="https://ai.google.dev/gemini-api/docs/rate-limits",
        note="Eligible Flash models only. Use an unbilled free-tier project; availability and quotas vary. Free-tier data may be used to improve products.",
    ),
    "mistral": dict(
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        key_url="https://console.mistral.ai/api-keys",
        limits_url="https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them",
        note="Use Free mode for evaluation; organization request/token/month limits apply. Check data-training preferences before sending private code.",
    ),
}
GEMINI_FREE_MODELS = {
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
}

EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def safe_error(exc: Exception) -> str:
    """Actionable categories, never raw provider bodies (may contain keys/prompts)."""
    if type(exc).__name__ == "EmptyModelOutput":
        return "Model produced no usable output. Lower reasoning effort or reduce context; the output budget may have been exhausted."
    if isinstance(exc, PermissionError):
        return "The configured provider key is missing. Reconnect it or select Ollama."
    code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if code in (401, 403):
        return "Authentication or access denied. Check the saved key and model permissions."
    if code == 402:
        return "Provider credits exhausted. Local fallback only; no paid upgrade was requested."
    if code == 429:
        return "Provider rate limit or quota reached. Wait for the provider reset or use Ollama."
    if code == 404:
        return "Model or endpoint not found. Refresh the catalog or pull the selected Ollama model."
    if code in (400, 422):
        return "Provider rejected the request. Check model capabilities, context size and reasoning settings."
    if (
        isinstance(exc, (TimeoutError, httpx.TimeoutException))
        or "timeout" in type(exc).__name__.lower()
    ):
        return (
            "Model request timed out. Reduce review size or use a smaller local model."
        )
    if isinstance(exc, (AttributeError, NotImplementedError)):
        return "This model adapter does not support the requested operation (such as tool calling)."
    if (
        isinstance(exc, httpx.ConnectError)
        or "connection" in type(exc).__name__.lower()
    ):
        return "Cannot reach the model runtime. Check the provider connection or start Ollama."
    if code and code >= 500:
        return "Provider runtime failed. For Ollama, check available RAM and its server logs; installed does not mean loaded."
    return "Model call failed. Check the model connection, available memory and runtime logs."


def reasoning_options(provider: str, model: str, raw: dict) -> list[str]:
    if provider == "openrouter":
        info = raw.get("reasoning") or {}
        if "supported_efforts" not in info:
            return []  # Do not guess capability from a model's name.
        efforts = info["supported_efforts"]
        values = EFFORTS if efforts is None else efforts
        return [
            e
            for e in EFFORTS
            if e in values and not (e == "none" and info.get("mandatory"))
        ]
    if provider == "groq" and model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
        return ["low", "medium", "high"]
    if provider == "gemini" and model == "gemini-3.1-flash-lite":
        return ["minimal", "low", "medium", "high"]
    if provider == "gemini" and model in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
        return ["none", "low", "medium", "high"]
    return []  # New/unknown models use provider default, not unsupported parameters.


def _zero(value) -> bool:
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def normalize_model(provider: str, raw: dict) -> dict | None:
    model = raw.get("id")
    if not isinstance(model, str) or not model or len(model) > 200:
        return None
    if provider == "openrouter":
        prices = raw.get("pricing") or {}
        outputs = (raw.get("architecture") or {}).get("output_modalities", ["text"])
        if (
            not model.endswith(":free")
            or "text" not in outputs
            or not prices
            or not all(_zero(v) for v in prices.values())
        ):
            return None
    elif provider == "gemini":
        # Conservative, documented free-tier text models. Others may be used via
        # OpenRouter only if that gateway advertises an explicit zero-price endpoint.
        if model not in GEMINI_FREE_MODELS:
            return None
    elif provider == "groq":
        if raw.get("active") is False or any(
            x in model.lower() for x in ("whisper", "orpheus", "guard", "compound")
        ):
            return None
    elif provider == "mistral":
        if not (raw.get("capabilities") or {}).get("completion_chat"):
            return None
    return dict(
        id=model,
        name=raw.get("name") or model,
        reasoning=reasoning_options(provider, model, raw),
        context_length=raw.get("context_length")
        or raw.get("context_window")
        or raw.get("max_context_length"),
        cost=(
            "zero-price endpoint"
            if provider == "openrouter"
            else "account free tier — verify your plan"
        ),
    )


async def discover(provider: str, key: str) -> list[dict]:
    if provider not in PROVIDERS:
        raise ValueError("Choose Groq, OpenRouter, Gemini or Mistral.")
    if not key:
        raise ValueError("Add an API key before loading models.")
    # No arbitrary base URLs, redirects or key-in-query-string requests.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12, connect=4), follow_redirects=False
    ) as client:
        response = await client.get(
            PROVIDERS[provider]["base_url"] + "/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("Provider did not return a model catalog. Try again later.")
    models = [
        m
        for raw in data[:2000]
        if isinstance(raw, dict) and (m := normalize_model(provider, raw))
    ]
    return sorted(models, key=lambda m: m["id"])


def safe_stream_error(token: str) -> str:
    """Only allow our public categories through a review error marker."""
    text = token.removeprefix("__ERROR__").split("__ERROR_END__")[0].strip()
    categories = [
        Exception(),
        TimeoutError(),
        NotImplementedError(),
        httpx.ConnectError(""),
    ]
    for code in (400, 401, 402, 404, 429, 500):
        exc = Exception()
        exc.status_code = code
        categories.append(exc)
    for exc in categories:
        message = safe_error(exc)
        if text == message:
            return message
    return "Model call failed. Use Profile → Model connections → Test to diagnose the configured model."
