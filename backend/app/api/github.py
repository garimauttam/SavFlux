"""
github.py — the GitHub surface, as endpoints.

Everything here is a thin, honest wrapper over `app.services.github_service`.
The rules that matter and are enforced rather than documented:

  * A route that needs GitHub says so with 409, not 500, when nothing is
    connected. "Connect your account" is a different instruction from
    "something broke", and the UI renders them differently.
  * `GitHubError` never escapes as a 500. Each message is chosen for a human,
    because the previous integration's only failure mode was a 401 the user
    could not act on.
  * No response contains the token. `POST /connect` returns the account it
    belongs to, never the credential.

Endpoints:
  GET    /github/status                 connected? who? is the token still valid?
  POST   /github/connect                save a PAT, validated against GitHub first
  DELETE /github/connect                forget the saved token
  GET    /github/repos                  the account's repositories, searchable
  GET    /github/repos/{owner}/{name}   one repository + the caller's permissions
  GET    /github/repos/{owner}/{name}/branches
  POST   /github/compare               compare two remote branch refs, read-only
  GET    /github/repos/{owner}/{name}/pulls
  GET    /github/pulls/{owner}/{name}/{number}
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import require_api_key
from app.services import github_service as gh

router = APIRouter(prefix="/github", tags=["github"])


# The field is named `token` and nothing else is, deliberately: an earlier
# revision of this file defined `ConnectRequest` with a `token()` *method* of
# the same name, which replaced the field outright, so every request parsed as
# missing its credential. One class, one field, no shadowing.
class ConnectPayload(BaseModel):
    token: str = Field(..., min_length=8, max_length=400)


class BranchComparePayload(BaseModel):
    repo: str = Field(..., min_length=3, max_length=300)
    base: str = Field(..., min_length=1, max_length=100)
    head: str = Field(..., min_length=1, max_length=100)


def _fail(exc: gh.GitHubError) -> HTTPException:
    """Turn a service error into the right status code.

    401 for a bad credential and 404 for a repo the token cannot see are both
    the user's problem to fix, but they are not the same problem, and collapsing
    them into 500 is what made the old PR path unusable.
    """
    code = {
        "auth": 401,
        "rate_limit": 429,
        "not_found": 404,
        "validation": 422,
        "network": 502,
    }.get(exc.kind, 500)
    return HTTPException(status_code=code, detail=str(exc))


async def _require_connection() -> str:
    if not gh.is_connected():
        raise HTTPException(
            status_code=409,
            detail="GitHub is not connected. POST a token to /api/v1/github/connect first.",
        )
    return gh.get_token()


@router.get("/status")
async def github_status(_: None = Depends(require_api_key)):
    """Connection state for the top bar. Never 500s — an unreachable GitHub is
    reported in the body, because "cannot reach GitHub" is a status of this
    integration, not a failure of the request."""
    return await gh.status()


@router.post("/connect")
async def connect(payload: ConnectPayload, _: None = Depends(require_api_key)):
    """Validate a PAT against GitHub, and only then persist it.

    Validation comes first on purpose: storing an unvalidated token writes a
    credential that is silently wrong until the user tries to use it, and the
    next status check then reports "connected" for something that cannot work.
    """
    token = payload.token.strip()
    try:
        viewer = await gh.get_viewer(token)
    except gh.GitHubError as exc:
        raise _fail(exc)
    gh.store_token(token)
    return {"ok": True, "user": viewer, "stored": True, "message": None}


@router.delete("/connect")
async def disconnect(_: None = Depends(require_api_key)):
    """Forget the token connected from the app.

    An env-provided token is reported, not deleted — this process does not own
    it, and pretending otherwise would leave the user thinking they are signed
    out when the header still shows an account. The message says which one is
    still in charge.
    """
    import os

    removed = gh.clear_token()
    # The fallback is reported whenever the environment still holds a token,
    # regardless of whether an app token was there to remove.
    #
    # It used to be `env_present and not removed`, which was exactly backwards
    # for the case this endpoint exists for: a person who connected a token in
    # the app, disconnected it, and still sees an account in the header. That is
    # `removed=True, env_present=True` — and it reported no fallback and no
    # message, so the product claimed they were signed out while requests kept
    # authenticating. The two cases where `not removed` helps are both ones
    # where the answer is already obvious: no app token and no env token means
    # genuinely signed out, and no app token with an env token was never signed
    # in to begin with.
    env_present = bool(os.getenv("GITHUB_TOKEN", "").strip())
    return {
        "ok": True,
        "removed": removed,
        "fallback": "env" if env_present else None,
        "message": "GITHUB_TOKEN is set in the server environment, so a token from the "
                   "environment is still in use. Unset it to sign out fully."
                   if env_present else None,
    }


@router.get("/repos")
async def repos(
    q: str = Query("", description="server-side search over name and description"),
    sort: str = Query("pushed", pattern="^(pushed|updated|created|full_name)$"),
    per_page: int = Query(50, ge=1, le=100),
    page: int = Query(1, ge=1, le=20),
    _: None = Depends(require_api_key),
):
    await _require_connection()
    try:
        items = await gh.list_repos(per_page=per_page, page=page, sort=sort, query=q.strip())
    except gh.GitHubError as exc:
        raise _fail(exc)
    return {"repos": items, "query": q, "page": page, "per_page": per_page}


@router.get("/repos/{owner}/{name}")
async def repo(owner: str, name: str, _: None = Depends(require_api_key)):
    await _require_connection()
    try:
        return await gh.get_repo(f"{owner}/{name}")
    except gh.GitHubError as exc:
        raise _fail(exc)


@router.get("/repos/{owner}/{name}/branches")
async def branches(owner: str, name: str, _: None = Depends(require_api_key)):
    await _require_connection()
    try:
        items = await gh.list_branches(f"{owner}/{name}")
    except gh.GitHubError as exc:
        raise _fail(exc)
    return {"branches": items}


@router.post("/compare")
async def compare_branches(
    payload: BranchComparePayload,
    _: None = Depends(require_api_key),
):
    """Compare two remote GitHub refs without mutating either branch."""
    from app.services.pr_service import parse_repo_ref, validate_branches

    try:
        slug = parse_repo_ref(payload.repo)
        head, base = validate_branches(payload.head, payload.base)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return await gh.compare_branches(slug, base=base, head=head)
    except gh.GitHubError as exc:
        raise _fail(exc)


@router.get("/repos/{owner}/{name}/pulls")
async def pulls(
    owner: str,
    name: str,
    state: str = Query("open", pattern="^(open|closed|all)$"),
    _: None = Depends(require_api_key),
):
    await _require_connection()
    try:
        items = await gh.list_pulls(f"{owner}/{name}", state=state)
    except gh.GitHubError as exc:
        raise _fail(exc)
    return {"pulls": items, "state": state}


@router.get("/pulls/{owner}/{name}/{number}")
async def pull(owner: str, name: str, number: int, _: None = Depends(require_api_key)):
    await _require_connection()
    try:
        return await gh.get_pull(f"{owner}/{name}", number)
    except gh.GitHubError as exc:
        raise _fail(exc)
