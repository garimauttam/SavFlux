from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import worktree_service


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def git_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "SavFlux Test", cwd=repo)
    _git("config", "user.email", "test@savflux.local", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "indexed base", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo)

    mirror = tmp_path / "mirror.git"
    _git("clone", "--mirror", str(repo), str(mirror))
    data = tmp_path / "savflux-data"
    data.mkdir()

    import app.core.paths as paths
    from app.services import history_service, trust_service

    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(history_service, "mirror_path", lambda _url: mirror)
    monkeypatch.setattr(trust_service, "get_entry", lambda _url, check_upstream=False: {"indexed_sha": sha})
    return {"repo": repo, "mirror": mirror, "data": data, "sha": sha, "url": "https://github.com/acme/widgets"}


def test_creates_real_local_branch_and_persists_agent_edits(git_workspace):
    url = git_workspace["url"]
    info = worktree_service.ensure_worktree(url)
    root = info["path"]

    assert info["exists"] is True
    assert info["indexed_sha"] == git_workspace["sha"]
    assert info["branch"].startswith("savflux/agent-")
    assert _git("-C", str(root), "branch", "--show-current") == info["branch"]

    status = worktree_service.apply_changes(url, [{
        "path": "src/module.py",
        "original": "value = 1\n",
        "content": "value = 2\n",
    }])
    assert status["persisted"] is True
    assert status["dirty"] is True
    assert status["changed_file_count"] == 1
    assert "-value = 1" in status["diff"] and "+value = 2" in status["diff"]

    again = worktree_service.workspace_status(url)
    assert again["dirty"] is True
    assert again["branch"] == info["branch"]


def test_workspace_conflict_and_reset_are_explicit(git_workspace):
    url = git_workspace["url"]
    worktree_service.apply_changes(url, [{
        "path": "src/module.py", "original": "value = 1\n", "content": "value = 3\n",
    }])
    with pytest.raises(RuntimeError, match="no longer matches"):
        worktree_service.apply_changes(url, [{
            "path": "src/module.py", "original": "value = 1\n", "content": "value = 4\n",
        }])

    clean = worktree_service.reset_workspace(url)
    assert clean["reset"] is True
    assert clean["dirty"] is False
    assert (clean["path"] is None)


def test_reindex_marks_workspace_stale_and_explicit_reset_advances_its_base(git_workspace, monkeypatch):
    url = git_workspace["url"]
    worktree_service.apply_changes(url, [{
        "path": "src/module.py", "original": "value = 1\n", "content": "value = 2\n",
    }])

    source = git_workspace["repo"]
    (source / "src" / "module.py").write_text("value = 10\n", encoding="utf-8")
    _git("add", ".", cwd=source)
    _git("commit", "-m", "new indexed version", cwd=source)
    new_sha = _git("rev-parse", "HEAD", cwd=source)
    _git("--git-dir", str(git_workspace["mirror"]), "fetch", "--all")

    from app.services import trust_service
    monkeypatch.setattr(trust_service, "get_entry", lambda _url, check_upstream=False: {"indexed_sha": new_sha})

    stale = worktree_service.workspace_status(url)
    assert stale["stale_index"] is True
    assert stale["dirty"] is True
    with pytest.raises(RuntimeError, match="older index"):
        worktree_service.apply_changes(url, [{
            "path": "src/module.py", "original": "value = 1\n", "content": "value = 3\n",
        }])

    reset = worktree_service.reset_workspace(url)
    assert reset["base_sha"] == new_sha
    assert reset["indexed_sha"] == new_sha
    assert reset["stale_index"] is False
    assert reset["dirty"] is False
    assert (reset["path"] is None)


def test_new_files_are_included_in_local_diff(git_workspace):
    status = worktree_service.apply_changes(git_workspace["url"], [{
        "path": "src/new.py", "original": None, "content": "created = True\n",
    }])
    assert status["dirty"] is True
    assert {item["status"] for item in status["files_changed"]} == {"A"}
    assert "+created = True" in status["diff"]


def test_workspace_status_api_is_authenticated_and_explains_empty_state(client, isolated_data_dir):
    repo = "https://github.com/acme/not-created"
    response = client.get("/api/v1/workspace/status", params={"repo": repo})
    assert response.status_code == 200
    assert response.json()["exists"] is False
    assert "Build a patch in Agent mode" in response.json()["message"]

    denied = client.get(
        "/api/v1/workspace/status",
        params={"repo": repo},
        headers={"Authorization": "Bearer invalid-test-access-token"},
    )
    assert denied.status_code == 401


def test_paths_cannot_escape_through_traversal_or_symlinks(git_workspace):
    url = git_workspace["url"]
    with pytest.raises(ValueError, match="unsafe|outside"):
        worktree_service.apply_changes(url, [{
            "path": "../../outside.txt", "original": None, "content": "nope",
        }])

    root = worktree_service.ensure_worktree(url)["path"]
    outside = git_workspace["data"].parent / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        worktree_service.apply_changes(url, [{
            "path": "escape", "original": None, "content": "overwrite",
        }])
    assert outside.read_text(encoding="utf-8") == "keep"
