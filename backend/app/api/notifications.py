"""
notifications.py — P2 Notifications Center API ($0, local file)

Endpoints:
  GET    /notifications              — list ?limit=&kind=&unread_only=
  GET    /notifications/unread-count — {unread}
  POST   /notifications              — create {title, message?, kind?, level?, meta?}
  PATCH  /notifications/{id}/read    — mark read {read: true/false}
  POST   /notifications/read-all     — mark all read
  DELETE /notifications/{id}         — delete one
  DELETE /notifications              — clear ?kind=
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    limit: int = Query(100, ge=1, le=500),
    kind: str | None = None,
    unread_only: bool = Query(False),
    _: None = Depends(require_api_key),
):
    try:
        from app.services.notification_service import list_notifications as _list
        notifs = _list(limit=limit, kind=kind, unread_only=unread_only)
        return {"notifications": notifs, "total": len(notifs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/unread-count")
async def unread_count(_: None = Depends(require_api_key)):
    try:
        from app.services.notification_service import get_unread_count
        return {"unread": get_unread_count()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_notification(body: dict, _: None = Depends(require_api_key)):
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    message = body.get("message", "")
    kind = body.get("kind", "general")
    level = body.get("level", "info")
    meta = body.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="meta must be object")
    try:
        from app.services.notification_service import create_notification as _create
        notif = _create(title=title, message=message, kind=kind, level=level, meta=meta)
        return notif
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/read-all")
async def read_all(_: None = Depends(require_api_key)):
    try:
        from app.services.notification_service import mark_all_read
        n = mark_all_read()
        return {"status": "ok", "marked": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{notif_id}/read")
async def mark_read(notif_id: str, body: dict, _: None = Depends(require_api_key)):
    read = body.get("read", True)
    if not isinstance(read, bool):
        raise HTTPException(status_code=400, detail="read must be boolean")
    try:
        from app.services.notification_service import mark_read as _mark
        notif = _mark(notif_id, read=read)
        if not notif:
            raise HTTPException(status_code=404, detail="Notification not found")
        return notif
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{notif_id}")
async def delete_notification(notif_id: str, _: None = Depends(require_api_key)):
    try:
        from app.services.notification_service import delete_notification as _delete
        ok = _delete(notif_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"status": "deleted", "id": notif_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("")
async def clear_notifications(kind: str | None = Query(None), _: None = Depends(require_api_key)):
    try:
        from app.services.notification_service import clear_notifications as _clear
        n = _clear(kind=kind)
        return {"status": "cleared", "deleted": n, "kind": kind or "all"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
