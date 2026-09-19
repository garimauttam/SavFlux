"""
snippets.py — P2 Snippet Vault API ($0, local file)

Endpoints:
  GET    /snippets              — list ?limit=&tag=&language=&starred=&q=
  POST   /snippets              — save {code, title?, language?, tags?, source?}
  POST   /snippets/{id}/use     — increment use_count
  POST   /snippets/{id}/star    — toggle star
  DELETE /snippets/{id}         — delete
  DELETE /snippets              — clear all (admin)
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/snippets", tags=["snippets"])


@router.get("")
async def list_snippets(
    limit: int = Query(100, ge=1, le=500),
    tag: str | None = None,
    language: str | None = None,
    starred: bool | None = None,
    q: str | None = None,
    _: None = Depends(require_api_key),
):
    try:
        if q:
            from app.services.snippet_service import search_snippets
            snippets = search_snippets(q, limit=limit)
            # Apply additional filters client-side if needed
            if tag:
                tag_lower = tag.lower()
                snippets = [s for s in snippets if any(t.lower() == tag_lower for t in s.get("tags", []))]
            if language:
                snippets = [s for s in snippets if s.get("language","").lower() == language.lower()]
            if starred is not None:
                snippets = [s for s in snippets if bool(s.get("starred")) == starred]
        else:
            from app.services.snippet_service import list_snippets as _list
            snippets = _list(limit=limit, tag=tag, language=language, starred=starred)
        return {"snippets": snippets, "total": len(snippets)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_snippet(body: dict, _: None = Depends(require_api_key)):
    code = (body.get("code") or body.get("text") or "").strip("\n")
    if not code or not code.strip():
        raise HTTPException(status_code=400, detail="code is required")
    title = body.get("title")
    language = body.get("language") or "text"
    tags = body.get("tags") or []
    source = body.get("source")
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be array")
    try:
        from app.services.snippet_service import create_snippet as _create
        snippet = _create(code=code, title=title, language=language, tags=tags, source=source)
        return snippet
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{snippet_id}/use")
async def use_snippet(snippet_id: str, _: None = Depends(require_api_key)):
    try:
        from app.services.snippet_service import use_snippet as _use
        snippet = _use(snippet_id)
        if not snippet:
            raise HTTPException(status_code=404, detail="Snippet not found")
        return snippet
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{snippet_id}/star")
async def star_snippet(snippet_id: str, _: None = Depends(require_api_key)):
    try:
        from app.services.snippet_service import toggle_star
        snippet = toggle_star(snippet_id)
        if not snippet:
            raise HTTPException(status_code=404, detail="Snippet not found")
        return snippet
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{snippet_id}")
async def delete_snippet(snippet_id: str, _: None = Depends(require_api_key)):
    try:
        from app.services.snippet_service import delete_snippet as _delete
        ok = _delete(snippet_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Snippet not found")
        return {"status": "deleted", "id": snippet_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("")
async def clear_snippets(_: None = Depends(require_api_key)):
    try:
        from app.services.snippet_service import clear_snippets as _clear
        n = _clear()
        return {"status": "cleared", "deleted": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
