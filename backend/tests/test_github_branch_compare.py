"""Read-only remote branch comparison through GitHub's compare API."""

from app.services import github_service as gh


async def test_compare_branches_encodes_slash_refs_and_shapes_diff(monkeypatch):
    captured = {}

    async def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, kwargs=kwargs)
        return {
            "status": "ahead",
            "ahead_by": 2,
            "behind_by": 1,
            "total_commits": 2,
            "files": [
                {
                    "filename": "src/auth.py",
                    "status": "modified",
                    "additions": 3,
                    "deletions": 1,
                    "changes": 4,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                },
                {
                    "filename": "assets/logo.bin",
                    "status": "modified",
                    "additions": 0,
                    "deletions": 0,
                    "changes": 0,
                },
            ],
            "commits": [
                {
                    "sha": "1234567890",
                    "html_url": "https://github.com/o/r/commit/123",
                    "commit": {
                        "message": "Improve auth\nmore details",
                        "author": {"name": "SavFlux", "date": "2026-09-01T00:00:00Z"},
                    },
                }
            ],
        }

    monkeypatch.setattr(gh, "_request", fake_request)
    result = await gh.compare_branches("o/r", "main", "feature/auth-flow")

    assert captured["method"] == "GET"
    assert captured["path"] == "/repos/o/r/compare/main...feature%2Fauth-flow"
    assert result["ahead_by"] == 2
    assert result["behind_by"] == 1
    assert result["files"][0]["patch"].startswith("@@")
    assert result["files"][1]["patch"] is None
    assert result["commits"][0]["message"] == "Improve auth"
    assert result["commits"][0]["sha"] == "12345678"


async def test_compare_bounds_patch_payload_and_file_list(monkeypatch):
    async def fake_request(*_args, **_kwargs):
        return {
            "files": [
                {
                    "filename": f"src/{index}.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "x" * 30_000,
                }
                for index in range(105)
            ],
            "commits": [],
        }

    monkeypatch.setattr(gh, "_request", fake_request)
    result = await gh.compare_branches("o/r", "main", "feature/x")

    assert len(result["files"]) == 100
    assert result["files_truncated"] is True
    assert result["files"][0]["patch_truncated"] is True
    assert sum(len(item["patch"] or "") for item in result["files"]) <= 500_000


def test_compare_branches_route_is_read_only_and_surfaces_validation(client, monkeypatch):
    calls = []

    async def fake_compare(slug, *, base, head):
        calls.append((slug, base, head))
        return {"repo": slug, "base": base, "head": head, "files": []}

    monkeypatch.setattr("app.api.github.gh.compare_branches", fake_compare)

    response = client.post(
        "/api/v1/github/compare",
        json={"repo": "https://github.com/owner/project", "base": "main", "head": "feature/ui"},
    )
    assert response.status_code == 200
    assert response.json()["head"] == "feature/ui"
    assert calls == [("owner/project", "main", "feature/ui")]

    invalid = client.post(
        "/api/v1/github/compare",
        json={"repo": "owner/project", "base": "main", "head": "../main"},
    )
    assert invalid.status_code == 422
    assert len(calls) == 1
