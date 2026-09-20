"""
Tests for patch construction.

Two things matter here beyond "does it produce a diff":

  1. The patch must actually apply. A diff that looks right but that
     `git apply` rejects is worse than no diff, because the failure surfaces
     on the user's machine. Several tests shell out to real `git apply
     --check` in a scratch repo to prove the output is well-formed.

  2. Paths in a patch are file writes on whoever applies it, and this content
     can be LLM-generated. Traversal has to be rejected at construction time.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from app.services.patch_service import (
    DEFAULT_CONTEXT,
    FileChange,
    PatchError,
    build_file_diff,
    build_patch,
    build_pr_body,
    normalise_path,
    suggest_branch_name,
)

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _init_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a real git repo so patches can be validated with `git apply`."""
    subprocess.run([GIT, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([GIT, "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run([GIT, "config", "user.name", "Test"], cwd=tmp_path, check=True)
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run([GIT, "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run([GIT, "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _git_apply_check(repo: Path, diff: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [GIT, "apply", "--check", "-"],
        cwd=repo,
        input=diff,
        text=True,
        capture_output=True,
    )


# ── Path safety ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "../../.ssh/authorized_keys",
        "/etc/passwd",
        "a/../../../etc/shadow",
        ".git/config",
        "src/../.git/hooks/pre-commit",
        "C:/Windows/system32/drivers/etc/hosts",
        "~/.bashrc",
        "src/evil\x00.py",
    ],
)
def test_traversal_and_git_internal_paths_are_rejected(path):
    """
    A path in a diff header is a write target on the reviewer's machine, and
    this content can come from an LLM. Rejecting at construction means a bad
    path can never reach a patch file at all.
    """
    with pytest.raises(PatchError):
        normalise_path(path)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/auth.py", "src/auth.py"),
        ("./src/auth.py", "src/auth.py"),
        ("src\\windows\\path.py", "src/windows/path.py"),
        ("  spaced.py  ", "spaced.py"),
        ("deeply/nested/but/fine/mod.py", "deeply/nested/but/fine/mod.py"),
        ("file.with.dots.py", "file.with.dots.py"),
    ],
)
def test_legitimate_paths_are_accepted_and_normalised(path, expected):
    assert normalise_path(path) == expected


def test_empty_path_is_rejected():
    with pytest.raises(PatchError):
        normalise_path("   ")


def test_dotfile_at_root_is_allowed():
    """`.github/workflows/ci.yml` is ordinary; only `.git/` is off limits."""
    assert normalise_path(".github/workflows/ci.yml") == ".github/workflows/ci.yml"


# ── Diff correctness ──────────────────────────────────────────────────────────


def test_modification_produces_an_applicable_patch(tmp_path):
    original = "def add(a, b):\n    return a - b\n"
    repo = _init_repo(tmp_path, {"calc.py": original})

    result = build_patch([FileChange("calc.py", original, "def add(a, b):\n    return a + b\n")])
    check = _git_apply_check(repo, result.diff)

    assert check.returncode == 0, f"git rejected the patch: {check.stderr}"
    assert result.files_changed == 1
    assert result.additions == 1
    assert result.deletions == 1


@requires_git
def test_new_file_patch_applies(tmp_path):
    repo = _init_repo(tmp_path, {"existing.py": "x = 1\n"})
    result = build_patch([FileChange("brand/new.py", None, "def hello():\n    return 'hi'\n")])

    assert "new file mode" in result.diff
    check = _git_apply_check(repo, result.diff)
    assert check.returncode == 0, check.stderr
    assert result.file_summaries[0]["status"] == "added"
    assert result.deletions == 0


@requires_git
def test_deletion_patch_applies(tmp_path):
    content = "obsolete = True\n"
    repo = _init_repo(tmp_path, {"old.py": content})
    result = build_patch([FileChange("old.py", content, None)])

    assert "deleted file mode" in result.diff
    check = _git_apply_check(repo, result.diff)
    assert check.returncode == 0, check.stderr
    assert result.additions == 0


@requires_git
def test_multi_file_patch_applies_as_one_unit(tmp_path):
    a_before, b_before = "a = 1\n", "b = 2\n"
    repo = _init_repo(tmp_path, {"a.py": a_before, "b.py": b_before})

    result = build_patch(
        [
            FileChange("a.py", a_before, "a = 10\n"),
            FileChange("b.py", b_before, "b = 20\n"),
            FileChange("c.py", None, "c = 30\n"),
        ]
    )

    assert result.files_changed == 3
    check = _git_apply_check(repo, result.diff)
    assert check.returncode == 0, check.stderr


@requires_git
def test_file_without_trailing_newline_round_trips(tmp_path):
    """
    A missing final newline is the classic malformed-patch case: without git's
    "\\ No newline at end of file" marker the last line silently merges.
    """
    original = "line one\nline two"  # no trailing newline
    repo = _init_repo(tmp_path, {"nonewline.txt": original})

    result = build_patch([FileChange("nonewline.txt", original, "line one\nline two changed")])
    assert "\\ No newline at end of file" in result.diff
    check = _git_apply_check(repo, result.diff)
    assert check.returncode == 0, check.stderr


@requires_git
def test_unicode_content_survives_the_patch(tmp_path):
    original = "greeting = 'hello'\n"
    repo = _init_repo(tmp_path, {"i18n.py": original})
    result = build_patch([FileChange("i18n.py", original, "greeting = 'héllo 🌍 日本語'\n")])

    assert "🌍" in result.diff
    check = _git_apply_check(repo, result.diff)
    assert check.returncode == 0, check.stderr


def test_identical_content_is_not_emitted_as_an_empty_section():
    """
    A PR that lists files with no changes wastes reviewer attention, which is
    the scarcest resource in the pipeline.
    """
    same = "unchanged = True\n"
    with pytest.raises(PatchError, match="identical"):
        build_patch([FileChange("same.py", same, same)])


def test_unchanged_file_is_dropped_but_others_survive():
    same = "unchanged = True\n"
    result = build_patch(
        [
            FileChange("same.py", same, same),
            FileChange("changed.py", "x = 1\n", "x = 2\n"),
        ]
    )
    assert result.files_changed == 1
    assert result.file_summaries[0]["path"] == "changed.py"


def test_duplicate_paths_are_rejected():
    """
    Two sections for one file produce a patch whose second hunk cannot apply,
    and the failure would surface on the user's machine rather than here.
    """
    with pytest.raises(PatchError, match="duplicate"):
        build_patch(
            [
                FileChange("dup.py", "a\n", "b\n"),
                FileChange("dup.py", "a\n", "c\n"),
            ]
        )


def test_change_with_no_content_on_either_side_is_rejected():
    with pytest.raises(PatchError):
        build_file_diff(FileChange("ghost.py", None, None))


def test_empty_change_set_is_rejected():
    with pytest.raises(PatchError, match="no changes"):
        build_patch([])


def test_oversized_patch_is_refused():
    """A runaway diff is far more likely an LLM bug than a real change."""
    huge = "\n".join(f"line {i}" for i in range(200_000))
    with pytest.raises(PatchError, match="KB limit"):
        build_patch([FileChange("huge.py", "", huge)])


# ── Line accounting ───────────────────────────────────────────────────────────


def test_additions_and_deletions_are_counted_per_file():
    original = "a\nb\nc\n"
    modified = "a\nB\nc\nd\n"  # one replace, one insert
    result = build_patch([FileChange("counts.py", original, modified)])
    summary = result.file_summaries[0]
    assert summary["additions"] == 2
    assert summary["deletions"] == 1
    assert result.additions == 2
    assert result.deletions == 1


def test_new_file_counts_every_line_as_an_addition():
    change = FileChange("new.py", None, "one\ntwo\nthree\n")
    assert change.added_lines == 3
    assert change.removed_lines == 0


def test_deleted_file_counts_every_line_as_a_removal():
    change = FileChange("gone.py", "one\ntwo\n", None)
    assert change.added_lines == 0
    assert change.removed_lines == 2


def test_context_lines_are_configurable():
    original = "\n".join(f"line{i}" for i in range(20)) + "\n"
    modified = original.replace("line10", "CHANGED")

    tight = build_patch([FileChange("f.py", original, modified)], context=1)
    wide = build_patch([FileChange("f.py", original, modified)], context=8)
    assert len(wide.diff) > len(tight.diff)


# ── Digest ────────────────────────────────────────────────────────────────────


def test_digest_is_stable_for_identical_input():
    change = lambda: [FileChange("f.py", "a\n", "b\n")]  # noqa: E731
    assert build_patch(change()).digest == build_patch(change()).digest


def test_digest_changes_when_the_diff_changes():
    """
    The digest guards against applying a preview the user is no longer looking
    at, so it has to move when the content does.
    """
    first = build_patch([FileChange("f.py", "a\n", "b\n")])
    second = build_patch([FileChange("f.py", "a\n", "c\n")])
    assert first.digest != second.digest


# ── Branch naming ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        "Fix SQL injection in auth.py",
        "feat: add rate limiting!!!",
        "Ünïcödé tïtlé with émojis 🎉",
        "   ",
        "a" * 200,
        "../../escape",
        "branch;rm -rf /",
    ],
)
def test_suggested_branch_names_are_always_valid_git_refs(title):
    """
    git ref rules forbid a specific set of characters and sequences. Using an
    allow-list rather than enumerating them means a new hostile input cannot
    slip through.
    """
    name = suggest_branch_name(title)
    assert name.startswith("savflux/")
    assert ".." not in name
    assert not name.endswith((".", "/", ".lock"))
    assert all(c.isalnum() or c in "-/" for c in name), f"invalid characters in {name!r}"
    if GIT:
        check = subprocess.run(
            [GIT, "check-ref-format", "--branch", name], capture_output=True
        )
        assert check.returncode == 0, f"git rejected branch name {name!r}"


def test_branch_names_are_deterministic_within_a_day():
    assert suggest_branch_name("Fix the bug") == suggest_branch_name("Fix the bug")


# ── PR body ───────────────────────────────────────────────────────────────────


def test_pr_body_leads_with_the_summary_then_the_file_table():
    result = build_patch([FileChange("auth.py", "x = 1\n", "x = 2\n")])
    body = build_pr_body("Fixes the login bypass.", result)

    assert body.index("Fixes the login bypass.") < body.index("### Changes")
    assert "| `auth.py` | modified |" in body
    assert "1 file(s) changed" in body


def test_pr_body_lists_the_findings_it_addresses():
    result = build_patch([FileChange("db.py", "x = 1\n", "x = 2\n")])
    body = build_pr_body(
        "Parameterises the query.",
        result,
        findings=[{"rule_id": "PY-SEC-SQLI", "title": "SQL injection", "line": 42}],
    )
    assert "### Issues addressed" in body
    assert "SQL injection" in body
    assert "`L42`" in body
    assert "`PY-SEC-SQLI`" in body


def test_pr_body_omits_the_diff_by_default():
    """
    Inlining a large diff pushes the review discussion below the fold, so it
    has to be opt-in.
    """
    result = build_patch([FileChange("f.py", "a\n", "b\n")])
    assert "```diff" not in build_pr_body("Summary.", result)
    assert "```diff" in build_pr_body("Summary.", result, include_diff=True)


def test_pr_body_handles_a_missing_summary():
    result = build_patch([FileChange("f.py", "a\n", "b\n")])
    assert "_No summary provided._" in build_pr_body("", result)


def test_pr_body_marks_itself_as_machine_generated():
    """A human reviewer must be able to tell at a glance. Never imply otherwise."""
    result = build_patch([FileChange("f.py", "a\n", "b\n")])
    body = build_pr_body("Summary.", result)
    assert "🤖" in body
    assert "review before merging" in body.lower()


# ── Realistic end-to-end shape ────────────────────────────────────────────────


@requires_git
def test_fixing_a_real_finding_produces_an_applicable_patch(tmp_path):
    """
    The whole point of the feature: the analyzer finds a SQL injection, the
    agent proposes the parameterised form, and the result is a patch a
    maintainer can apply without touching an editor.
    """
    vulnerable = textwrap.dedent(
        """\
        def get_user(conn, user_id):
            query = "SELECT * FROM users WHERE id = " + str(user_id)
            return conn.execute(query).fetchone()
        """
    )
    fixed = textwrap.dedent(
        """\
        def get_user(conn, user_id):
            return conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        """
    )
    repo = _init_repo(tmp_path, {"db.py": vulnerable})

    result = build_patch([FileChange("db.py", vulnerable, fixed)])
    assert _git_apply_check(repo, result.diff).returncode == 0

    # And it really applies, not just passes --check.
    subprocess.run([GIT, "apply", "-"], cwd=repo, input=result.diff, text=True, check=True)
    assert "?" in (repo / "db.py").read_text()
    assert '+ str(user_id)' not in (repo / "db.py").read_text()


# ── API surface ───────────────────────────────────────────────────────────────
#
# The endpoint is what turns the patch builder into the missing half of
# "create a PR": callers supply new file contents, not a diff they had to
# produce themselves.


def test_build_patch_endpoint_returns_an_applicable_diff(client):
    response = client.post(
        "/api/v1/review/build-patch",
        json={
            "changes": [
                {"path": "db.py", "original": "x = 1\n", "content": "x = 2\n"}
            ],
            "title": "Fix the constant",
            "summary": "Bumps x.",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["files_changed"] == 1
    assert data["additions"] == 1
    assert data["deletions"] == 1
    assert data["diff"].startswith("diff --git a/db.py b/db.py")
    assert data["suggested_branch"].startswith("savflux/fix-the-constant")
    assert "Bumps x." in data["pr_body"]
    assert data["digest"]


def test_build_patch_endpoint_rejects_path_traversal(client):
    """The endpoint is the boundary where untrusted paths arrive."""
    response = client.post(
        "/api/v1/review/build-patch",
        json={"changes": [{"path": "../../.ssh/authorized_keys", "content": "key"}]},
    )
    assert response.status_code == 400
    assert "unsafe" in response.json()["detail"].lower()


def test_build_patch_endpoint_requires_at_least_one_change(client):
    response = client.post("/api/v1/review/build-patch", json={"changes": []})
    assert response.status_code == 422


def test_build_patch_endpoint_caps_the_file_count(client):
    changes = [{"path": f"f{i}.py", "content": "x\n"} for i in range(60)]
    response = client.post("/api/v1/review/build-patch", json={"changes": changes})
    assert response.status_code == 422


def test_build_patch_endpoint_treats_an_unindexed_file_as_new(client):
    """
    A caller that supplies only the new content — which is all the write agent
    produces — must still get a valid patch rather than an error.
    """
    response = client.post(
        "/api/v1/review/build-patch",
        json={"changes": [{"path": "brand_new_file.py", "content": "print('hi')\n"}]},
    )
    assert response.status_code == 200
    data = response.json()
    assert "new file mode" in data["diff"]
    assert data["files"][0]["status"] == "added"


def test_build_patch_endpoint_reports_identical_content_as_a_client_error(client):
    response = client.post(
        "/api/v1/review/build-patch",
        json={"changes": [{"path": "same.py", "original": "a\n", "content": "a\n"}]},
    )
    assert response.status_code == 400
    assert "identical" in response.json()["detail"]
