"""
test_github_capabilities.py — read and write, decided by GitHub rather than by us.

WHY THIS FILE EXISTS
--------------------
The product promises two different things depending on what you connected:

* a **public** repository is read with no token at all — `get_token()` returns
  `""` and `auth_clone_kwargs()` returns `{}`, so the unauthenticated API is
  used;
* a **private** repository, and any push, needs a token, and a fine-grained
  token can be scoped to read.

For that to be a promise rather than a hope, the app has to know which it is
looking at, per repository, *before* offering to write. It did not: `get_repo`
computed `permissions` and `list_repos` dropped them, so the repository list —
the only place a user chooses what to work on — carried no capability signal at
all. The natural next step from there is a button that appears and then fails at
push time on someone else's repository.

The rule this file pins: **absence of a `permissions` object means "cannot", and
`can_write` is the only definition of "can push".** It is deliberately pessimistic
in every direction, because the cost of the two mistakes is not equal — offering
a write that fails costs a round trip and a confusing 403; hiding a write that
would have worked costs one sentence that says why.
"""

from __future__ import annotations

import base64
import os

import pytest

from app.services import github_service as gh


# ── shape_permissions: the four states a repository can be in ────────────────


def test_no_token_reports_cannot_write_rather_than_missing():
    """
    A public repository fetched unauthenticated has NO `permissions` key at all.
    The tempting reading is "no permissions object, so nothing to check, so allow
    it" — which is exactly backwards: no evidence of push is not evidence of push.
    """
    repo = gh.shape_repo({"full_name": "pallets/click", "name": "click", "private": False})
    assert repo["permissions"] == {"admin": False, "push": False, "pull": False}
    assert gh.can_write(repo) is False


def test_a_read_only_token_is_reported_as_read_only():
    """
    A fine-grained token with Contents: Read. This is the state the read-only
    story depends on, and it must not be rounded up to "can write" because the
    token is *valid* — validity and capability are different questions.
    """
    repo = gh.shape_repo(
        {
            "full_name": "o/private",
            "name": "private",
            "private": True,
            "permissions": {"admin": False, "push": False, "pull": True},
        }
    )
    assert gh.can_write(repo) is False
    # Read is genuinely available — that is what makes the repository usable.
    assert repo["permissions"]["pull"] is True


def test_a_write_token_is_reported_as_writable():
    repo = gh.shape_repo(
        {
            "full_name": "o/r",
            "name": "r",
            "permissions": {"admin": True, "push": True, "pull": True},
        }
    )
    assert gh.can_write(repo) is True


def test_push_and_pull_are_independent():
    """
    GitHub really does hand out these combinations — a fine-grained token with
    Pull requests: write but Contents: read cannot push a branch, and conflating
    the two would offer a "create PR" button that cannot create the branch the
    pull request needs.
    """
    pr_write_no_push = gh.shape_repo(
        {"name": "r", "permissions": {"admin": False, "push": False, "pull": True}}
    )
    assert gh.can_write(pr_write_no_push) is False


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"name": "r"}, {"name": "r", "permissions": None}, {"name": "r", "permissions": {}}],
)
def test_can_write_never_raises_on_a_partial_repo(payload):
    """
    The inputs here are all things a real response can be: a redirect, a deleted
    repository, a 202 while GitHub computes a large fork. A capability check that
    can raise is a capability check that will crash a page render, so it is
    written to answer False for anything it cannot confirm.
    """
    assert gh.can_write(gh.shape_repo(payload) if payload is not None else None) is False


# ── The list must carry the signal the single-repo view already had ───────────


async def test_list_repos_carries_permissions(monkeypatch):
    """
    THE REGRESSION. `GET /repos/{slug}` computed permissions and `list_repos`
    did not, so the repository list — the screen a user picks their work from —
    could not tell a writable repository from a readable one. Asserted through
    the public function rather than the helper, so dropping the key from
    `shape_repo` breaks this test.
    """
    captured: dict = {}

    async def fake_request(method, path, **kwargs):
        captured["path"] = path
        return [
            {
                "full_name": "o/read-only",
                "name": "read-only",
                "private": True,
                "permissions": {"admin": False, "push": False, "pull": True},
            },
            {
                "full_name": "o/writable",
                "name": "writable",
                "private": True,
                "permissions": {"admin": False, "push": True, "pull": True},
            },
        ]

    monkeypatch.setattr(gh, "_request", fake_request)
    repos = await gh.list_repos()

    assert [r["full_name"] for r in repos] == ["o/read-only", "o/writable"]
    assert [gh.can_write(r) for r in repos] == [False, True]
    assert captured["path"] == "/user/repos"


async def test_get_repo_still_reports_permissions_and_pr_count(monkeypatch):
    """
    The bespoke block this replaced did two things: it added permissions and an
    open-PR count. Only the first was duplicated, so only the first may vanish.
    """

    async def fake_request(method, path, **kwargs):
        return {
            "full_name": "o/r",
            "name": "r",
            "open_issues_count": 7,
            "permissions": {"admin": False, "push": True, "pull": True},
        }

    monkeypatch.setattr(gh, "_request", fake_request)
    repo = await gh.get_repo("o/r")

    assert gh.can_write(repo) is True
    assert repo["open_pr_count"] == 7


# ── The clone path ──────────────────────────────────────────────────────────
#
# `auth_clone_kwargs` exists so a private clone authenticates without putting the
# token in the clone URL. It does that by passing `-c http.extraHeader=…` as
# git's `multi_options`. GitPython 3.1.36+ refuses `multi_options` unless the
# caller passes `allow_unsafe_options=True`.
#
# That regression was invisible until a real index was attempted: with a
# GITHUB_TOKEN in the environment — the documented setup for private repos —
# every clone, public or private, died with
# `UnsafeOptionError: -c is not allowed`, and the agent's empty state, which
# advertises "paste a public URL, no account needed", could not index anything.


def test_auth_clone_kwargs_carries_the_credential_as_git_config_env(monkeypatch):
    """
    The opt-in travels as an environment entry, not as a `-c` command-line
    option, because GitPython splits `multi_options` on whitespace and the
    header value contains a space after the colon.
    """
    monkeypatch.setattr(gh, "get_token", lambda: "ghp_secret")

    env = gh.auth_clone_kwargs()["env"]

    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == "AUTHORIZATION: basic " + base64.b64encode(
        b"x-access-token:ghp_secret"
    ).decode()
    assert env["GIT_CONFIG_COUNT"] == "1"
    # The value must not be able to be re-split into extra argv entries.
    assert " " in env["GIT_CONFIG_VALUE_0"]
    assert "multi_options" not in gh.auth_clone_kwargs()


def test_auth_clone_kwargs_without_a_token_stays_empty(monkeypatch):
    """
    The public-repository path must not send a credential. It has none, and
    adding one would make an unauthenticated clone depend on an auth mechanism
    it does not need.
    """
    monkeypatch.setattr(gh, "get_token", lambda: "")

    assert gh.auth_clone_kwargs() == {}


def test_auth_clone_kwargs_preserve_the_surrounding_environment(monkeypatch):
    """
    `env=` replaces the subprocess environment outright, so anything not copied
    across is gone. Losing PATH here breaks the clone on any machine where git
    is not at an absolute path.
    """
    monkeypatch.setattr(gh, "get_token", lambda: "ghp_secret")

    env = gh.auth_clone_kwargs()["env"]

    for key in ("PATH", "HOME", "LANG"):
        assert env.get(key) == os.environ.get(key), f"{key} was dropped"


def test_git_reads_the_credential_as_one_opaque_value(monkeypatch):
    """
    The bug this guards is a *word-splitting* bug, and the only way to catch it
    is to let git read the value back.

    The header is `AUTHORIZATION: basic <base64>` — it contains a space, and
    that space is what GitPython's `multi_options` split on before handing the
    argument to git, producing `fatal: Too many arguments` (exit 129). Asking
    git for the value proves the whole value arrives intact.
    """
    import subprocess

    monkeypatch.setattr(gh, "get_token", lambda: "ghp_secret")
    env = gh.auth_clone_kwargs()["env"]

    proc = subprocess.run(
        ["git", "config", "--get", "http.https://github.com/.extraheader"],
        env=env, capture_output=True, text=True, timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == env["GIT_CONFIG_VALUE_0"]
    # The two spaces that used to break the clone, present and accounted for:
    # one after the header name, one after the auth scheme.
    assert proc.stdout.strip().count(" ") == 2
    assert proc.stdout.strip().endswith(base64.b64encode(b"x-access-token:ghp_secret").decode())


def test_clone_kwargs_are_accepted_by_git(monkeypatch, tmp_path):
    """
    And the arguments git receives for a real clone are legal, with no network:
    a path that cannot resolve fails with "does not exist", never with
    "Too many arguments".
    """
    import subprocess

    monkeypatch.setattr(gh, "get_token", lambda: "ghp_secret")
    env = gh.auth_clone_kwargs()["env"]

    proc = subprocess.run(
        ["git", "clone", "-v", "--depth=1", "--",
         "file:///nonexistent-savflux-fixture", str(tmp_path / "out")],
        env=env, capture_output=True, text=True, timeout=60,
    )

    assert "Too many arguments" not in proc.stderr, proc.stderr
    assert proc.returncode != 129, proc.stderr
