"""
slash.py — P2 Slash Commands API ($0, local)

Endpoints:
  GET  /slash/commands          — list all commands ?q=&limit=
  POST /slash/expand            — expand {command, args} -> {prompt}
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/slash", tags=["slash"])


@router.get("/commands")
async def list_commands(
    q: str | None = Query(None, max_length=100, description="Filter query"),
    limit: int = Query(20, ge=1, le=50),
    _: None = Depends(require_api_key),
):
    try:
        if q:
            from app.services.slash_service import search_commands
            import asyncio
            cmds = await asyncio.to_thread(search_commands, q, limit)
        else:
            from app.services.slash_service import list_commands as _list
            import asyncio
            cmds = await asyncio.to_thread(_list)
            cmds = cmds[:limit]
        return {"commands": cmds, "total": len(cmds)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/expand")
async def expand_command(body: dict, _: None = Depends(require_api_key)):
    command = (body.get("command") or body.get("cmd") or "").strip()
    args = (body.get("args") or body.get("text") or body.get("query") or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required (e.g. /explain)")
    try:
        from app.services.slash_service import expand_command as _expand
        import asyncio
        result = await asyncio.to_thread(_expand, command, args)
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
