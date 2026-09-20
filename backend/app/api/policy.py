"""
policy.py — API routes for the risk policy gate.

WHY THE GATE NEEDS ENDPOINTS
A gate that cannot be inspected is a gate people route around. Everything here
exists so the policy is arguable rather than mysterious:

  GET  /policy          — the thresholds, the signal weights, and the hint lists
                          that produce them. If a change scored 6, the reason is
                          on this page.
  GET  /policy/ledger   — every decision the gate has made, newest first, with
                          the signals that fired. "Why was this blocked?" has an
                          answer that does not depend on anyone's memory.
  POST /policy/assess   — score a change *without* attempting it. Lets a user
                          find out what would happen before building a patch, and
                          is what the UI calls to preview the gate.
  POST /policy/verify   — apply a diff in a throwaway repo with real `git apply`
                          and report whether it lands. The strongest single
                          signal the gate has, callable on its own.
  DELETE /policy/ledger — clear the ledger (key-gated; it is a destructive act).

None of these can push anything. Assessment deliberately has no side effects on
the ledger, so a preview never looks like an attempt.
"""

# NOTE: no `from __future__ import annotations` in this module, deliberately.
# With PEP 563 in effect, FastAPI resolves the string annotations through the
# *wrapper's* globals — and `@limiter.limit(...)` wraps these endpoints with
# `functools.wraps`, which copies __name__ and __module__ but not __globals__.
# The Pydantic body models then fail to resolve, and FastAPI silently degrades
# the parameter to a query parameter: a request with a perfectly good JSON body
# comes back 422 "query.body: Field required". Nothing in the stack raises, so
# the only symptom is a wrong-looking validation error.
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.limiter import limiter
from app.services import risk_policy
from app.services.risk_policy import ACTION_CREATE_PR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/policy", tags=["policy"])

#: The action a change is being assessed for. Only pushes are gated today, but the
#: parameter is in the API so a caller can ask about the gate as it applies to a
#: specific operation rather than to "risk in general".
_ACTIONS = (risk_policy.ACTION_AUTOFIX, risk_policy.ACTION_BUILD_PATCH, ACTION_CREATE_PR)


class AssessRequest(BaseModel):
    """A change to score. Supply `diff`, `files`, or both."""

    diff: str = ""
    files: list[dict] = []
    repo_url: str | None = None
    action: str = ACTION_CREATE_PR
    #: When known: whether a verifier already confirmed the change. Omitted means
    #: unknown, which is scored as unverified — a change nobody checked.
    verified: bool | None = None
    #: Supplying a token lets the caller test whether their approval would pass,
    #: which is how the UI avoids offering a button that cannot work.
    approval_token: str = ""
    approval_reason: str = ""

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        v = (v or ACTION_CREATE_PR).strip().lower()
        if v not in _ACTIONS:
            raise ValueError(f"unknown action {v!r}; expected one of {', '.join(_ACTIONS)}")
        return v

    @field_validator("files")
    @classmethod
    def validate_files(cls, v: list) -> list:
        if len(v) > 50:
            raise ValueError("too many files to assess (max 50)")
        for entry in v:
            if not isinstance(entry, dict) or "path" not in entry:
                raise ValueError("each file needs at least a `path`")
        return v


class VerifyRequest(BaseModel):
    diff: str
    #: Repo-relative path → current content. Optional: without it, `git apply` runs
    #: against an empty tree, which still proves the hunks are well-formed.
    originals: dict[str, str] = {}

    @field_validator("originals")
    @classmethod
    def validate_originals(cls, v: dict) -> dict:
        if len(v) > 50:
            raise ValueError("too many files to stage (max 50)")
        return v


@router.get("")
async def policy_status():
    """
    Current thresholds, signal weights and decision counts.

    Unauthenticated on purpose: this describes how the product behaves, and a user
    deciding whether to trust the gate should be able to read it before handing
    over a key.
    """
    return risk_policy.status()


@router.get("/ledger")
async def policy_ledger(limit: int = Query(50, ge=1, le=200)):
    """Recent gated decisions, newest first, with the signals behind each."""
    return {"entries": risk_policy.ledger(limit=limit)}


@router.post("/assess")
@limiter.limit("60/minute")
async def assess_change(request: Request, body: AssessRequest):
    """
    Score a change and report what the policy would do — without attempting it.

    A diff is applied in a scratch repo first, exactly as `/review/create-pr`
    does, and the files that land are parsed. That is not gold-plating: scoring a
    bare diff can only pattern-match its added lines, so the same change would
    score lower here than at the push, and a preview that disagrees with the gate
    is worse than no preview. One code path, one answer.

    Nothing is recorded in the ledger: this is a question, not a push. The
    response includes the approval token, so a caller that intends to proceed can
    assess once, show the user the score, and then push with the token it already
    has.
    """
    if not body.diff and not body.files:
        raise HTTPException(status_code=400, detail="supply `diff` or `files` to assess")

    files = body.files or None
    verified = body.verified
    verification: dict | None = None

    if body.diff and not files:
        from app.services.impact_analyzer import analyze_diff

        changed = analyze_diff(body.diff).get("changed_files", [])
        originals = await asyncio.to_thread(_indexed_originals, changed, body.repo_url)
        applied = await asyncio.to_thread(risk_policy.apply_diff, body.diff, originals)
        verification = {
            "verified": applied["verified"],
            "detail": applied["detail"],
            "files": len(applied.get("files") or {}),
        }
        # An explicit `verified` from the caller still wins: they may know
        # something about the change that a scratch apply cannot.
        if verified is None:
            verified = applied["verified"]
        files = [
            {"path": path, "content": content, "original": originals.get(path, "")}
            for path, content in (applied.get("files") or {}).items()
        ] or None

    risk, decision = await asyncio.to_thread(
        risk_policy.gate_change,
        action=body.action,
        diff=body.diff,
        files=files,
        repo_url=body.repo_url,
        verified=verified,
        approve_token=body.approval_token,
        approval_reason=body.approval_reason,
        verification_detail=(verification or {}).get("detail", ""),
    )
    return {
        "risk": risk.to_dict(),
        "policy": decision.to_dict(include_signals=False),
        # An explicit, unambiguous sentence for a UI to render verbatim, so
        # clients do not re-derive "can I push?" from three booleans and get it
        # wrong in the permissive direction.
        "verdict": decision.status,
        "verification": verification,
    }


def _indexed_originals(paths: list[str], repo_url: str | None) -> dict[str, str]:
    """Current content of the files a diff touches, from the index. Empty on a miss."""
    from app.services.indexed_content import read_indexed_file

    contents: dict[str, str] = {}
    for path in paths or []:
        content = read_indexed_file(path, repo_url=repo_url)
        if content:
            contents[path] = content
    return contents


@router.post("/verify")
@limiter.limit("30/minute")
async def verify_patch(request: Request, body: VerifyRequest):
    """
    Prove a diff applies, in a scratch repo, with real `git apply`.

    This is the check the gate uses as its strongest evidence, exposed so it can
    be run on its own — for a diff from anywhere, not just one SavFlux built.
    """
    if not body.diff.strip():
        raise HTTPException(status_code=400, detail="supply a unified diff to verify")

    return await asyncio.to_thread(
        risk_policy.verify_diff_applies, body.diff, body.originals
    )


@router.delete("/ledger", dependencies=[Depends(require_api_key)])
async def clear_policy_ledger():
    """
    Drop the decision ledger. Returns how many entries were removed.

    Key-gated because the ledger is the audit trail: clearing it is a deliberate
    act, and it is the only operation here that changes stored state.
    """
    removed = risk_policy.clear_ledger()
    return {"removed": removed}
