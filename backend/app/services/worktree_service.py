"""Persistent, isolated Agent worktrees for indexed GitHub repositories.

A GitHub ingest mirror already stores the exact commit used to build the index.
This service creates a separate local branch/worktree at that commit and applies
reviewed Agent patch proposals there. It never pushes or commits; the existing
digest-bound PR path remains the only route that writes to GitHub.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.core.paths import data_file
from app.services.patch_service import PatchError, normalise_path

_GIT_TIMEOUT = 30
_MAX_FILES = 100
_MAX_TOTAL_CHARS = 3_000_000


def _git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError("Git is not installed on the SavFlux server.")
    try:
        return subprocess.run(
            [git_bin, *args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The local Git workspace operation timed out.") from exc


def _canonical_repo(repo_url: str) -> tuple[str, str]:
    from app.services.pr_service import parse_repo_ref

    try:
        slug = parse_repo_ref(repo_url)
    except ValueError as exc:
        raise ValueError("Use a GitHub repository URL or owner/name to create an Agent workspace.") from exc
    return f"https://github.com/{slug}", slug


def _workspace_root(repo_url: str) -> Path:
    canonical, slug = _canonical_repo(repo_url)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    name = slug.split("/")[-1]
    parent = data_file("agent_worktrees")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    return parent / f"{name}-{digest}" / "worktree"


def _workspace_branch(repo_url: str) -> str:
    canonical, _ = _canonical_repo(repo_url)
    return "savflux/agent-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _indexed_commit(repo_url: str) -> str:
    from app.services.trust_service import get_entry

    entry = get_entry(repo_url, check_upstream=False)
    sha = (entry or {}).get("indexed_sha")
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError("No indexed Git commit is recorded for this repository. Re-index it before creating an Agent workspace.")
    return sha


def _mirror(repo_url: str) -> Path:
    from app.services.history_service import mirror_path

    canonical, _ = _canonical_repo(repo_url)
    mirror = mirror_path(canonical)
    if not mirror.exists():
        raise RuntimeError("No local Git mirror is available. Re-index this GitHub repository before creating a workspace.")
    return mirror


def _verify_existing_worktree(root: Path, mirror: Path) -> None:
    if not root.exists():
        return
    if not (root / ".git").exists():
        raise RuntimeError("A non-Git folder exists at the Agent workspace path; refusing to overwrite it.")
    common = _git(["-C", str(root), "rev-parse", "--git-common-dir"])
    if common.returncode != 0:
        raise RuntimeError("The saved Agent workspace is not a valid Git worktree.")
    common_path = Path(common.stdout.strip())
    if not common_path.is_absolute():
        common_path = (root / common_path).resolve()
    if common_path.resolve() != mirror.resolve():
        raise RuntimeError("The saved Agent workspace points at a different repository; refusing to reuse it.")


def ensure_worktree(repo_url: str) -> dict[str, Any]:
    """Create or return the persistent worktree at the exact indexed commit."""
    canonical, _ = _canonical_repo(repo_url)
    mirror = _mirror(canonical)
    sha = _indexed_commit(canonical)
    check = _git(["--git-dir", str(mirror), "cat-file", "-e", f"{sha}^{{commit}}"])
    if check.returncode != 0:
        raise RuntimeError("The indexed commit is missing from the local Git mirror. Re-index the repository.")

    root = _workspace_root(canonical)
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.parent.chmod(0o700)
    except OSError:
        pass
    _verify_existing_worktree(root, mirror)

    if not root.exists():
        branch = _workspace_branch(canonical)
        created = _git([
            "--git-dir", str(mirror), "worktree", "add", "-B", branch,
            str(root), sha,
        ])
        if created.returncode != 0:
            raise RuntimeError("Could not create the Agent worktree: " + (created.stderr.strip() or "Git worktree add failed."))
        try:
            root.parent.chmod(0o700)
            root.chmod(0o700)
        except OSError:
            pass

    head = _git(["-C", str(root), "rev-parse", "HEAD"])
    if head.returncode != 0:
        raise RuntimeError("Could not read the Agent worktree's base commit.")
    return {
        "exists": True,
        "branch": _workspace_branch(canonical),
        "base_sha": head.stdout.strip(),
        "indexed_sha": sha,
        "path": root,
    }


def _safe_target(root: Path, relative: str) -> Path:
    try:
        cleaned = normalise_path(relative)
    except PatchError as exc:
        raise ValueError(str(exc)) from exc
    target = root / cleaned
    cursor = root
    for part in Path(cleaned).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Refusing to write through a symlink in the Agent workspace: {cleaned}")
    resolved_parent = target.parent.resolve()
    if resolved_parent != root.resolve() and root.resolve() not in resolved_parent.parents:
        raise ValueError("The path resolves outside the Agent workspace.")
    return target


def _current_file(root: Path, rel_path: str) -> str | None:
    target = _safe_target(root, rel_path)
    if not target.exists():
        return None
    if not target.is_file():
        raise ValueError(f"The Agent workspace target is not a regular file: {rel_path}")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"The Agent workspace file is not UTF-8 text: {rel_path}") from exc


def _git_diff(root: Path) -> dict[str, Any]:
    names = _git(["-C", str(root), "diff", "--name-status", "HEAD", "--no-color"])
    patch = _git(["-C", str(root), "diff", "--no-ext-diff", "--no-color", "--unified=3", "HEAD"])
    stats = _git(["-C", str(root), "diff", "--stat", "HEAD"])
    if any(proc.returncode != 0 for proc in (names, patch, stats)):
        raise RuntimeError("Could not read the local Agent diff from Git.")
    changed = []
    for row in names.stdout.splitlines():
        status, _, path = row.partition("\t")
        changed.append({"status": status, "path": path})
    diff_text = patch.stdout
    truncated = len(diff_text) > 200_000
    if truncated:
        diff_text = diff_text[:200_000] + "\n… local diff truncated at 200 kB …\n"
    return {
        "dirty": bool(changed),
        "files_changed": changed,
        "changed_file_count": len(changed),
        "stat": stats.stdout.strip(),
        "diff": diff_text,
        "truncated": truncated,
    }


def workspace_status(repo_url: str) -> dict[str, Any]:
    canonical, _ = _canonical_repo(repo_url)
    root = _workspace_root(canonical)
    if not root.exists():
        return {
            "exists": False,
            "dirty": False,
            "repo_url": canonical,
            "message": "No Agent worktree exists yet. Build a patch in Agent mode to create one.",
        }
    info = ensure_worktree(canonical)
    diff = _git_diff(root)
    return {
        **info,
        **diff,
        "stale_index": info.get("base_sha") != info.get("indexed_sha"),
        "repo_url": canonical,
        "path": None,
    }


def apply_changes(repo_url: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply an already-built Agent proposal after checking it against the indexed base."""
    if not changes:
        raise ValueError("No Agent changes were supplied.")
    if len(changes) > _MAX_FILES:
        raise ValueError(f"An Agent workspace patch may change at most {_MAX_FILES} files.")
    total = sum(len(str((change or {}).get("content") or "")) for change in changes)
    if total > _MAX_TOTAL_CHARS:
        raise ValueError("The Agent workspace patch is too large (3 MB text limit).")

    info = ensure_worktree(repo_url)
    if info.get("base_sha") != info.get("indexed_sha"):
        raise RuntimeError("The Agent worktree is based on an older index. Reset it to the latest indexed commit before applying another proposal.")
    root: Path = info["path"]
    prepared: list[tuple[Path, str, str | None, str]] = []
    for change in changes:
        change = change or {}
        relative = normalise_path(str(change.get("path") or ""))
        current = _current_file(root, relative)
        original = change.get("original")
        if original is not None and current != original:
            raise RuntimeError(f"Workspace conflict for {relative}: it no longer matches the indexed base. Reset or review the existing local change first.")
        if original is None and current is not None:
            raise RuntimeError(f"Workspace conflict for {relative}: a file already exists locally but is not present in the indexed base.")
        target = _safe_target(root, relative)
        content = str(change.get("content") or "")
        prepared.append((target, relative, current, content))

    written: list[tuple[Path, str | None]] = []
    try:
        for target, relative, current, content in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after directory creation: a pre-existing symlinked parent
            # is never allowed to redirect an LLM-generated path outside the repo.
            target = _safe_target(root, relative)
            target.write_text(content, encoding="utf-8")
            written.append((target, current))
            added = _git(["-C", str(root), "add", "-N", "--", relative])
            if added.returncode != 0:
                raise RuntimeError("Git could not register a new workspace file: " + added.stderr.strip())
    except Exception:
        for target, previous in reversed(written):
            relative = target.relative_to(root).as_posix()
            _git(["-C", str(root), "reset", "--", relative])
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_text(previous, encoding="utf-8")
            except OSError:
                pass
        raise

    return {**workspace_status(repo_url), "persisted": True}


def reset_workspace(repo_url: str) -> dict[str, Any]:
    """Discard only uncommitted changes inside SavFlux's isolated worktree."""
    canonical, _ = _canonical_repo(repo_url)
    root = _workspace_root(canonical)
    if not root.exists():
        return workspace_status(canonical)
    info = ensure_worktree(canonical)
    reset = _git(["-C", str(root), "reset", "--hard", info["indexed_sha"]])
    clean = _git(["-C", str(root), "clean", "-fd"])
    if reset.returncode != 0 or clean.returncode != 0:
        raise RuntimeError("Could not reset the Agent worktree safely.")
    return {**workspace_status(canonical), "reset": True, "branch": info["branch"]}
