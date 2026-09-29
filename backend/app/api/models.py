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


# The free-tier connection flow is separate from the legacy paid-provider endpoint.
# Keys are explicit writes, never included in status/catalog/error responses.
from typing import Literal
from fastapi import Request
from app.limiter import limiter
from app.services import provider_connections as connections
from app.services.provider_catalog import discover, safe_error


class CatalogRequest(BaseModel):
    provider: Literal['groq', 'openrouter', 'gemini', 'mistral']
    api_key: SecretStr | None = None


class ConnectionRequest(CatalogRequest):
    model: str = Field(min_length=1, max_length=200)
    reasoning: str = Field('default', max_length=20)
    consent: bool = False
    free_plan_confirmed: bool = False


class RoutingRequest(BaseModel):
    mode: Literal['single', 'tasks'] = 'single'
    single: str = Field('ollama', max_length=30)
    tasks: dict[Literal['chat', 'coding', 'review', 'reasoning'], str] = Field(default_factory=dict)


@router.get('/connections', dependencies=[Depends(require_api_key)])
def connection_status():
    return connections.status()


@router.post('/catalog', dependencies=[Depends(require_api_key)])
@limiter.limit('20/minute')
async def provider_catalog(request: Request, payload: CatalogRequest):
    key = payload.api_key.get_secret_value() if payload.api_key else model_service.provider_api_key(payload.provider)
    if len(key) > 500 or any(c.isspace() for c in key):
        raise HTTPException(400, 'Invalid API key format.')
    try:
        return {'models': await discover(payload.provider, key)}
    except ValueError:
        raise HTTPException(400, 'Could not read this catalog. Check the key and provider availability.')
    except Exception as exc:
        raise HTTPException(502, safe_error(exc))


@router.put('/connections', dependencies=[Depends(require_api_key)])
@limiter.limit('10/minute')
async def save_connection(request: Request, payload: ConnectionRequest):
    try:
        return await connections.save(payload.provider, payload.api_key.get_secret_value() if payload.api_key else '',
                                      payload.model, payload.reasoning, payload.consent, payload.free_plan_confirmed)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, safe_error(exc))


@router.put('/routing', dependencies=[Depends(require_api_key)])
def update_routing(payload: RoutingRequest):
    try:
        return connections.save_routing(payload.mode, payload.single, payload.tasks)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete('/connections/{provider}', dependencies=[Depends(require_api_key)])
def remove_connection(provider: str):
    try:
        return connections.remove(provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class ModelTestRequest(BaseModel):
    provider: Literal['ollama', 'groq', 'openrouter', 'gemini', 'mistral']


@router.post('/test', dependencies=[Depends(require_api_key)])
@limiter.limit('6/minute')
async def test_model(request: Request, payload: ModelTestRequest):
    """Explicit tiny generation test; never fall back or send repository content."""
    import httpx
    from app.services.provider_catalog import PROVIDERS
    provider = payload.provider
    selected = model_service.read_selection().get('connections', {}).get(provider)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=4), follow_redirects=False) as client:
            if provider == 'ollama':
                model = model_service.active_review_model()
                res = await client.post(model_service._base_url() + '/api/generate', json={
                    'model': model, 'prompt': 'Reply with OK.', 'stream': False, 'options': {'num_predict': 32}})
            else:
                if not selected or not model_service.provider_api_key(provider):
                    raise HTTPException(400, 'Save a connection first.')
                model = selected['model']
                body = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}], 'max_tokens': 128}
                if provider == 'openrouter':
                    body['provider'] = {'max_price': {'prompt': 0, 'completion': 0}, 'allow_fallbacks': False}
                res = await client.post(PROVIDERS[provider]['base_url'] + '/chat/completions', json=body,
                    headers={'Authorization': 'Bearer ' + model_service.provider_api_key(provider)})
            res.raise_for_status()
            data = res.json()
            if data.get('error'):
                raise HTTPException(502, 'The runtime returned a generation error. Check its logs and available memory.')
            text = data.get('response') if provider == 'ollama' else (data.get('choices') or [{}])[0].get('message', {}).get('content')
            return {'ok': bool(text), 'message': f'{provider} / {model}: ' + ('generation succeeded. This does not guarantee a large review will fit.' if text else 'responded, but produced no visible text within the small test budget. Reasoning may have consumed the budget.')}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, safe_error(exc))
