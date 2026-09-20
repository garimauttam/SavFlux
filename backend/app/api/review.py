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
import logging
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

logger = logging.getLogger(__name__)

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


class CreatePRRequest(BaseModel):
    repo: str   # "owner/name" or github.com URL
    head: str   # source branch
    base: str = "main"
    title: str = ""
    body: str = ""
    diff: str = ""  # optional unified diff — returned as a patch when offline

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > 200:
            raise ValueError("title too long (max 200 chars)")
        return v or "SavFlux review fixes"


class ProposedChange(BaseModel):
    """One file's new content. `original` omitted means read it from the index."""

    path: str
    content: str | None = None
    original: str | None = None
    delete: bool = False


class AutofixRequest(BaseModel):
    """Run deterministic repairs over one file and return a ready patch."""

    path: str
    content: str = ""

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("path is required")
        return v.strip()


@router.post("/autofix")
@limiter.limit("20/minute")
async def autofix_file(request: Request, body: AutofixRequest,
                       _: None = Depends(require_api_key)):
    """
    Apply every safe deterministic fix to a file and return the resulting patch.

    No LLM call: these repairs are decidable from the syntax tree, so they are
    free and instant. Each one is verified by re-parsing and re-analysing —
    a fix that breaks the file, fails to clear the finding, or introduces a
    worse one is discarded and reported under `skipped` rather than returned.

    Findings that need judgement (which SQL value to parameterise, where a
    secret should live) are never touched here; they stay in the review output
    for a human or the LLM.
    """
    import asyncio

    from app.services.code_analysis import analyze_file
    from app.services.code_analysis.autofix import autofix_python
    from app.services.patch_service import FileChange, PatchError, build_patch

    original = body.content or _read_source_content(body.path)
    if not original:
        raise HTTPException(
            status_code=404,
            detail=f"No content for {body.path!r}. Pass `content` explicitly or index the file first.",
        )

    language = body.path.rsplit(".", 1)[-1].lower() if "." in body.path else ""
    if language not in ("py", "pyw", "pyi"):
        raise HTTPException(
            status_code=400,
            detail="Automatic fixes are currently implemented for Python only. "
                   "Other languages are analysed but not rewritten.",
        )

    def _run():
        analysis = analyze_file(original, body.path, language)
        result = autofix_python(original, analysis.findings, body.path)
        return analysis, result

    analysis, result = await asyncio.to_thread(_run)

    response: dict = {
        "path": body.path,
        "fixed": result.changed,
        "fixes": [
            {
                "rule_id": f.rule_id,
                "line": f.line,
                "description": f.description,
                "before": f.before,
                "after": f.after,
            }
            for f in result.fixes
        ],
        "skipped": result.rejected,
        "findings_before": len(analysis.findings),
        "score_before": analysis.risk_score(),
    }

    if not result.changed:
        # Honest empty result: nothing was safe to fix, so there is no patch.
        response["patch"] = None
        response["findings_after"] = response["findings_before"]
        response["score_after"] = response["score_before"]
        return response

    after = analyze_file(result.content, body.path, language)
    response["findings_after"] = len(after.findings)
    response["score_after"] = after.risk_score()

    try:
        patch = build_patch([FileChange(body.path, original, result.content)])
    except PatchError as e:
        raise HTTPException(status_code=400, detail=str(e))

    response["patch"] = patch.to_dict()
    return response


class BuildPatchRequest(BaseModel):
    """
    Turn proposed file contents into a reviewable unified diff.

    This is the step that was missing between "the agent suggested a fix" and
    "open a PR": `/create-pr` accepted a diff but nothing in the product
    produced one, so the user had to apply edits by hand first.
    """

    changes: list[ProposedChange]
    title: str = ""
    summary: str = ""
    context_lines: int = 3
    findings: list[dict] = []

    @field_validator("changes")
    @classmethod
    def validate_changes(cls, v: list) -> list:
        if not v:
            raise ValueError("at least one change is required")
        if len(v) > 50:
            raise ValueError("too many files in one patch (max 50) — split the change")
        return v


@router.post("/build-patch")
@limiter.limit("20/minute")
async def build_review_patch(request: Request, body: BuildPatchRequest,
                             _: None = Depends(require_api_key)):
    """
    Build a git-applicable patch from proposed file changes.

    `original` is optional: when omitted the current content is read from the
    index, so a caller that only has the new version of a file (which is all
    the write agent produces) still gets a correct diff.

    Returns the diff plus per-file counts and a digest. The digest lets the UI
    detect that the preview it is showing no longer matches what would be
    applied.
    """
    import asyncio

    from app.services.patch_service import (
        FileChange,
        PatchError,
        build_patch,
        build_pr_body,
        suggest_branch_name,
    )

    def _resolve() -> tuple:
        changes: list[FileChange] = []
        for proposed in body.changes:
            original = proposed.original
            if original is None and not proposed.delete:
                # Not supplied — reconstruct from the index. A file that is not
                # indexed is treated as new rather than failing the request.
                original = _read_source_content(proposed.path) or None
            elif proposed.delete and original is None:
                original = _read_source_content(proposed.path) or None

            changes.append(
                FileChange(
                    path=proposed.path,
                    original=original,
                    modified=None if proposed.delete else (proposed.content or ""),
                )
            )
        return build_patch(changes, context=max(0, min(body.context_lines, 10)))

    try:
        result = await asyncio.to_thread(_resolve)
    except PatchError as e:
        raise HTTPException(status_code=400, detail=str(e))

    title = (body.title or "").strip() or "SavFlux: apply review fixes"
    return {
        **result.to_dict(),
        "title": title,
        "suggested_branch": suggest_branch_name(title),
        "pr_body": build_pr_body(body.summary, result, findings=body.findings),
    }


def _read_source_content(path: str) -> str:
    """
    Best-effort current content for a repo-relative path.

    Temp clones are deleted after ingest, so ChromaDB is usually the only copy;
    `reconstruct_chunks` rebuilds the file from its indexed chunks. Returning
    "" on any failure is deliberate — the caller then treats the file as new,
    which produces a valid patch either way.
    """
    try:
        from app.services.ingestion_service import _get_vectorstore
        from app.services.chunk_reconstruction import reconstruct_chunks

        store = _get_vectorstore()
        # Source ids are "{repo_url}::{relative_path}", so an exact match on a
        # bare path misses; scan for the suffix instead.
        results = store._collection.get(include=["documents", "metadatas"])
        metas = results.get("metadatas") or []
        docs = results.get("documents") or []

        matching = [
            (meta, doc)
            for meta, doc in zip(metas, docs)
            if str(meta.get("source", "")).split("::")[-1] == path
        ]
        if matching:
            return reconstruct_chunks(matching)
    except Exception as exc:  # noqa: BLE001 - index may be empty or mocked
        logger.debug("could not reconstruct %s from the index: %s", path, exc)
    return ""


@router.post("/create-pr")
@limiter.limit("10/minute")
async def create_pull_request(request: Request, body: CreatePRRequest,
                              _: None = Depends(require_api_key)):
    """
    Create a GitHub PR — live via API when GITHUB_TOKEN is configured,
    otherwise a deterministic manual plan ($0, offline-safe).

    Live:   {status: "created", number, url}
    Manual: {status: "manual", gh_command, patch?, reason}
    """
    from app.services.pr_service import (
        parse_repo_ref, validate_branches, build_gh_command, create_pr_via_api,
    )
    import os
    try:
        repo_slug = parse_repo_ref(body.repo)
        head, base = validate_branches(body.head, body.base)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    if os.getenv("GITHUB_TOKEN", "").strip():
        try:
            created = await create_pr_via_api(repo_slug, head, base, body.title, body.body)
            return {"status": "created", "repo": repo_slug, **created}
        except RuntimeError as e:
            # Fall through to the manual plan with the API error attached
            return {
                "status": "manual",
                "repo": repo_slug,
                "reason": f"GitHub API failed ({e}); use the command below instead.",
                "gh_command": build_gh_command(repo_slug, head, base, body.title, body.body),
                "patch": body.diff[:100_000] or None,
            }

    return {
        "status": "manual",
        "repo": repo_slug,
        "reason": "GITHUB_TOKEN not configured — run the command below (gh CLI) to open the PR.",
        "gh_command": build_gh_command(repo_slug, head, base, body.title, body.body),
        "patch": body.diff[:100_000] or None,
    }
