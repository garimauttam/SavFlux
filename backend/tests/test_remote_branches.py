"""Remote branch discovery stays read-only and requires a SavFlux session."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import require_api_key
from app.api.github import router
from app.services import github_service as gh


@pytest.mark.parametrize("token", ["", "test-github-token"])
async def test_branch_lookup_uses_public_api_or_connected_credentials(monkeypatch, token):
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json=[{
            "name": "feature/remote", "protected": True,
            "commit": {"sha": "123456789abcdef"},
        }])

    original_client = httpx.AsyncClient
    monkeypatch.setattr(gh, "get_token", lambda: token)
    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    result = await gh.list_branches("owner/project", page=2)
    assert result == [{"name": "feature/remote", "protected": True, "sha": "12345678"}]
    request = captured[0]
    assert request.url.path == "/repos/owner/project/branches"
    assert dict(request.url.params) == {"per_page": "100", "page": "2"}
    if token:
        assert request.headers["Authorization"] == f"Bearer {token}"
    else:
        assert "Authorization" not in request.headers


async def test_other_github_operations_still_require_connection(monkeypatch):
    monkeypatch.setattr(gh, "get_token", lambda: "")
    with pytest.raises(gh.GitHubError, match="not connected"):
        await gh._request("GET", "/user")
    with pytest.raises(gh.GitHubError, match="not connected"):
        await gh._request("POST", "/repos/o/r/pulls", allow_anonymous=True)


def branch_app(authenticated=True):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    if authenticated:
        app.dependency_overrides[require_api_key] = lambda: {"id": "test-user"}
    return app


def test_branch_endpoint_paginates_without_requiring_github_connection(monkeypatch):
    calls = []

    async def list_branches(slug, *, page):
        calls.append((slug, page))
        return [{"name": f"branch-{i}"} for i in range(100)] if page == 1 else [{"name": "last"}]

    monkeypatch.setattr(gh, "is_connected", lambda: False)
    monkeypatch.setattr(gh, "list_branches", list_branches)
    with TestClient(branch_app()) as client:
        first = client.get("/api/v1/github/repos/owner/project/branches")
        assert first.status_code == 200
        assert len(first.json()["branches"]) == 100
        assert first.json()["next_page"] == 2
        last = client.get("/api/v1/github/repos/owner/project/branches?page=2")
        assert last.json() == {"branches": [{"name": "last"}], "next_page": None}
        assert client.get("/api/v1/github/repos/owner/project/branches?page=0").status_code == 422
    assert calls == [("owner/project", 1), ("owner/project", 2)]


def test_savflux_auth_is_still_required(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated requests must not call GitHub")

    monkeypatch.setattr(gh, "list_branches", forbidden)
    with TestClient(branch_app(authenticated=False)) as client:
        assert client.get("/api/v1/github/repos/owner/project/branches").status_code == 401


@pytest.mark.parametrize("kind,status", [("rate_limit", 429), ("not_found", 404), ("auth", 401), ("network", 502)])
def test_branch_errors_remain_actionable(monkeypatch, kind, status):
    async def fail(*args, **kwargs):
        raise gh.GitHubError("GitHub lookup failed", kind=kind)

    monkeypatch.setattr(gh, "list_branches", fail)
    with TestClient(branch_app()) as client:
        response = client.get("/api/v1/github/repos/owner/project/branches")
    assert response.status_code == status
    assert response.json()["detail"] == "GitHub lookup failed"


async def test_public_repo_metadata_includes_default_branch_without_write_access(monkeypatch):
    captured = {}

    async def request(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"full_name": "owner/project", "default_branch": "trunk"}

    monkeypatch.setattr(gh, "_request", request)
    result = await gh.get_repo("owner/project")
    assert captured == {"method": "GET", "path": "/repos/owner/project", "allow_anonymous": True}
    assert result["default_branch"] == "trunk"
    assert result["permissions"]["push"] is False


def test_public_metadata_route_does_not_require_github_but_still_requires_savflux(monkeypatch):
    async def get_repo(slug):
        assert slug == "owner/project"
        return {"default_branch": "trunk"}

    monkeypatch.setattr(gh, "is_connected", lambda: False)
    monkeypatch.setattr(gh, "get_repo", get_repo)
    with TestClient(branch_app()) as client:
        response = client.get("/api/v1/github/repos/owner/project")
        assert response.status_code == 200
        assert response.json()["default_branch"] == "trunk"
    with TestClient(branch_app(authenticated=False)) as client:
        assert client.get("/api/v1/github/repos/owner/project").status_code == 401


@pytest.mark.parametrize("token", ["", "test-github-token"])
async def test_comparison_uses_read_only_public_or_authenticated_github(monkeypatch, token):
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={"status": "ahead", "ahead_by": 1, "files": [], "commits": []})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(gh, "get_token", lambda: token)
    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    result = await gh.compare_branches("owner/project", "main", "feature/test")
    assert result["ahead_by"] == 1
    request = captured[0]
    assert request.method == "GET"
    assert b"main...feature%2Ftest" in request.url.raw_path
    if token:
        assert request.headers["Authorization"] == f"Bearer {token}"
    else:
        assert "Authorization" not in request.headers


def test_compare_endpoint_requires_savflux_not_github_connection(monkeypatch):
    calls = []

    async def compare(slug, *, base, head):
        calls.append((slug, base, head))
        return {"status": "ahead"}

    monkeypatch.setattr(gh, "is_connected", lambda: False)
    monkeypatch.setattr(gh, "compare_branches", compare)
    body = {"repo": "owner/project", "base": "main", "head": "feature/test"}
    with TestClient(branch_app()) as client:
        assert client.post("/api/v1/github/compare", json=body).status_code == 200
    with TestClient(branch_app(authenticated=False)) as client:
        assert client.post("/api/v1/github/compare", json=body).status_code == 401
    assert calls == [("owner/project", "main", "feature/test")]


@pytest.mark.parametrize("status,remaining,kind", [(404, None, "not_found"), (403, "0", "rate_limit"), (429, None, "rate_limit")])
async def test_public_comparison_does_not_hide_access_or_rate_limit_errors(monkeypatch, status, remaining, kind):
    original_client = httpx.AsyncClient
    def respond(request):
        return httpx.Response(status, json={"message": "GitHub refused comparison"}, headers={"X-RateLimit-Remaining": remaining} if remaining else {})
    monkeypatch.setattr(gh, "get_token", lambda: "")
    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(gh.GitHubError) as exc:
        await gh.compare_branches("owner/project", "main", "feature/test")
    assert exc.value.kind == kind
