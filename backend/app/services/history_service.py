"""
history_service.py — Time-machine git history per indexed file ($0, git CLI).

Indexed sources for GitHub repos are stable IDs: "{repo_url}::{rel_path}".
For each repo we keep a bare mirror under chroma_data/mirrors/<slug> so
`git log --follow` and `git blame` work without re-cloning:

  - Ingestion seeds/refreshes the mirror from its temp clone (local, fast).
  - Reads refresh from upstream best-effort (10s timeout, never fatal).

All git calls use the argv-list form (no shell). Uploaded files have no
upstream repo, so history endpoints 404 for them with a clear message.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from app.core.config import get_settings

settings = get_settings()
_LOCK = threading.Lock()
_GIT_TIMEOUT = 15


# ── Mirrors ───────────────────────────────────────────────────────────────────

def _mirrors_root() -> Path:
    p = Path(settings.chroma_persist_directory) / "mirrors"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def mirror_slug(repo_url: str) -> str:
    digest = hashlib.sha256(repo_url.encode()).hexdigest()[:16]
    tail = (repo_url.rstrip("/").rsplit("/", 1)[-1] or "repo")[:40]
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in tail)
    return f"{safe}-{digest}"


def mirror_path(repo_url: str) -> Path:
    return _mirrors_root() / mirror_slug(repo_url)


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError("git binary not available")
    return subprocess.run(
        [git_bin, *args], capture_output=True, text=True,
        timeout=_GIT_TIMEOUT, cwd=str(cwd) if cwd else None,
    )


def seed_mirror_from_tmp(repo_url: str, tmp_dir: str) -> str | None:
    """Clone the temp ingest dir into the bare mirror (local, no network).

    Called by ingestion after a successful clone. Returns the mirror path
    or None on any failure (best-effort — never breaks ingestion).
    """
    try:
        url = (repo_url or "").strip()
        if not url or not Path(tmp_dir, ".git").exists():
            return None
        dest = mirror_path(url)
        with _LOCK:
            if dest.exists():
                # Refresh existing mirror from the fresh clone (local fetch).
                proc = _git("--git-dir", str(dest), "fetch", tmp_dir,
                            "+refs/heads/*:refs/heads/*")
                if proc.returncode != 0:
                    return None
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                proc = _git("clone", "--mirror", "-q", tmp_dir, str(dest))
                if proc.returncode != 0:
                    shutil.rmtree(dest, ignore_errors=True)
                    return None
        return str(dest)
    except Exception:
        return None


def ensure_mirror(repo_url: str) -> Path | None:
    """Return a usable mirror, cloning/updating from upstream if needed."""
    url = (repo_url or "").strip()
    if not url or url == "uploaded_files":
        return None
    dest = mirror_path(url)
    with _LOCK:
        if dest.exists():
            # Best-effort upstream refresh — stale mirror is still usable.
            try:
                _git("--git-dir", str(dest), "remote", "update", "--prune")
            except Exception:
                pass
            return dest
        if not (url.startswith("https://") or url.startswith("git@")):
            return None
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            proc = _git("clone", "--mirror", "-q", url, str(dest))
            if proc.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                return None
            return dest
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            return None


# ── Path resolution ───────────────────────────────────────────────────────────

def split_source(source: str) -> tuple[str | None, str]:
    """Split an indexed source id into (repo_url, rel_path).

    GitHub sources are "{repo_url}::{rel_path}". Anything else (uploads,
    absolute tmp paths) returns (None, basename) — no git history available.
    """
    src = (source or "").strip()
    if "::" in src:
        url, rel = src.split("::", 1)
        if url and rel and url != "uploaded_files":
            return url, rel.strip("/")
    return None, (src.rsplit("/", 1)[-1] if src else "")


def resolve_path(mirror: Path, rel_path: str) -> str | None:
    """Resolve a repo-relative path inside the mirror (exact → suffix → basename)."""
    rel = (rel_path or "").strip().strip("/")
    if not rel:
        return None
    try:
        # ls-tree (not ls-files): bare mirrors have no index/working tree,
        # so ls-files always returns empty. ls-tree reads the HEAD tree.
        proc = _git("--git-dir", str(mirror), "ls-tree", "-r", "--name-only", "HEAD")
        if proc.returncode != 0:
            return None
        tracked = proc.stdout.splitlines()
        if rel in tracked:
            return rel
        matches = [t for t in tracked if t.endswith("/" + rel) or t == rel]
        if matches:
            return sorted(matches, key=len)[0]
        base = rel.rsplit("/", 1)[-1]
        matches = [t for t in tracked if t.rsplit("/", 1)[-1] == base]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return sorted(matches, key=len)[0]
    except Exception:
        pass
    return None


# ── Timeline + blame ──────────────────────────────────────────────────────────

def file_timeline(repo_url: str, rel_path: str, limit: int = 30) -> dict[str, Any]:
    """Commit history for one file (newest first). Raises RuntimeError when unavailable."""
    mirror = ensure_mirror(repo_url)
    if not mirror:
        raise RuntimeError("No git history available for this repo (uploads have no upstream).")
    resolved = resolve_path(mirror, rel_path)
    if not resolved:
        raise RuntimeError(f"File not found in repo history: {rel_path}")
    fmt = "%H%x1f%an%x1f%ad%x1f%s"
    proc = _git("--git-dir", str(mirror), "log", "--follow",
                f"--format={fmt}", "--date=iso", "-n", str(max(1, min(limit, 100))),
                "--", resolved)
    if proc.returncode != 0:
        raise RuntimeError("git log failed for this file.")
    commits = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, author, date, message = parts
        commits.append({
            "sha": sha, "short_sha": sha[:7],
            "author": author, "date": date, "message": message,
        })
    return {"repo_url": repo_url, "path": resolved, "commits": commits,
            "total": len(commits)}


def file_blame(repo_url: str, rel_path: str, rev: str = "HEAD") -> dict[str, Any]:
    """Line-by-line blame: which commit last touched each line.

    rev must be a 40-hex sha or HEAD (validated — no arbitrary rev parsing).
    """
    rev = (rev or "HEAD").strip()
    if rev != "HEAD" and not (len(rev) == 40 and all(c in "0123456789abcdef" for c in rev)):
        raise RuntimeError("Invalid revision (must be HEAD or a full commit sha).")
    mirror = ensure_mirror(repo_url)
    if not mirror:
        raise RuntimeError("No git history available for this repo (uploads have no upstream).")
    resolved = resolve_path(mirror, rel_path)
    if not resolved:
        raise RuntimeError(f"File not found in repo history: {rel_path}")
    # NOTE: no --line-number flag exists for blame; we number lines sequentially.
    proc = _git("--git-dir", str(mirror), "blame", "--porcelain",
                rev, "--", resolved)
    if proc.returncode != 0:
        raise RuntimeError("git blame failed for this file.")
    lines: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for raw in proc.stdout.splitlines():
        if raw.startswith("\t"):
            lines.append({
                "line_no": len(lines) + 1,
                "sha": cur.get("sha", ""),
                "short_sha": cur.get("sha", "")[:7],
                "author": cur.get("author", ""),
                "date": cur.get("author-time", ""),
                "content": raw[1:],
            })
            cur = {}
        elif raw.startswith("author "):
            cur["author"] = raw[len("author "):]
        elif raw.startswith("author-time "):
            try:
                import datetime
                cur["author-time"] = datetime.datetime.fromtimestamp(
                    int(raw[len("author-time "):])).strftime("%Y-%m-%d")
            except Exception:
                cur["author-time"] = ""
        elif len(raw) >= 40 and all(c in "0123456789abcdef " for c in raw[:41]):
            cur["sha"] = raw[:40]
    return {"repo_url": repo_url, "path": resolved, "rev": rev,
            "lines": lines, "total_lines": len(lines)}
