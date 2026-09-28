"""
test_pr_token_source.py — one credential, one question.

THE BUG THIS PINS
-----------------
`github_service.get_token()` was written, and documented, to answer "which
account is this person?" with a clear precedence: the token connected **in the
app** wins over `GITHUB_TOKEN` from the environment, because a machine very often
already exports that variable for CI, a deploy, or another tool, and a product
that used it would open pull requests as a stranger.

That rule was implemented in one file and then bypassed in three others, all
written before the Connect button existed and never revisited:

    app/services/pr_service.py   create_pr_via_api  → sent os.getenv(...)
    app/api/review.py            the push gate      → tested os.getenv(...)
    app/services/agent_tools.py  create_pr gate     → tested os.getenv(...)

The consequences were both bad, and in opposite directions:

* **The Connect button did nothing for writing.** A user pasted a token into
  SavFlux, connected a repository, browsed it — and then Create PR answered
  "GITHUB_TOKEN not configured", because they had connected it in the app and
  not exported it in their shell. This is the "GitHub integration is
  incomplete" complaint, in one sentence.

* **A machine with an ambient token pushed as the wrong account.** Worse than
  the first, because it succeeds: the PR opens, under a machine identity the
  person did not choose, on a repository they did not intend.

So the rule these tests hold: the answer to "is GitHub connected" and the
credential actually sent are both `github_service.get_token()`, everywhere, with
no `os.getenv("GITHUB_TOKEN")` left outside the one place that documents the
fallback. The last test greps the source for that, because a test that only
exercises today's code stops the day someone adds a fourth call site.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services import github_service as gh
from app.services import pr_service

APP = Path(__file__).resolve().parents[1] / "app"


# ── The credential actually sent ─────────────────────────────────────────────


async def test_create_pr_sends_the_token_connected_in_the_app(monkeypatch, tmp_path):
    """
    THE regression. A token stored by the Connect dialog — not exported by
    anybody — must be the one that reaches GitHub, and the request must be made.
    """
    monkeypatch.setattr(gh, "get_token", lambda: "ghp_connected_in_the_app")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    seen: dict = {}

    async def fake_pull(slug, *, title, head, base, body=""):
        seen.update(slug=slug, title=title, head=head, base=base, body=body)
        return {"number": 42, "url": "https://github.com/o/r/pull/42", "state": "open"}

    monkeypatch.setattr(gh, "create_pull", fake_pull)
    result = await pr_service.create_pr_via_api("o/r", "savflux/autofix", "main", "Fix the salt")

    assert result["number"] == 42
    assert seen["head"] == "savflux/autofix"
    # The call happened at all — before the fix, the missing env var short
    # circuited it and the user got a manual plan instead.
    assert gh.get_token() == "ghp_connected_in_the_app"


async def test_the_app_token_wins_over_an_ambient_environment_token(monkeypatch):
    """
    The precedence, end to end. `get_token()` is stubbed here to return the app
    token, and the assertion is on the *credential that would be sent* — which is
    `get_token()`, by construction. What is pinned here is that the plumbing no
    longer has a second opinion.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_ci_token")
    monkeypatch.setattr(gh, "get_token", lambda: "ghp_connected_in_the_app")

    used: dict = {}

    async def fake_pull(slug, **kwargs):
        used["token"] = gh.get_token()
        return {"number": 1, "url": "u", "state": "open"}

    monkeypatch.setattr(gh, "create_pull", fake_pull)
    await pr_service.create_pr_via_api("o/r", "h", "main", "t")

    assert used["token"] == "ghp_connected_in_the_app"
    assert used["token"] != "ghp_ambient_ci_token"


async def test_not_connected_says_so_in_words_a_person_can_act_on(monkeypatch):
    """
    "GITHUB_TOKEN is not configured" names a shell variable. The person reading
    it connected an account in the product and cannot find that variable
    anywhere, which is the whole bug restated. The message has to name the thing
    they did do.
    """
    monkeypatch.setattr(gh, "get_token", lambda: "")
    with pytest.raises(RuntimeError) as exc:
        await pr_service.create_pr_via_api("o/r", "h", "main", "t")
    assert "not connected" in str(exc.value).lower()
    assert "connect" in str(exc.value).lower()


async def test_a_github_error_is_still_a_runtime_error(monkeypatch):
    """
    The contract callers depend on: exactly one exception type to handle, so
    they can fall back to the `gh` command. `GitHubError` subclasses
    `RuntimeError`, so delegation preserved it — but that is a property of the
    class hierarchy, not of the new code, so it is asserted rather than assumed.
    """
    monkeypatch.setattr(gh, "get_token", lambda: "ghp_x")

    async def boom(*a, **k):
        raise gh.GitHubError("GitHub API 403: Resource not accessible", status=403, kind="auth")

    monkeypatch.setattr(gh, "create_pull", boom)
    with pytest.raises(RuntimeError) as exc:
        await pr_service.create_pr_via_api("o/r", "h", "main", "t")
    assert "403" in str(exc.value)


# ── No fourth copy of the question ───────────────────────────────────────────


#: The one place allowed to read the variable, and why.
#:
#: `api/github.py` asks a genuinely different question in `DELETE /connect`:
#: "after I delete the token you pasted in, will requests still be
#: authenticated?" The answer has to come from the environment specifically,
#: because that is the fallback `get_token()` would fall through to. It is
#: reporting state, not deciding whether a write may happen.
_ALLOWED = {
    "api/github.py": "reports the env fallback when disconnecting",
}


def test_no_module_outside_github_service_asks_the_environment_directly():
    """
    Structural, and deliberately not a grep.

    The three bugs above were each a *single line* that looked reasonable in
    isolation. A behavioural test cannot tell you a fourth one has not been
    written yet; this can. `github_service` is the one allowed to read
    `GITHUB_TOKEN`, because that is where the fallback is documented.

    Parsed with `ast` rather than matched as text, because the fix for one of
    these bugs was to write a comment explaining why the line is wrong — and a
    comment mentioning `os.getenv("GITHUB_TOKEN")` must not be able to fail the
    test that forbids the call.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP))
        if path.name == "github_service.py" or rel in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "getenv"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "GITHUB_TOKEN":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "these modules ask the process environment whether GitHub is connected, "
        "which is not the same question as 'did the user connect an account in "
        f"SavFlux': {offenders}. Use github_service.get_token()."
    )


def test_get_token_still_falls_back_to_the_environment():
    """
    The fallback itself must survive: a headless deployment that never touches
    the UI has to keep working, which is the whole reason the env var is read at
    all. Only the *priority* was wrong, not the existence.
    """
    import os
    from app.core import paths

    token_file = paths.data_file("github_token")
    saved = token_file.read_bytes() if token_file.exists() else None
    try:
        token_file.unlink(missing_ok=True)
        os.environ["GITHUB_TOKEN"] = "ghp_from_the_shell"
        assert gh.get_token() == "ghp_from_the_shell"
    finally:
        os.environ.pop("GITHUB_TOKEN", None)
        if saved is not None:
            token_file.write_bytes(saved)


# ── Disconnect must not claim a sign-out it did not perform ─────────────────


async def test_disconnect_reports_the_env_fallback_even_when_it_removed_a_token(
    client, monkeypatch
):
    """
    The case the endpoint exists for, and the one its condition excluded.

    A person connects a token in the app, disconnects it, and the header still
    shows an account because the environment has one. Reporting `fallback: null`
    there says "you are signed out" while requests keep authenticating — so the
    next thing they do is something they believe is anonymous.

    The condition was `env_present and not removed`, and this scenario is
    exactly `removed=True, env_present=True`.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_the_shell")

    def fake_clear():
        return True

    monkeypatch.setattr(gh, "clear_token", fake_clear)

    resp = client.delete("/api/v1/github/connect")
    assert resp.status_code == 200
    body = resp.json()

    assert body["removed"] is True
    assert body["fallback"] == "env", (
        "the app token was removed but the environment token is still in charge; "
        "reporting no fallback tells the user they are signed out when they are not"
    )
    assert body["message"] and "environment" in body["message"]


async def test_disconnect_with_nothing_left_is_a_real_sign_out(client, monkeypatch):
    """The other end of the same switch, so the fix cannot just always warn."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def fake_clear():
        return True

    monkeypatch.setattr(gh, "clear_token", fake_clear)

    resp = client.delete("/api/v1/github/connect")
    body = resp.json()
    assert body["fallback"] is None
    assert body["message"] is None
