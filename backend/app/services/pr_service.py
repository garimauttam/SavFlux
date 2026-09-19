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
    """Create a PR through api.github.com. Raises RuntimeError on failure."""
    import httpx
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_API}/repos/{repo_slug}/pulls",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"title": title, "head": head, "base": base, "body": body or ""},
        )
    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"GitHub API {resp.status_code}: {detail}")
    data = resp.json()
    return {"number": data.get("number"), "url": data.get("html_url"),
            "state": data.get("state")}
