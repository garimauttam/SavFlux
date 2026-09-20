"""
test_history.py — Unit tests for the time-machine history service.

Builds a real temp git repo (2 commits) and seeds a mirror from it —
no network, no ChromaDB. Skipped when the git binary is unavailable.
"""

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")


@pytest.fixture()
def _repo(tmp_path, isolated_data_dir):
    # isolated_data_dir redirects the git-mirror root into tmp (see conftest).
    src = tmp_path / "src"
    src.mkdir()
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.t"}

    def git(*args):
        subprocess.run(["git", *args], cwd=src, check=True,
                       capture_output=True, env={**dict(__import__("os").environ), **env})

    git("init", "-q")
    (src / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "first")
    (src / "app.py").write_text("x = 1\ny = 2\n")
    git("commit", "-qam", "second")
    return src


def test_split_source():
    from app.services.history_service import split_source
    assert split_source("https://github.com/x/y::pkg/app.py") == ("https://github.com/x/y", "pkg/app.py")
    assert split_source("/tmp/abc/app.py") == (None, "app.py")
    assert split_source("") == (None, "")


def test_timeline_and_blame_from_seeded_mirror(_repo):
    from app.services.history_service import (
        seed_mirror_from_tmp, file_timeline, file_blame, resolve_path, mirror_path,
    )
    url = "https://github.com/test/demo"
    assert seed_mirror_from_tmp(url, str(_repo)) is not None
    assert resolve_path(mirror_path(url), "app.py") == "app.py"

    tl = file_timeline(url, "app.py")
    assert tl["total"] == 2
    assert tl["commits"][0]["message"] == "second"
    assert len(tl["commits"][0]["sha"]) == 40

    blame = file_blame(url, "app.py")
    assert blame["total_lines"] == 2
    assert blame["lines"][0]["content"] == "x = 1"
    assert blame["lines"][1]["sha"] == tl["commits"][0]["sha"]  # y=2 came from "second"


def test_blame_rejects_bad_rev(_repo):
    from app.services.history_service import seed_mirror_from_tmp, file_blame
    url = "https://github.com/test/demo"
    seed_mirror_from_tmp(url, str(_repo))
    with pytest.raises(RuntimeError):
        file_blame(url, "app.py", rev="HEAD~1; rm -rf /")


def test_uploads_have_no_history(isolated_data_dir):
    from app.services.history_service import ensure_mirror
    assert ensure_mirror("uploaded_files") is None
