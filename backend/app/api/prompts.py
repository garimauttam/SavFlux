"""
prompts.py — Prompt Library API ($0, local file).

Endpoints:
  GET    /prompts           — list saved ?limit=&kind=
  POST   /prompts           — save {text, title?, kind?, tags?}
  POST   /prompts/{id}/use  — bump use_count + record history
  DELETE /prompts/{id}      — delete one
  GET    /prompts/history   — recent usage ?limit=
  DELETE /prompts/history   — clear history
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("")
async def list_prompts(
    limit: int = Query(100, ge=1, le=500),
    kind: str | None = None,
    _: None = Depends(require_api_key),
):
    try:
        from app.services.prompt_service import list_prompts as _list
        prompts = _list(limit=limit, kind=kind)
        return {"prompts": prompts, "total": len(prompts)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_prompt(body: dict, _: None = Depends(require_api_key)):
    try:
        from app.services.prompt_service import create_prompt as _create
        return _create(
            text=body.get("text", ""),
            title=body.get("title"),
            kind=body.get("kind", "general"),
            tags=body.get("tags"),
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{prompt_id}/use")
async def use_prompt(prompt_id: str, _: None = Depends(require_api_key)):
    try:
        from app.services.prompt_service import use_prompt as _use
        prompt = _use(prompt_id)
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")
        return prompt
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{prompt_id}")
async def delete_prompt(prompt_id: str, _: None = Depends(require_api_key)):
    try:
        from app.services.prompt_service import delete_prompt as _delete
        if not _delete(prompt_id):
            raise HTTPException(status_code=404, detail="Prompt not found")
        return {"status": "deleted", "id": prompt_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def prompt_history(
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(require_api_key),
):
    try:
        from app.services.prompt_service import list_history
        rows = list_history(limit=limit)
        return {"history": rows, "total": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/history")
async def clear_prompt_history(_: None = Depends(require_api_key)):
    try:
        from app.services.prompt_service import clear_history
        return {"status": "cleared", "deleted": clear_history()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
