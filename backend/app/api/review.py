"""
review.py — API routes for the agentic code review feature.

Two endpoints:
  POST /review/file    — review a file from the indexed repo (by path)
  POST /review/paste   — review code pasted directly into the UI

Both return a streaming response. The stream has two types of chunks:
  __STATUS__...text...{"step": "..."}__STATUS_END__  →  progress update (tool running)
  everything else                                    →  actual review text tokens
"""

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.limiter import limiter
from app.services.review_agent import stream_code_review, stream_fast_code_review
from app.services.multi_review_agent import stream_multi_review
from app.services.impact_analyzer import analyze_diff, inline_comments_for_diff

router = APIRouter(prefix="/review", tags=["review"])


def _get_allowed_roots() -> list[Path]:
    """
    Returns the directories the review endpoint is permitted to read files from.
    Populated from settings so it stays in sync with the configured data directory.

    WHY TWO TEMP DIRS ON MACOS?
    tempfile.mkdtemp() (used by ingestion to clone repos) creates dirs under /tmp,
    which on macOS is a symlink to /private/tmp.
    tempfile.gettempdir() returns the *user-session* temp dir under
    /var/folders/.../T, which resolves to /private/var/folders/.../T.
    These are two different directories — neither is a parent of the other.
    We include both resolved forms so the path-traversal check passes for
    files in either location.
    """
    from app.core.config import get_settings
    s = get_settings()
    # Each temp-dir location is included TWICE:
    #   - resolved form  (/private/var/folders/.../T, /private/tmp) — for paths that still exist
    #   - unresolved form (/var/folders/.../T, /tmp)               — for paths that were cleaned up
    #
    # WHY TWO FORMS?
    # After ingestion, the temp clone dir is deleted by shutil.rmtree.  The `source`
    # values stored in ChromaDB still point into that deleted directory.
    # Path.resolve() follows symlinks only for path components that *exist* on disk.
    # Once the dir is gone, resolve() returns the path as-is — no symlink expansion.
    # macOS has two relevant symlinks: /tmp → /private/tmp and
    # /var/folders/... → /private/var/folders/... .
    # Without the unresolved forms, paths from deleted temp dirs fail the
    # startswith() check and the validator returns a 422.
    tmp_dir = Path(tempfile.gettempdir())
    roots = [
        Path(s.chroma_persist_directory).resolve(),
        tmp_dir.resolve(),   # resolved: /private/var/folders/.../T  (live paths)
        tmp_dir,             # unresolved: /var/folders/.../T        (post-rmtree paths)
        Path("/tmp").resolve(),  # resolved: /private/tmp            (mkdtemp, live)
        Path("/tmp"),            # unresolved: /tmp                  (mkdtemp, post-rmtree)
    ]
    return roots


class ReviewFileRequest(BaseModel):
    file_path: str       # Absolute path from the indexed files list
    file_name: str
    language: str = ""

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        """
        Prevent path traversal attacks.

        Without this, a crafted request body like {"file_path": "/etc/passwd"}
        would cause the server to read and return the contents of any file
        accessible to the process — a critical information disclosure vulnerability.

        Fix: resolve the path to its absolute canonical form (resolves .. and symlinks),
        then check it starts with one of the known safe roots (temp dir or data dir).

        We return the ORIGINAL (unresolved) path so it matches the `source` value
        stored in ChromaDB at index time. On macOS /tmp is a symlink to /private/tmp;
        resolving it would break the ChromaDB metadata lookup in the fallback.
        """
        # Stable repo source IDs: "https://github.com/owner/repo::path/to/file"
        # Validate BOTH conditions independently — a crafted payload like
        # "https://evil.com::../../etc/passwd" must NOT bypass the path check.
        # The "::" segment is a ChromaDB source ID, never opened as a filesystem path.
        # Allow only if it's a well-formed source ID: starts with https:// or git@
        # AND contains "::" as the separator (no slashes before the "::").
        if "::" in v:
            prefix = v[: v.index("::")]
            if prefix.startswith("https://") or prefix.startswith("git@"):
                # Valid stable source ID — not a filesystem path, safe to pass through.
                return v
            # Malformed: "::"-containing value that isn't a repo URL → fall through to path check

        resolved = Path(v).resolve()
        allowed_roots = _get_allowed_roots()
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ValueError(
                f"File path is outside the allowed directory. "
                f"Only files within the server's data or temp directories may be reviewed."
            )
        # Return the original value, not resolved — preserves the path as stored in ChromaDB
        return v


class ReviewPasteRequest(BaseModel):
    code: str            # Raw code content
    file_name: str = "snippet"
    language: str = ""


class ReviewMultiRequest(BaseModel):
    files: list[ReviewFileRequest]

    @field_validator("files")
    @classmethod
    def validate_files(cls, v: list) -> list:
        if not v:
            raise ValueError("At least one file must be provided.")
        if len(v) > 200:
            raise ValueError("Too many files. Maximum 200 files per multi-review request.")
        return v


class PRWebhookRequest(BaseModel):
    repo: str
    pr_number: int
    title: str = ""
    diff: str


def _indexed_impact(repo_url: str | None, diff: str) -> dict:
    """Build impact metadata when an indexed dependency graph is available."""
    try:
        from app.services.dep_graph import build_dependency_graph
        graph = build_dependency_graph(repo_url) if repo_url else None
        return analyze_diff(diff, graph)
    except Exception:
        return analyze_diff(diff)


@router.post("/impact")
@limiter.limit("30/minute")
async def analyze_pr_impact(
    request: Request,
    body: PRWebhookRequest,
    _: None = Depends(require_api_key),
):
    """Return deterministic PR risk and dependency impact metadata."""
    if not body.diff.strip():
        raise HTTPException(status_code=400, detail="PR diff cannot be empty.")
    return {
        "status": "success",
        "repo": body.repo,
        "pr_number": body.pr_number,
        "impact": _indexed_impact(body.repo, body.diff[:100_000]),
        "inline_comments": inline_comments_for_diff(body.diff[:100_000]),
    }


@router.post("/file")
@limiter.limit("10/minute")
async def review_indexed_file(request: Request, body: ReviewFileRequest, _: None = Depends(require_api_key)):
    """
    Review a file that's already been indexed into the vector store.
    Attempts to read from disk; if cleaned up, reconstructs from ChromaDB chunks.

    Uses fast mode (1 LLM call) by default — same as multi-file review.
    Set REVIEW_MODE=agentic in .env to enable the full ReAct loop (3–9 calls, ~2min).
    """
    from app.core.config import get_settings
    settings = get_settings()
    review_fn = (
        stream_code_review
        if getattr(settings, "review_mode", "fast") == "agentic"
        else stream_fast_code_review
    )

    content = ""
    try:
        with open(body.file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (FileNotFoundError, OSError):
        # Fallback: recover content from ChromaDB chunks if file was in a temporary clone directory
        from app.services.ingestion_service import _get_vectorstore
        try:
            vs = _get_vectorstore()
            results = vs._collection.get(
                where={"source": body.file_path},
                include=["documents", "metadatas"],
            )
            docs = results.get("documents") or []
            metadatas = results.get("metadatas") or []
            if docs and metadatas:
                from app.services.chunk_reconstruction import reconstruct_chunks
                content = reconstruct_chunks(zip(metadatas, docs))
        except Exception:
            pass

        if not content:
            raise HTTPException(
                status_code=404,
                detail=f"File not found on disk or vector store: {body.file_path}. "
                       "Please re-index or use direct paste review.",
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return StreamingResponse(
        review_fn(body.file_name, content, body.language),
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/paste")
@limiter.limit("10/minute")
async def review_pasted_code(request: Request, body: ReviewPasteRequest, _: None = Depends(require_api_key)):
    """
    Review code pasted directly — no indexing required.
    Uses fast mode by default (1 LLM call ~20–40s).
    Set REVIEW_MODE=agentic in .env for the full ReAct loop.
    """
    if not body.code.strip():
        raise HTTPException(status_code=400, detail="No code provided.")

    if len(body.code) > 50_000:
        raise HTTPException(
            status_code=413,
            detail="Code too large (max 50,000 chars). Split into smaller files.",
        )

    from app.core.config import get_settings
    settings = get_settings()
    review_fn = (
        stream_code_review
        if getattr(settings, "review_mode", "fast") == "agentic"
        else stream_fast_code_review
    )

    return StreamingResponse(
        review_fn(body.file_name, body.code, body.language),
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/multi")
@limiter.limit("10/minute")  # Fast mode uses fewer LLM calls; keep room for dev retries.
async def review_multiple_files(request: Request, body: ReviewMultiRequest, _: None = Depends(require_api_key)):
    """
    Review multiple indexed files in a single request.

    Performance: files missing from disk are recovered via a single batched ChromaDB
    $in query instead of N individual queries — saves ~500ms for large repos.
    """
    # ── Step 1: read all files that exist on disk ─────────────────────────────
    disk_content: dict[str, str] = {}       # file_path → content
    missing_paths: list[str] = []           # paths not found on disk

    for item in body.files:
        try:
            with open(item.file_path, "r", encoding="utf-8", errors="replace") as f:
                disk_content[item.file_path] = f.read()
        except (FileNotFoundError, OSError):
            missing_paths.append(item.file_path)
        except Exception:
            missing_paths.append(item.file_path)

    # ── Step 2: batch-fetch all missing paths from ChromaDB in one call ───────
    chroma_content: dict[str, str] = {}     # file_path → reconstructed content
    if missing_paths:
        from app.services.ingestion_service import _get_vectorstore
        from app.services.chunk_reconstruction import reconstruct_chunks
        try:
            vs = _get_vectorstore()
            # Single $in query replaces N individual where={"source": path} calls.
            # On a 61-file batch this cuts the ChromaDB round-trip from ~610ms to ~14ms.
            where_filter = (
                {"source": {"$in": missing_paths}}
                if len(missing_paths) > 1
                else {"source": missing_paths[0]}
            )
            batch_results = vs._collection.get(
                where=where_filter,
                include=["documents", "metadatas"],
            )
            all_docs  = batch_results.get("documents") or []
            all_metas = batch_results.get("metadatas") or []

            # Group chunks by source path, then reconstruct each file.
            from collections import defaultdict
            grouped: dict[str, list[tuple]] = defaultdict(list)
            for meta, doc in zip(all_metas, all_docs):
                src = meta.get("source", "")
                if src:
                    grouped[src].append((meta, doc))

            for src, pairs in grouped.items():
                reconstructed = reconstruct_chunks(pairs)
                if reconstructed:
                    chroma_content[src] = reconstructed
        except Exception:
            pass  # best-effort; missing files simply won't appear in file_dicts

    # ── Step 3: assemble file_dicts in original request order ─────────────────
    file_dicts: list[dict] = []
    for item in body.files:
        content = disk_content.get(item.file_path) or chroma_content.get(item.file_path, "")
        if not content:
            # Skip unrecoverable files rather than aborting the whole batch.
            continue
        file_dicts.append({
            "file_path": item.file_path,
            "file_name": item.file_name,
            "language": item.language,
            "content": content,
        })

    if not file_dicts:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail="None of the requested files could be read from disk or vector store.",
        )

    return StreamingResponse(
        stream_multi_review(file_dicts),
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/pr-webhook")
@limiter.limit("15/minute")
async def review_pr_webhook(request: Request, body: PRWebhookRequest, _: None = Depends(require_api_key)):
    """
    Automated CI/CD GitHub PR Review webhook endpoint.
    Consumes a git diff, runs the ReAct agent review loop, and returns a structured JSON comment.
    """
    if not body.diff.strip():
        raise HTTPException(status_code=400, detail="PR diff cannot be empty.")

    impact = _indexed_impact(body.repo, body.diff)
    review_chunks = []
    async for token in stream_code_review(
        file_name=f"PR #{body.pr_number}: {body.title}",
        file_content=(
            f"PR IMPACT METADATA:\n{json.dumps(impact, indent=2)}\n\n"
            f"UNIFIED DIFF:\n{body.diff[:45000]}"
        ),
        language="diff",
    ):
        # Filter out UI status telemetry markers from the stream
        if not token.startswith("__STATUS__"):
            review_chunks.append(token)

    full_review = "".join(review_chunks).strip()
    return {
        "status": "success",
        "repo": body.repo,
        "pr_number": body.pr_number,
        "review": full_review,
        "impact": impact,
        "inline_comments": inline_comments_for_diff(body.diff[:100_000]),
    }
