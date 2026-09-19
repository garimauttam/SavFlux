"""
test_pr_security.py — Unit tests for Create-PR helpers, CVE parsers, prune.
All pure logic: no network, no ChromaDB.
"""

import pytest


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
