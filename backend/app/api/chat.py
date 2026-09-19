"""
chat.py — API route for the Q&A chat endpoint.

This is the endpoint the React frontend calls every time the user sends a message.
It returns a streaming response so the UI can render tokens in real time.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.limiter import limiter
from app.services.retrieval_service import stream_answer, get_indexed_files

router = APIRouter(prefix="/chat", tags=["chat"])


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str
    chat_history: list[Message] = []
    # active_repo_url (singular): legacy single-repo filter.
    # active_repo_urls (plural): multi-repo cross-search — retrieval spans selected repos.
    # The plural field takes precedence when both are provided.
    active_repo_url: str | None = None
    active_repo_urls: list[str] | None = None  # multi-repo cross-search

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Question cannot be empty.")
        if len(v) > 2000:
            raise ValueError("Question is too long. Maximum 2000 characters.")
        return v

    @field_validator("active_repo_urls")
    @classmethod
    def validate_repo_urls(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) > 20:
            raise ValueError("Too many repos selected. Maximum 20.")
        return v


@router.post("/stream")
@limiter.limit("20/minute")   # 20 req/min/IP — humans type ~2/min at most
async def chat_stream(request: Request, body: ChatRequest, _: None = Depends(require_api_key)):
    """
    Streams the answer to a question using RAG.

    The frontend calls this with:
      { question: "What does auth.py do?", chat_history: [...] }

    And gets back a chunked HTTP response where each chunk is a piece of the answer.
    The first chunk is a special __SOURCES__ marker with citation metadata.
    All subsequent chunks are raw answer text tokens.

    WHY NOT USE WEBSOCKETS?
    For a simple request-response pattern (send question, receive stream),
    HTTP streaming is simpler. WebSockets would add complexity for no benefit here.
    """
    history = [msg.model_dump() for msg in body.chat_history]

    return StreamingResponse(
        stream_answer(
            body.question,
            history,
            body.active_repo_url,
            body.active_repo_urls,
        ),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/share")
@limiter.limit("30/minute")
async def share_chat(request: Request, body: dict, _: None = Depends(require_api_key)):
    """
    Snapshot a Q&A pair as a shareable /s/{id} link.

    MessageBubble POSTs {question, answer, sources, repo_url, ledger,
    chat_history}. Delegates to share_service — same store as /share.
    """
    try:
        from app.services.share_service import create_share
        return create_share(
            question=body.get("question", ""),
            answer=body.get("answer", ""),
            sources=body.get("sources"),
            repo_url=body.get("repo_url"),
            ledger=body.get("ledger"),
            chat_history=body.get("chat_history"),
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/indexed-files")
async def get_files(_: None = Depends(require_api_key)):
    """
    Returns all files currently in the vector store.
    Used by the frontend's Repo Map panel.
    """
    files = await get_indexed_files()
    return {"files": files, "total": len(files)}
