"""
pr_service.py — Create-PR helper ($0 without a token, live with one).

Two modes:
  1. GITHUB_TOKEN set → creates the PR via the GitHub REST API (httpx).
  2. No token         → returns a ready-to-paste `gh pr create` command plus
     the unified diff as a downloadable patch (deterministic, offline-safe).

Repo refs accept "owner/name" or any https://github.com/owner/name... URL.
"""

from __future__ import annotations

import os
import re
import shlex

_API = "https://api.github.com"


def parse_repo_ref(repo: str) -> str:
    """Normalize a repo ref to 'owner/name'. Raises ValueError when invalid."""
    ref = (repo or "").strip().rstrip("/")
    if ref.startswith("https://"):
        m = re.match(r"https?://github\.com/([^/]+)/([^/\s?#]+)", ref)
        if not m:
            raise ValueError("Only github.com repo URLs are supported")
        owner, name = m.group(1), re.sub(r"\.git$", "", m.group(2))
    elif ref.startswith("git@"):
        m = re.match(r"git@github\.com:([^/]+)/([^/\s?#]+)", ref)
        if not m:
            raise ValueError("Only github.com repo URLs are supported")
        owner, name = m.group(1), re.sub(r"\.git$", "", m.group(2))
    else:
        parts = ref.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repo must be 'owner/name' or a github.com URL")
        owner, name = parts
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid owner/name characters")
    return f"{owner}/{name}"


def build_gh_command(repo_slug: str, head: str, base: str, title: str, body: str = "") -> str:
    """Shell-safe `gh pr create` command for the manual (no-token) path."""
    parts = ["gh", "pr", "create", "--repo", repo_slug,
             "--head", head, "--base", base, "--title", title]
    if body.strip():
        parts += ["--body", body]
    return " ".join(shlex.quote(p) for p in parts)


def validate_branches(head: str, base: str) -> tuple[str, str]:
    head, base = (head or "").strip(), (base or "").strip() or "main"
    for label, value in (("head", head), ("base", base)):
        if not value or len(value) > 100:
            raise ValueError(f"{label} branch is required (max 100 chars)")
        if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", value):
            raise ValueError(f"{label} branch has invalid characters")
        if ".." in value or value.startswith(("/", "-", ".")) or value.endswith(("/", ".lock")):
            raise ValueError(f"{label} branch looks invalid")
    if head == base:
        raise ValueError("head and base must differ")
    return head, base


async def create_pr_via_api(repo_slug: str, head: str, base: str,
                            title: str, body: str = "") -> dict:
    """
    Create a PR through api.github.com. Raises RuntimeError on failure.

    Every failure — HTTP status, DNS, TLS, timeout — surfaces as RuntimeError, so
    callers have exactly one thing to handle and can fall back to the `gh` command.
    A transport error used to escape as an unhandled httpx exception and turn the
    endpoint into a 500 with an empty body: the one moment the manual plan is most
    useful was the one moment it was unavailable.

    WHY THIS DELEGATES INSTEAD OF POSTING
    --------------------------------------
    This used to open its own `httpx` client and read its credential from
    `os.getenv("GITHUB_TOKEN")`. That made it a second GitHub integration, and it
    was the one the product's own Connect button could not reach: a user who
    pasted a token into SavFlux was told "GITHUB_TOKEN not configured" here,
    while a machine that *did* export the variable had its pull requests opened
    as that machine's account rather than the one connected in the UI. That is
    precisely the failure `github_service.get_token()` exists to prevent,
    reintroduced a module away from where it had been fixed.

    So the POST, the credential and the error classification live in one place.
    `GitHubError` already subclasses `RuntimeError`, and its `kind` carries the
    distinction callers act on — "auth" means fix the token, "rate_limit" means
    wait — which a bare status code did not.
    """
    from app.services import github_service

    if not github_service.get_token():
        raise RuntimeError(
            "GitHub is not connected. Connect an account in SavFlux, or run the "
            "command below with the gh CLI."
        )
    created = await github_service.create_pull(
        repo_slug, title=title, head=head, base=base, body=body
    )
    return {"number": created.get("number"), "url": created.get("url"),
            "state": created.get("state")}
