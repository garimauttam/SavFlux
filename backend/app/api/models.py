"""
models.py — "what is answering right now, and what else could".

The counterpart to the $0 promise. Every number the picker shows is read from
the machine rather than asserted by the product: the model list comes from
Ollama's `/api/tags`, the sizes from Ollama's own metadata, and the selected
model is whatever `llm_factory` will actually construct next.

Endpoints:
  GET  /models        — providers, installed local models, the selected one
  POST /models/select — switch the active model, no restart
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr

from app.api.deps import require_api_key
from app.services import model_service

router = APIRouter(prefix="/models", tags=["models"])


class SelectRequest(BaseModel):
    chat: str = Field("", max_length=200)
    review: str | None = Field(default=None, max_length=200)
    summary_mixture_models: list[str] | None = Field(default=None, max_length=8)


class ProviderRequest(BaseModel):
    provider: str = Field(..., pattern="^(ollama|openai|deepseek|openrouter)$")
    api_key: SecretStr | None = Field(default=None, max_length=500)
    model: str = Field("", max_length=200)


@router.get("")
async def list_models(_: None = Depends(require_api_key)):
    return await model_service.models_status()


@router.post("/select")
async def select(payload: SelectRequest, _: None = Depends(require_api_key)):
    try:
        return await model_service.select_model(
            chat=payload.chat,
            review=payload.review,
            summary_mixture_models=payload.summary_mixture_models,
        )
    except model_service.ModelNotInstalled as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/provider")
async def select_provider(payload: ProviderRequest, _: None = Depends(require_api_key)):
    try:
        model_service.save_provider_config(
            provider=payload.provider,
            api_key=payload.api_key.get_secret_value() if payload.api_key else "",
            model=payload.model,
        )
        model_service.reset_llm_cache()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await model_service.models_status()


@router.delete("/provider/key/{provider}")
async def delete_provider_key(provider: str, _: None = Depends(require_api_key)):
    if provider not in {"openai", "deepseek", "openrouter"}:
        raise HTTPException(status_code=422, detail="Choose a supported hosted provider.")
    removed = model_service.forget_provider_key(provider)
    model_service.reset_llm_cache()
    return {"removed": removed, "provider": provider}
