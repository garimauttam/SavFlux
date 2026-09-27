"""
review.py — API routes for the agentic code review feature.

Endpoints:
  POST   /review/file    — review a file from the indexed repo (by path)
  POST   /review/paste   — review code pasted directly into the UI
  POST   /review/multi   — review a set of files, planned and batched
  POST   /review/autofix — propose a verified fix for one finding
  GET    /review/cache   — what the content-hash review cache is holding
  DELETE /review/cache   — drop it (the next review is a cold one)

The review streams carry two kinds of chunk, framed by `app.services.stream_protocol`
— the same protocol the code agent, the writer and chat use:
  __STATUS__{"step": "...", "message": "..."}__STATUS_END__  →  progress (a stage, a
      file, a tool call, a per-stage duration) — never part of the answer
  everything else                                            →  review markdown

Markers are telemetry, so they are what a client renders as a timeline; the prose
between them is what it renders as the review.
"""

import asyncio
import json
import logging
import secrets
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.limiter import limiter
from app.services.review_agent import stream_code_review, stream_fast_code_review
from app.services.stream_protocol import is_protocol_token
from app.services.multi_review_agent import stream_multi_review
from app.services import review_cache
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
        # The disconnect is the only cancellation signal a streaming response gets,
        # and forwarding it is what turns Stop from "the output went quiet" into "the
        # model was let go". Both review modes take the same keyword, so the choice of
        # function above does not change what cancellation means.
        review_fn(body.file_name, content, body.language, should_stop=request.is_disconnected),
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
        review_fn(body.file_name, body.code, body.language, should_stop=request.is_disconnected),
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
        # The reader's disconnect is the only cancellation signal a streaming
        # response gets, and forwarding it is what makes Stop stop spending model
        # time rather than just hiding the output.
        stream_multi_review(file_dicts, should_stop=request.is_disconnected),
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
        # Filter out UI telemetry markers from the stream — the webhook posts one
        # review body to GitHub, where a marker would be visible to reviewers.
        if not is_protocol_token(token):
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
    #: Digest of `diff`, as returned by `POST /review/build-patch`. Required
    #: before the endpoint will create anything: it is the caller's proof that
    #: this exact change set was displayed to and approved by a human.
    confirm_digest: str = ""
    #: Second gate, for changes that are risky rather than merely unconfirmed.
    #: `approval_token` comes from a previous response's `risk.approval_token`
    #: and is bound to this exact change and this exact assessment, so a diff
    #: that moves — or a score that rises — invalidates it. `approval_reason`
    #: is a sentence for the ledger: who is approving what, and why.
    approval_token: str = ""
    approval_reason: str = ""
    #: Optional repo URL for the indexed dependency graph. Supplying it lets the
    #: risk gate measure blast radius instead of noting that it could not; the
    #: push itself still uses `repo`.
    repo_url: str | None = None

    @field_validator("approval_reason", "approval_token")
    @classmethod
    def validate_approval_fields(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > 500:
            raise ValueError("approval fields are too long (max 500 chars)")
        return v

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
    #: Exact indexed source id, e.g. "https://github.com/o/r::src/auth.py".
    #: Supplying it turns an index lookup into one metadata-filtered query
    #: instead of a suffix scan; omitting it still works, just more slowly.
    source: str | None = None


class AutofixRequest(BaseModel):
    """
    Run deterministic repairs over one file and return a ready patch.

    `path` is repo-relative: it becomes the `a/` and `b/` names in the diff, so
    an absolute path or an index id here would produce a patch nobody can apply.
    Content resolution is `content` → then the index, via `source` (the exact
    indexed id, when the caller has it) or `repo_url` + `path`.
    """

    path: str
    content: str = ""
    source: str | None = None
    repo_url: str | None = None

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

    The response carries `content` — the repaired file. The UI needs it to chain
    several files into one patch, and returning the text the backend actually
    verified is what keeps a client from re-deriving it (and disagreeing).

    The pipeline itself lives in `app.services.fix_service`, which the agent's
    `autofix` tool also calls: one set of gates, two callers.
    """
    from app.services.fix_service import (
        FixError,
        MissingContentError,
        UnsupportedLanguageError,
        apply_fixes,
    )

    try:
        outcome = await apply_fixes(
            body.path,
            content=body.content or None,
            source=body.source,
            repo_url=body.repo_url,
        )
    except MissingContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except UnsupportedLanguageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FixError as exc:  # pragma: no cover - base class, kept for completeness
        raise HTTPException(status_code=400, detail=str(exc))

    return outcome.to_dict()


class BatchAutofixRequest(BaseModel):
    """
    Repair a reviewed *set* of files and return one patch.

    The single-file endpoint is right for a file, but a review usually covers a
    dozen, and one request per file runs into the endpoint's rate limit long
    before it runs out of files — a 30-file "apply everything safe" click would
    spend its last ten files on HTTP 429. One request also means one patch, one
    digest, and one thing to confirm before a push.
    """

    files: list[ProposedChange]
    title: str = ""
    summary: str = ""
    repo_url: str | None = None

    @field_validator("files")
    @classmethod
    def validate_files(cls, v: list) -> list:
        if not v:
            raise ValueError("at least one file is required")
        if len(v) > 25:
            raise ValueError("too many files in one pass (max 25) — split the review")
        return v


@router.post("/autofix-set")
@limiter.limit("10/minute")
async def autofix_file_set(request: Request, body: BatchAutofixRequest,
                           _: None = Depends(require_api_key)):
    """
    Run the verified autofix over several files and build one patch from the result.

    Every file goes through `fix_service.apply_fixes`, so the gates are the same
    ones `POST /review/autofix` uses — the batch is a loop, not a second path. A
    file that cannot be fixed (not Python, not in the index) is reported under
    `errors` and does not stop the others: a review set is a mixed bag, and
    failing the whole batch because one file is JavaScript would be a bug.
    """
    import asyncio

    from app.services.fix_service import FixError, apply_fixes
    from app.services.patch_service import (
        FileChange,
        PatchError,
        build_patch,
        build_pr_body,
        suggest_branch_name,
    )

    results: list[dict] = []
    errors: list[dict] = []
    changes: list[FileChange] = []

    for proposed in body.files:
        try:
            outcome = await apply_fixes(
                proposed.path,
                content=proposed.content or None,
                source=proposed.source,
                repo_url=body.repo_url,
            )
        except FixError as exc:
            errors.append({"path": proposed.path, "reason": str(exc)})
            continue

        results.append(outcome.to_dict(include_content=False))
        if outcome.changed:
            changes.append(FileChange(proposed.path, outcome.original, outcome.content))

    response: dict = {
        "files": results,
        "scanned": len(results),
        "fixed_count": sum(1 for r in results if r["fixed"]),
        "errors": errors,
        "patch": None,
    }

    if not changes:
        return response  # honest empty result: nothing was safe to fix

    title = (body.title or "").strip() or f"SavFlux: verified fixes for {len(changes)} file(s)"

    def _build():
        result = build_patch(changes)
        return {
            **result.to_dict(),
            "title": title,
            "suggested_branch": suggest_branch_name(title),
            "pr_body": build_pr_body(body.summary or title, result),
        }

    try:
        response["patch"] = await asyncio.to_thread(_build)
    except PatchError as exc:
        # Every file changed but the combination is unrenderable (e.g. one file
        # over the size cap). Report the fixes; there is simply no patch.
        errors.append({"path": "*", "reason": str(exc)})

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
    #: Optional: when given, a missing `original` is fetched by exact source id
    #: instead of by suffix match — one indexed lookup instead of a scan.
    repo_url: str | None = None

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
        originals: dict[str, str] = {}
        for proposed in body.changes:
            original = proposed.original
            if original is None:
                # Not supplied — reconstruct from the index. A file that is not
                # indexed is treated as new rather than failing the request.
                original = _read_source_content(
                    proposed.path, body.repo_url, proposed.source
                ) or None
            if original:
                originals[proposed.path] = original

            changes.append(
                FileChange(
                    path=proposed.path,
                    original=original,
                    modified=None if proposed.delete else (proposed.content or ""),
                )
            )
        return build_patch(changes, context=max(0, min(body.context_lines, 10))), originals

    try:
        result, originals = await asyncio.to_thread(_resolve)
    except PatchError as e:
        raise HTTPException(status_code=400, detail=str(e))

    title = (body.title or "").strip() or "SavFlux: apply review fixes"

    # Assess as part of building, so the caller sees the score *before* it decides
    # to push — and so the approval token it can use later is issued here, next to
    # the diff it is bound to. Nothing is written to the ledger: this is an
    # assessment, not an attempted push.
    #
    # The patch is applied in a scratch repo first. The originals are already in
    # hand, so the verification is nearly free — and doing it here means the
    # dialog can say "this patch applies" before the user commits to a push,
    # rather than discovering it does not after being refused.
    from app.services.risk_policy import ACTION_CREATE_PR, apply_diff, assess_files, evaluate

    applied = await asyncio.to_thread(apply_diff, result.diff, originals)
    applied_files = applied.get("files") or {}
    risk = assess_files(
        [
            {
                "path": path,
                "content": content,
                "original": originals.get(path, ""),
            }
            for path, content in applied_files.items()
        ] or [
            {"path": c.path, "content": c.content or "", "original": c.original or ""}
            for c in body.changes
        ],
        repo_url=body.repo_url,
        verified=applied["verified"],
        verification_detail=applied["detail"],
    )
    decision = evaluate(ACTION_CREATE_PR, risk)

    return {
        **result.to_dict(),
        "title": title,
        "suggested_branch": suggest_branch_name(title),
        "pr_body": build_pr_body(body.summary, result, findings=body.findings),
        "risk": risk.to_dict(),
        "policy": decision.to_dict(include_signals=False),
        "verification": {
            "verified": applied["verified"],
            "detail": applied["detail"],
            "files": len(applied_files),
        },
    }


def _read_source_content(path: str, repo_url: str | None = None,
                         source: str | None = None) -> str:
    """
    Best-effort current content for a repo-relative path.

    Temp clones are deleted after ingest, so ChromaDB is usually the only copy.
    The lookup ladder (exact source id → file-name filter → full scan) lives in
    `app.services.indexed_content`; this wrapper exists so the review module keeps
    its historical call signature. Returning "" on a miss is deliberate — the
    caller then treats the file as new, which produces a valid patch either way.
    """
    from app.services.indexed_content import read_indexed_file

    return read_indexed_file(path, source=source, repo_url=repo_url)


@router.post("/create-pr")
@limiter.limit("10/minute")
async def create_pull_request(request: Request, body: CreatePRRequest,
                              _: None = Depends(require_api_key)):
    """
    Create a GitHub PR — live via API when GITHUB_TOKEN is configured,
    otherwise a deterministic manual plan ($0, offline-safe).

    Creating a pull request is the one irreversible thing this product does, so
    it is never implicit. A caller that supplies a `diff` must also supply the
    `confirm_digest` that `build-patch` returned for it; without a match the
    endpoint answers with the plan (branch, body, `gh` command, patch) and
    pushes nothing. Two things that would otherwise be races are therefore
    impossible: pushing a diff the user never saw, and pushing an older diff
    than the one they approved.

    Live:   {status: "created", number, url}
    Manual: {status: "manual", gh_command, patch?, reason, risk, policy}

    Every response carries the risk assessment and the policy decision, including
    the pushes that proceed — so "why did this go out?" has an answer as well as
    "why was it refused?".
    """
    from app.services.pr_service import (
        parse_repo_ref, validate_branches, build_gh_command, create_pr_via_api,
    )
    from app.services.patch_service import digest_of
    import os
    try:
        repo_slug = parse_repo_ref(body.repo)
        head, base = validate_branches(body.head, body.base)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    expected_digest = digest_of(body.diff) if body.diff else ""
    confirmed = bool(body.diff) and secrets.compare_digest(body.confirm_digest.strip(), expected_digest)

    plan = {
        "status": "manual",
        "repo": repo_slug,
        "head": head,
        "base": base,
        "digest": expected_digest,
        "gh_command": build_gh_command(repo_slug, head, base, body.title, body.body),
        "patch": body.diff[:100_000] or None,
    }

    # ── Risk policy ──────────────────────────────────────────────────────────
    # This runs before anything can be pushed and before the confirmation check
    # is reported, because "should this change go out?" comes before "is this
    # the diff you saw?". The assessment is returned either way, so the caller
    # always learns what the gate thought — including on a push that proceeds.
    #
    # Verification is attempted here rather than assumed: `git apply --check`
    # against the real file contents is the only thing that proves a patch
    # applies, and its result feeds the score.
    from app.services.risk_policy import (
        ACTION_CREATE_PR,
        apply_diff,
        gate_change,
        record_decision,
    )

    # Apply the patch once, in a scratch repo, and use the result twice: as the
    # verification signal, and as the source of the post-change content that gets
    # parsed. Pattern-matching a diff tells you it *looks* dangerous; parsing the
    # file it produces tells you what it *is* — same evidence standard a review
    # is held to.
    risk_files: list[dict] = []
    verification: dict = {"verified": None, "detail": "no diff supplied", "files": 0}
    if body.diff:
        from app.services.impact_analyzer import analyze_diff as _analyze_diff

        changed = _analyze_diff(body.diff).get("changed_files", [])
        originals = {
            path: content for path, content in (
                (p, _read_source_content(p, body.repo_url)) for p in changed
            ) if content
        }
        applied = await asyncio.to_thread(apply_diff, body.diff, originals)
        verification = {
            "verified": applied["verified"],
            "detail": applied["detail"],
            "files": len(applied.get("files") or {}),
        }
        risk_files = [
            {"path": path, "content": content, "original": originals.get(path, "")}
            for path, content in (applied.get("files") or {}).items()
        ]

    risk, decision = gate_change(
        action=ACTION_CREATE_PR,
        diff=body.diff,
        files=risk_files or None,
        repo_url=body.repo_url,
        verified=verification.get("verified"),
        approve_token=body.approval_token,
        approval_reason=body.approval_reason,
        verification_detail=verification.get("detail", ""),
    )
    plan["risk"] = risk.to_dict()
    plan["verification"] = verification

    if decision.blocked:
        # Not a 403: a refusal is a normal response that carries the escape hatch.
        # The user gets the patch, the branch, and the command — the only thing
        # they have lost is SavFlux doing the pushing for them.
        plan["reason"] = decision.reason
        plan["policy"] = decision.to_dict(include_signals=False)
        record_decision(decision, outcome="blocked", repo=repo_slug, actor="api")
        return plan

    if decision.requires_approval and not decision.approved:
        plan["reason"] = decision.reason
        plan["policy"] = decision.to_dict(include_signals=False)
        record_decision(decision, outcome="refused_no_approval",
                        repo=repo_slug, actor="api")
        return plan

    if body.diff and not confirmed:
        plan["reason"] = (
            "Confirmation required: the diff has not been confirmed. Display it, then "
            f"resend with confirm_digest={expected_digest!r} (as returned by /review/build-patch). "
            "Nothing was pushed."
        )
        plan["policy"] = decision.to_dict(include_signals=False)
        return plan

    plan["policy"] = decision.to_dict(include_signals=False)

    if os.getenv("GITHUB_TOKEN", "").strip():
        try:
            created = await create_pr_via_api(repo_slug, head, base, body.title, body.body)
            record_decision(decision, outcome="created", repo=repo_slug, actor="api",
                            detail=f"PR #{created.get('number')}")
            return {"status": "created", "repo": repo_slug, "risk": plan["risk"],
                    "policy": plan["policy"], **created}
        except RuntimeError as e:
            # Fall through to the manual plan with the API error attached
            plan["reason"] = f"GitHub API failed ({e}); use the command below instead."
            record_decision(decision, outcome="api_failed", repo=repo_slug, actor="api",
                            detail=str(e))
            return plan

    plan["reason"] = "GITHUB_TOKEN not configured — run the command below (gh CLI) to open the PR."
    record_decision(decision, outcome="manual_plan", repo=repo_slug, actor="api")
    return plan


# ── Review cache ──────────────────────────────────────────────────────────────
#
# The review pipeline caches each file's review against the hash of its content,
# the model, and the prompt version. That cache is the reason a second review of
# an unchanged repository costs a fraction of a second instead of a minute, so it
# is worth being able to look at: `stats` reports size and hit rate, and the
# delete endpoint exists so a user can force a cold review without hunting for a
# file on disk.


@router.get("/cache")
def review_cache_stats():
    """
    Report what the review cache holds and how well it is working.

    `hits` counts reviews served from cache; `writes` counts reviews a model
    actually produced. A high hit rate means an unchanged repository — which is
    the normal case when someone re-runs a review on a branch they have not
    touched.
    """
    return review_cache.stats()


@router.delete("/cache", dependencies=[Depends(require_api_key)])
def clear_review_cache():
    """
    Drop every cached review. Returns the number of entries removed.

    Requires an API key because it discards work that cost model calls to
    produce; it does not delete anything else.
    """
    removed = review_cache.clear()
    return {"removed": removed, "entries": review_cache.stats()["entries"]}
