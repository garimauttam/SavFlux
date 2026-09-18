"""
write.py — API route for the code writing agent.

POST /write/generate      — Generate / edit / test code, optionally grounded in
                            indexed repo files as style context.
GET  /write/file-content  — Return raw file content reconstructed from ChromaDB
                            chunks. Used by the frontend diff view to obtain the
                            original file before showing a before/after comparison.

Three modes (body.mode):
  generate — write a new file from a natural-language description
  edit     — rewrite an existing indexed file applying requested changes
  tests    — generate a test file for an existing indexed file
"""

import tempfile
from typing import Literal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.limiter import limiter
from app.services.write_agent import stream_code_write

router = APIRouter(prefix="/write", tags=["write"])


def _get_allowed_roots() -> list[Path]:
    """Same allowed-roots logic as review.py — prevents path traversal on context files.
    Includes /tmp resolved form for macOS where mkdtemp() uses /private/tmp."""
    from app.core.config import get_settings
    s = get_settings()
    return [
        Path(s.chroma_persist_directory).resolve(),
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),   # macOS: /tmp → /private/tmp (mkdtemp uses this)
    ]


class WriteGenerateRequest(BaseModel):
    prompt: str                                          # Description / change instructions
    language: str = "python"                             # Target language
    file_name: str = "output.py"                         # Output filename
    context_sources: list[str] = []                      # Context / target file sources
    mode: Literal["generate", "edit", "tests"] = "generate"

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        v = v.strip()
        # Allow empty prompt in tests mode (will use default instructions)
        if len(v) > 4000:
            raise ValueError("Prompt is too long (max 4000 characters).")
        return v

    @field_validator("context_sources", mode="before")
    @classmethod
    def validate_context_sources(cls, sources: list) -> list[str]:
        """
        Validate each context source path against allowed roots.
        Prevents path traversal — same pattern as ReviewFileRequest.
        Returns original (unresolved) paths so they match ChromaDB metadata.
        """
        if not sources:
            return []
        if len(sources) > 10:
            raise ValueError("Maximum 10 context files allowed per request.")

        allowed_roots = _get_allowed_roots()
        validated = []
        for v in sources:
            # Same stable-ID validation as ReviewFileRequest — both conditions checked
            # independently to prevent "https://evil.com::../../etc/passwd" bypass.
            if "::" in v:
                prefix = v[: v.index("::")]
                if prefix.startswith("https://") or prefix.startswith("git@"):
                    validated.append(v)
                    continue
                # Malformed "::" value — fall through to path check
            resolved = Path(v).resolve()
            if not any(resolved == root or root in resolved.parents for root in allowed_roots):
                raise ValueError(
                    f"Context file path '{v}' is outside the allowed directory."
                )
            validated.append(v)  # return original, not resolved
        return validated


@router.post("/generate")
@limiter.limit("10/minute")
async def generate_code(request: Request, body: WriteGenerateRequest, _: None = Depends(require_api_key)):
    """
    Generate, edit, or test code using the indexed codebase as context.
    Returns a streaming response of status markers + code tokens.
    """
    return StreamingResponse(
        stream_code_write(
            prompt=body.prompt,
            language=body.language,
            file_name=body.file_name,
            context_sources=body.context_sources,
            mode=body.mode,
        ),
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/file-content")
async def get_file_content(source: str = Query(..., description="Absolute source path from indexed files list"), _: None = Depends(require_api_key)):
    """
    Return the raw content of an indexed file, reconstructed from its ChromaDB chunks.

    Used exclusively by the frontend diff view — after an Edit-mode generation completes,
    the UI fetches the original file content here so it can compute a before/after diff
    client-side without any extra LLM call.

    WHY NOT READ FROM DISK?
    The temp clone directory is deleted after ingestion. ChromaDB is the only reliable
    source of truth for file content after indexing. This uses the same chunk-reconstruction
    pattern already used in review.py and write_agent.py.

    Security: same path-traversal guard as all other file endpoints.
    """
    # Validate path against allowed roots (path traversal guard)
    resolved = Path(source).resolve()
    allowed_roots = _get_allowed_roots()
    is_indexed_source_id = "::" in source and (source.startswith("https://") or source.startswith("git@"))
    if not is_indexed_source_id and not any(
        resolved == root or root in resolved.parents for root in allowed_roots
    ):
        raise HTTPException(
            status_code=403,
            detail="File path is outside the allowed directory.",
        )

    # Reconstruct file content from ChromaDB chunks
    try:
        from app.services.ingestion_service import _get_vectorstore
        vs = _get_vectorstore()
        results = vs._collection.get(
            where={"source": source},
            include=["documents", "metadatas"],
        )
        docs = results.get("documents") or []
        metas = results.get("metadatas") or []
        if not docs:
            raise HTTPException(status_code=404, detail=f"No indexed content found for: {source}")

        from app.services.chunk_reconstruction import reconstruct_chunks
        content = reconstruct_chunks(zip(metas, docs))
        file_name = metas[0].get("file_name", source.split("/")[-1])
        language = metas[0].get("language", "")
        return {"content": content, "file_name": file_name, "language": language}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
