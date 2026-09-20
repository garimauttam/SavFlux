"""
test_pr_security.py — Unit tests for Create-PR helpers, CVE parsers, prune.
All pure logic: no network, no ChromaDB.

The create-pr tests carry the `isolated_data_dir` fixture because that endpoint
now records every decision in the risk policy ledger — without isolation they
write into the developer's real `chroma_data/`, which is the exact failure mode
that fixture exists to prevent.
"""

import pytest

from app.services.patch_service import digest_of


# ── pr_service ────────────────────────────────────────────────────────────────

def test_parse_repo_ref():
    from app.services.pr_service import parse_repo_ref
    assert parse_repo_ref("octo/repo") == "octo/repo"
    assert parse_repo_ref("https://github.com/octo/repo") == "octo/repo"
    assert parse_repo_ref("https://github.com/octo/repo.git") == "octo/repo"
    assert parse_repo_ref("git@github.com:octo/repo.git") == "octo/repo"
    with pytest.raises(ValueError):
        parse_repo_ref("not-a-repo")
    with pytest.raises(ValueError):
        parse_repo_ref("https://gitlab.com/octo/repo")


def test_build_gh_command_quotes():
    from app.services.pr_service import build_gh_command
    cmd = build_gh_command("o/r", "feat/x", "main", "Add things; rm -rf /", "body here")
    assert cmd.startswith("gh pr create")
    assert "'Add things; rm -rf /'" in cmd  # shell-quoted, not executed


def test_validate_branches():
    from app.services.pr_service import validate_branches
    assert validate_branches("feat/x", "") == ("feat/x", "main")
    with pytest.raises(ValueError):
        validate_branches("main", "main")
    with pytest.raises(ValueError):
        validate_branches("feat;evil", "main")


# ── security_service parsers ──────────────────────────────────────────────────

def test_parse_requirements():
    from app.services.security_service import parse_requirements
    pkgs = parse_requirements("django==4.2.7\nrequests>=2.0  # comment\n# bare\nflask\n")
    by_name = {p["name"]: p for p in pkgs}
    assert by_name["django"]["version"] == "4.2.7"
    assert by_name["requests"]["version"] is None
    assert by_name["flask"]["version"] is None
    assert all(p["ecosystem"] == "PyPI" for p in pkgs)


def test_parse_package_json():
    from app.services.security_service import parse_package_json
    pkgs = parse_package_json('{"dependencies": {"left-pad": "1.3.0", "x": "^2.1.0", "y": "latest"}}')
    by_name = {p["name"]: p for p in pkgs}
    assert by_name["left-pad"]["version"] == "1.3.0"
    assert by_name["x"]["version"] == "2.1.0"  # ^ stripped, exact semver kept
    assert by_name["y"]["version"] is None
    assert parse_package_json("not json") == []


def test_parse_go_mod():
    from app.services.security_service import parse_go_mod
    pkgs = parse_go_mod("module x\n\nrequire (\n\tgithub.com/a/b v1.2.3\n\tgithub.com/c/d v0.0.0-20240101120000-abc123 // indirect\n)\n")
    assert ("github.com/a/b", "1.2.3") in [(p["name"], p["version"]) for p in pkgs]
    assert all(p["ecosystem"] == "Go" for p in pkgs)


def test_parse_manifest_dispatch():
    from app.services.security_service import parse_manifest
    assert parse_manifest("requirements.txt", "a==1.0")[0]["ecosystem"] == "PyPI"
    assert parse_manifest("Dockerfile", "FROM x") == []


# ── prune_chat_history ────────────────────────────────────────────────────────

def test_prune_keeps_newest_and_budget():
    from app.services.query_enhancer import prune_chat_history
    hist = [{"role": "user", "content": "q%d" % i + ("x" * 400)} for i in range(10)]
    out = prune_chat_history(hist, max_tokens=500, keep_last=2)
    assert out["dropped"] > 0
    assert out["tokens_after"] <= 500 + 200  # keep_last may slightly exceed
    assert out["messages"][-1]["content"].startswith("q9")  # newest kept
    assert out["tokens_before"] > out["tokens_after"]


def test_prune_under_budget_is_noop():
    from app.services.query_enhancer import prune_chat_history
    hist = [{"role": "user", "content": "hi"}]
    out = prune_chat_history(hist)
    assert out["dropped"] == 0
    assert out["messages"] == hist


# ── POST /review/create-pr — the confirmation gate ───────────────────────────
#
# Creating a pull request is the only irreversible thing SavFlux does. The rule is
# that a diff must have been displayed and confirmed before anything is pushed, and
# this endpoint enforces it server-side rather than trusting a UI checkbox.

DIFF = (
    "diff --git a/net.py b/net.py\n"
    "--- a/net.py\n"
    "+++ b/net.py\n"
    "@@ -1,3 +1,3 @@\n"
    " import requests\n"
    "-    requests.get(u, verify=False)\n"
    "+    requests.get(u)\n"
)


@pytest.fixture()
def github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")


@pytest.fixture()
def fake_github_api(monkeypatch):
    from unittest.mock import AsyncMock
    api = AsyncMock(return_value={"number": 3, "url": "https://github.com/o/r/pull/3",
                                  "state": "open"})
    monkeypatch.setattr("app.services.pr_service.create_pr_via_api", api)
    return api


def test_create_pr_requires_the_confirmation_digest(isolated_data_dir, client, github_token, fake_github_api):
    response = client.post("/api/v1/review/create-pr", json={
        "repo": "o/r", "head": "savflux/fix", "base": "main",
        "title": "Fix TLS", "diff": DIFF,
    })
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "manual"
    assert data["digest"] == digest_of(DIFF)
    assert "confirmation required" in data["reason"].lower()
    assert data["gh_command"].startswith("gh pr create --repo o/r")
    assert data["patch"] == DIFF
    fake_github_api.assert_not_called()


def test_create_pr_refuses_a_digest_for_a_different_diff(isolated_data_dir, client, github_token, fake_github_api):
    response = client.post("/api/v1/review/create-pr", json={
        "repo": "o/r", "head": "savflux/fix", "diff": DIFF,
        "confirm_digest": digest_of(DIFF + "+ extra\n"),
    })
    assert response.json()["status"] == "manual"
    fake_github_api.assert_not_called()


def test_create_pr_pushes_once_the_exact_diff_is_confirmed(isolated_data_dir, client, github_token, fake_github_api):
    response = client.post("/api/v1/review/create-pr", json={
        "repo": "o/r", "head": "savflux/fix", "base": "main", "title": "Fix TLS",
        "diff": DIFF, "confirm_digest": digest_of(DIFF),
    })
    data = response.json()

    assert data["status"] == "created" and data["url"] == "https://github.com/o/r/pull/3"
    fake_github_api.assert_awaited_once_with("o/r", "savflux/fix", "main", "Fix TLS", "")


def test_create_pr_still_explains_itself_without_a_token(isolated_data_dir, client, monkeypatch, fake_github_api):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    response = client.post("/api/v1/review/create-pr", json={
        "repo": "o/r", "head": "savflux/fix", "diff": DIFF,
        "confirm_digest": digest_of(DIFF),
    })
    data = response.json()

    assert data["status"] == "manual"
    assert "GITHUB_TOKEN" in data["reason"]
    fake_github_api.assert_not_called()


def test_create_pr_without_a_diff_opens_the_pr_between_existing_branches(isolated_data_dir, client, github_token, fake_github_api):
    """No diff means nothing is pushed by us, so there is nothing to confirm."""
    response = client.post("/api/v1/review/create-pr", json={
        "repo": "o/r", "head": "feature", "base": "main", "title": "Release",
    })
    assert response.json()["status"] == "created"
    fake_github_api.assert_awaited_once()


async def test_create_pr_via_api_wraps_transport_failures(monkeypatch):
    """
    No network, bad TLS, DNS failure — all of them must arrive as RuntimeError so
    the endpoint can hand back the `gh` command instead of a 500.
    """
    import httpx

    from app.services.pr_service import create_pr_via_api

    class UnreachableClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setattr(httpx, "AsyncClient", UnreachableClient)

    with pytest.raises(RuntimeError, match="could not reach"):
        await create_pr_via_api("o/r", "head", "main", "title")
