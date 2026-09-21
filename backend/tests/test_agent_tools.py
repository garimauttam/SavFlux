"""
Tests for the agent's tool layer.

Two classes of bug this file exists to catch:

  1. **Drift.** A catalogue that advertises a tool nothing can call — which is
     exactly how `autofix`, `build-patch` and `create-pr` sat unreachable while
     passing their own tests. `test_dispatch_covers_the_catalogue` fails the
     moment a tool is registered without an implementation, or implemented
     without being registered.

  2. **An unconfirmed push.** `create_pr` reaches a real repository. Every path
     that could push without the caller having seen the diff is asserted to
     refuse, with the network mocked so the assertion is about the guard rather
     than about GitHub being reachable.

The end-to-end test drives the real agent stream — goal → plan → tools → report —
because "the tool works" and "the agent can reach the tool" are different claims,
and only the second one was broken.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import agent_tools
from app.services.agent_tools import (
    DISPATCH,
    MUTATING_TOOLS,
    TOOL_BY_NAME,
    TOOL_CATALOGUE,
    ToolResult,
    build_patch,
    create_pr,
)
from app.services.patch_service import digest_of
from app.services.fix_service import _apply
from app.services.indexed_content import read_indexed_file

SOURCE_ID = "https://github.com/octo/demo::pkg/net.py"
NET_PY = (
    "import hashlib\n"
    "import requests\n"
    "\n"
    "def fetch(url):\n"
    "    return requests.get(url, verify=False)\n"
    "\n"
    "def cache_key(data):\n"
    "    return hashlib.md5(data).hexdigest()\n"
)


def _fake_document(content: str = NET_PY, source: str = SOURCE_ID):
    from langchain_core.documents import Document

    return Document(
        page_content=content,
        metadata={"source": source, "file_name": "net.py", "language": "py"},
    )


def _stream_text(response) -> str:
    return "".join(response.iter_text())


def _done(steps: list[dict], tool: str) -> dict:
    """The completed marker for one tool — not the "tool: running…" one."""
    return next(
        step for step in steps
        if step.get("tool") == tool and step.get("step") in ("tool_done", "tool_error")
    )


def _statuses(text: str) -> list[dict]:
    """Every __STATUS__ payload in a stream, as dicts."""
    found = []
    cursor = 0
    while True:
        start = text.find("__STATUS__", cursor)
        if start == -1:
            return found
        end = text.find("__STATUS_END__", start)
        if end == -1:
            return found
        found.append(json.loads(text[start + 10:end]))
        cursor = end + len("__STATUS_END__")


# ── Catalogue ─────────────────────────────────────────────────────────────────


def test_the_three_unreachable_endpoints_are_registered_tools():
    """The regression this whole change exists to prevent."""
    names = {tool["name"] for tool in TOOL_CATALOGUE}
    assert {"autofix", "build_patch", "create_pr"} <= names


def test_dispatch_covers_the_catalogue():
    """Registered without an implementation, or implemented but unregistered."""
    assert {tool["name"] for tool in TOOL_CATALOGUE} == set(DISPATCH)
    assert set(DISPATCH) == set(TOOL_BY_NAME)


def test_every_tool_carries_a_typed_schema():
    for tool in TOOL_CATALOGUE:
        assert tool["description"]
        assert tool["args"] == [arg["name"] for arg in tool["args_schema"]]
        assert tool["args_schema"], tool["name"]
        for arg in tool["args_schema"]:
            assert arg["type"] in {"string", "integer", "array", "boolean", "object"}
            assert isinstance(arg["required"], bool)
            assert arg["description"]


def test_only_create_pr_is_marked_mutating():
    """The UI shows a warning for these; a wrong flag is a wrong warning."""
    flagged = {tool["name"] for tool in TOOL_CATALOGUE if tool["mutating"]}
    assert flagged == MUTATING_TOOLS == {"create_pr"}


def test_catalogue_is_served_over_the_api(client):
    response = client.get("/api/v1/agent/tools")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == len(TOOL_CATALOGUE)
    by_name = {tool["name"]: tool for tool in payload["tools"]}
    assert by_name["create_pr"]["mutating"] is True
    assert by_name["autofix"]["mutating"] is False


# ── autofix tool ──────────────────────────────────────────────────────────────


async def test_autofix_tool_returns_a_verified_patch():
    with patch("app.services.indexed_content.read_indexed_file", return_value=NET_PY):
        result = await agent_tools.autofix("pkg/net.py", source=SOURCE_ID)

    assert result.ok and result.data["fixed"] is True
    assert result.data["count"] == 2
    assert result.data["score_after"] > result.data["score_before"]
    assert result.data["content"] != NET_PY  # the repaired source travels with it
    assert "verify=False" not in result.data["content"]
    assert result.report and "verified fixes" in result.report


async def test_autofix_tool_reports_no_fixable_findings_honestly():
    source = "def q(conn, table):\n    conn.execute(f'SELECT * FROM {table}')\n"
    with patch("app.services.indexed_content.read_indexed_file", return_value=source):
        result = await agent_tools.autofix("db.py")

    assert result.ok is True
    assert result.data["fixed"] is False
    assert "nothing safe to fix" in result.message


async def test_autofix_tool_refuses_a_language_it_cannot_fix():
    """
    The contract: refuse loudly rather than return a silent empty result.

    Go is the fixture because it has no verified fixer. JavaScript used to be
    used here, and stopped being a valid example once JS/TS autofix landed — the
    point of the test is the refusal, not which language demonstrates it.
    """
    result = await agent_tools.autofix("main.go")
    assert result.ok is False
    assert "javascript" in result.message.lower(), result.message


async def test_autofix_tool_reports_a_missing_file():
    result = await agent_tools.autofix("ghost.py")
    assert result.ok is False
    assert "no content" in result.message.lower()


# ── build_patch tool ──────────────────────────────────────────────────────────


async def test_build_patch_tool_returns_diff_branch_and_body():
    original = _apply(NET_PY, "pkg/net.py", "py")
    result = await build_patch(
        changes=[{"path": "pkg/net.py", "original": NET_PY, "content": original.content}],
        title="Fix TLS verification",
        summary="Stops ignoring certificate errors.",
    )

    assert result.ok
    assert result.data["diff"].startswith("diff --git a/pkg/net.py b/pkg/net.py")
    assert result.data["suggested_branch"].startswith("savflux/fix-tls-verification")
    assert "Stops ignoring certificate errors." in result.data["pr_body"]
    assert result.data["digest"]


async def test_build_patch_digest_matches_the_patch_service_digest():
    """
    The digest is the confirmation token for `create_pr`. If the two producers
    ever disagreed, a diff the user approved could be rejected for confirming —
    or worse, a different one accepted.
    """
    from app.services.patch_service import FileChange, build_patch as service_build

    expected = service_build([FileChange("a.py", "x = 1\n", "x = 2\n")]).digest
    result = await build_patch(changes=[{"path": "a.py", "original": "x = 1\n", "content": "x = 2\n"}])

    assert result.data["digest"] == expected == digest_of(result.data["diff"])


async def test_build_patch_tool_reports_identical_content_as_a_failure():
    result = await build_patch(changes=[{"path": "a.py", "original": "x\n", "content": "x\n"}])
    assert result.ok is False
    assert "identical" in result.message


async def test_build_patch_tool_rejects_path_traversal():
    result = await build_patch(changes=[{"path": "../../.ssh/authorized_keys", "content": "k"}])
    assert result.ok is False
    assert "unsafe" in result.message.lower()


# ── create_pr tool ────────────────────────────────────────────────────────────

DIFF = (
    "diff --git a/pkg/net.py b/pkg/net.py\n"
    "--- a/pkg/net.py\n"
    "+++ b/pkg/net.py\n"
    "@@ -3,4 +3,4 @@\n"
    " def fetch(url):\n"
    "-    return requests.get(url, verify=False)\n"
    "+    return requests.get(url)\n"
)


@pytest.fixture()
def no_push(monkeypatch):
    """Fail the test if anything reaches the GitHub API."""
    api = AsyncMock(side_effect=AssertionError("pushed without confirmation"))
    monkeypatch.setattr("app.services.pr_service.create_pr_via_api", api)
    return api


async def test_create_pr_without_confirmation_returns_a_plan_and_pushes_nothing(no_push):
    result = await create_pr(repo="octo/demo", head="savflux/fix", base="main",
                             title="Fix TLS", diff=DIFF)

    assert result.ok
    assert result.data["status"] == "manual"
    assert result.data["digest"] == digest_of(DIFF)
    assert result.data["gh_command"].startswith("gh pr create --repo octo/demo")
    assert result.data["patch"] == DIFF
    assert "confirmation required" in result.data["reason"].lower()
    no_push.assert_not_called()


async def test_a_wrong_digest_is_refused(no_push):
    """A stale preview must not authorise a push."""
    result = await create_pr(repo="octo/demo", head="savflux/fix",
                             diff=DIFF, confirm_digest="deadbeefdeadbeef")

    assert result.data["status"] == "manual"
    no_push.assert_not_called()


async def test_the_right_digest_without_a_token_still_does_not_push(monkeypatch, no_push):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = await create_pr(repo="octo/demo", head="savflux/fix", diff=DIFF,
                             confirm_digest=digest_of(DIFF))

    assert result.ok
    assert result.data["status"] == "manual"
    assert "GITHUB_TOKEN" in result.data["reason"]
    no_push.assert_not_called()


async def test_the_right_digest_with_a_token_opens_the_pr(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    api = AsyncMock(return_value={"number": 7, "url": "https://github.com/octo/demo/pull/7",
                                  "state": "open"})
    monkeypatch.setattr("app.services.pr_service.create_pr_via_api", api)

    result = await create_pr(repo="octo/demo", head="savflux/fix", base="main",
                             title="Fix TLS", body="body", diff=DIFF,
                             confirm_digest=digest_of(DIFF))

    assert result.ok
    assert result.data["status"] == "created" and result.data["number"] == 7
    api.assert_awaited_once_with("octo/demo", "savflux/fix", "main", "Fix TLS", "body")


async def test_a_github_failure_falls_back_to_the_command(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setattr(
        "app.services.pr_service.create_pr_via_api",
        AsyncMock(side_effect=RuntimeError("422 head branch does not exist")),
    )
    result = await create_pr(repo="octo/demo", head="savflux/fix", diff=DIFF,
                             confirm_digest=digest_of(DIFF))

    assert result.ok is False
    assert result.data["status"] == "manual"
    assert "head branch does not exist" in result.data["reason"]
    assert result.data["gh_command"]


async def test_create_pr_without_a_diff_has_nothing_to_do():
    result = await create_pr(repo="octo/demo", head="savflux/fix")
    assert result.ok is False
    assert "no diff" in result.message.lower()


async def test_create_pr_rejects_a_non_github_repo():
    result = await create_pr(repo="/tmp/clone", head="savflux/fix", diff=DIFF)
    assert result.ok is False
    assert result.data["status"] == "invalid"


def test_status_markers_carry_telemetry_but_not_payloads():
    """
    A status line is emitted into the stream and re-parsed by the UI. The whole
    diff belongs in the report, not in a marker that is serialised per chunk.
    """
    from app.api.agent import _status

    result = ToolResult("build_patch", True, "built",
                        data={"digest": "abc123", "files_changed": 2,
                              "diff": "x" * 20_000, "pr_body": "y" * 5000,
                              "content": "z" * 5000, "files": [{"path": "a.py"}]})
    marker = _status("tool_done", result.message, tool=result.tool, **result.status_data())

    assert isinstance(marker, str) and marker.startswith("__STATUS__{")
    assert "abc123" in marker and "files_changed" in marker
    assert "xxx" not in marker and "yyy" not in marker and "zzz" not in marker
    assert len(marker) < 500


# ── Agent planning ────────────────────────────────────────────────────────────


def test_plan_inference():
    from app.api.agent import build_plan

    assert build_plan("map the authentication flow") == [
        "retrieve_context", "read_file", "dependency_graph", "blast_radius",
    ]
    assert "autofix" in build_plan("fix the TLS bug in net.py")
    assert "autofix" not in build_plan("improve the docs")   # "pr" is not a substring match
    assert "create_pr" in build_plan("fix the bug and open a PR")
    assert build_plan("open a pull request for the fix").index("create_pr") > \
        build_plan("open a pull request for the fix").index("build_patch")


def test_explicit_tools_win_and_keep_canonical_order():
    from app.api.agent import build_plan

    assert build_plan("anything at all", ["create_pr", "retrieve_context"]) == \
        ["retrieve_context", "create_pr"]


def test_unknown_tool_is_a_validation_error():
    from pydantic import ValidationError

    from app.api.agent import AgentRunRequest

    with pytest.raises(ValidationError):
        AgentRunRequest(goal="x", tools=["rm_rf"])
    with pytest.raises(ValidationError):
        AgentRunRequest(goal="x", tools=[])
    with pytest.raises(ValidationError):
        AgentRunRequest(goal="x", confirm_digest="not a digest")


# ── End to end: goal → tools → report ─────────────────────────────────────────


def _run_stream(client, **payload):
    response = client.post("/api/v1/agent/run", json=payload)
    assert response.status_code == 200, response.text
    return _stream_text(response)


@pytest.fixture()
def indexed_net_py():
    """Retrieval finds net.py; the index can reconstruct it; no disk access."""
    with patch("app.services.retrieval_service.retrieve_chunks",
               new=AsyncMock(return_value=[_fake_document()])), \
         patch("app.services.indexed_content.read_indexed_file", return_value=NET_PY):
        yield


def test_agent_reaches_autofix_and_build_patch_end_to_end(client, indexed_net_py, no_push):
    """
    The claim under test: the agent can prove a bug, fix it, and produce a patch
    the user can apply — from one goal, with no model call.
    """
    text = _run_stream(client, goal="fix the unsafe TLS call in the network layer",
                       repo_url="https://github.com/octo/demo")
    steps = _statuses(text)
    tools = [step.get("tool") for step in steps if step.get("tool")]

    assert "autofix" in tools and "build_patch" in tools
    autofix_step = _done(steps, "autofix")
    assert autofix_step["fixed"] is True and autofix_step["count"] == 2

    patch_step = _done(steps, "build_patch")
    assert patch_step["files_changed"] == 1 and patch_step["digest"]

    assert "```diff" in text and "diff --git a/pkg/net.py b/pkg/net.py" in text
    assert "verify=False" in text  # the removed line is visible in the diff
    no_push.assert_not_called()


def test_agent_does_not_open_a_pr_without_a_confirmed_diff(client, indexed_net_py, no_push):
    text = _run_stream(client, goal="fix the TLS call and open a PR",
                       repo_url="https://github.com/octo/demo")
    steps = _statuses(text)

    pr_step = _done(steps, "create_pr")
    assert pr_step["status"] == "manual"
    assert pr_step["digest"] == _apply(NET_PY, "pkg/net.py", "py").digest
    # The command a human runs is in the report; nothing was pushed.
    assert "gh pr create --repo octo/demo" in text
    no_push.assert_not_called()


def test_agent_opens_the_pr_when_the_exact_diff_was_confirmed(client, indexed_net_py, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    api = AsyncMock(return_value={"number": 12, "url": "https://github.com/octo/demo/pull/12",
                                  "state": "open"})
    monkeypatch.setattr("app.services.pr_service.create_pr_via_api", api)
    digest = _apply(NET_PY, "pkg/net.py", "py").digest

    text = _run_stream(client, goal="fix the TLS call and open a PR",
                       repo_url="https://github.com/octo/demo", confirm_digest=digest)
    steps = _statuses(text)

    assert _done(steps, "create_pr")["status"] == "created"
    assert "pull/12" in text
    api.assert_awaited_once()


def test_agent_refuses_a_confirmed_push_when_the_diff_changed(client, indexed_net_py, no_push):
    """
    The digest is bound to content, not to intent. If the file changed between
    the preview and the push, the confirmation no longer describes what would be
    pushed — so it is refused rather than silently applying the older one.
    """
    stale = _apply(NET_PY + "\nx = 1\n", "pkg/net.py", "py").digest
    assert stale  # a real digest from a different revision of the file

    text = _run_stream(client, goal="fix the TLS call and open a PR",
                       repo_url="https://github.com/octo/demo", confirm_digest=stale)
    assert _done(_statuses(text), "create_pr")["status"] == "manual"


def test_a_confirm_digest_without_a_pr_plan_is_rejected(client):
    """A confirmation that authorises nothing is a sign the caller is confused."""
    response = client.post("/api/v1/agent/run", json={
        "goal": "map the auth flow", "confirm_digest": digest_of(DIFF),
    })
    assert response.status_code == 400
    assert "does not open a pull request" in response.json()["detail"]


def test_agent_skips_the_pr_when_no_github_repo_is_known(client, indexed_net_py, no_push):
    text = _run_stream(client, goal="fix the TLS call and open a PR")
    skipped = [step for step in _statuses(text) if step.get("step") == "tool_skipped"]
    assert any("github.com" in step["message"] for step in skipped)
    no_push.assert_not_called()


def test_agent_run_stays_deterministic(client, indexed_net_py, no_push):
    """Same goal, same index, same bytes — that is the $0 guarantee."""
    first = _run_stream(client, goal="fix the weak hash in the cache layer",
                        repo_url="https://github.com/octo/demo")
    second = _run_stream(client, goal="fix the weak hash in the cache layer",
                         repo_url="https://github.com/octo/demo")
    assert first == second


# ── indexed_content lookup ladder ─────────────────────────────────────────────


def test_exact_source_id_is_tried_before_any_scan(monkeypatch):
    """
    The old lookup scanned every chunk to rebuild one file. The cheap path must
    be taken first, and the scan must not run at all when it hits.
    """
    calls: list[dict] = []

    class FakeCollection:
        def get(self, where=None, include=None):  # noqa: A002 - mimics chromadb
            calls.append({"where": where, "include": include})
            if where == {"source": SOURCE_ID}:
                return {"metadatas": [{"source": SOURCE_ID, "chunk_index": 0}],
                        "documents": ["print('hi')\n"]}
            raise AssertionError("unexpected query — the cheap path should have hit")

    monkeypatch.setattr("app.services.ingestion_service._get_vectorstore",
                        lambda: type("S", (), {"_collection": FakeCollection()})())

    content = read_indexed_file("pkg/net.py", source=SOURCE_ID)

    assert content == "print('hi')\n"
    assert len(calls) == 1 and calls[0]["where"] == {"source": SOURCE_ID}


def test_file_name_filter_is_the_second_rung(monkeypatch):
    calls: list[dict] = []

    class FakeCollection:
        def get(self, where=None, include=None):  # noqa: A002
            calls.append(where)
            if where and "file_name" in where:
                return {"metadatas": [{"source": SOURCE_ID, "chunk_index": 0}],
                        "documents": ["print('hi')\n"]}
            return {"metadatas": [], "documents": []}

    monkeypatch.setattr("app.services.ingestion_service._get_vectorstore",
                        lambda: type("S", (), {"_collection": FakeCollection()})())

    content = read_indexed_file("pkg/net.py", repo_url="https://github.com/other/repo")

    assert content == "print('hi')\n"
    # Every exact-source guess missed, the file-name filter hit, and the full
    # scan (where=None) never ran — the ladder stops as soon as it finds content.
    assert {"file_name": "net.py"} in calls
    assert None not in calls


def test_a_file_that_is_not_indexed_returns_empty(monkeypatch):
    class EmptyCollection:
        def get(self, where=None, include=None):  # noqa: A002
            return {"metadatas": [], "documents": []}

    monkeypatch.setattr("app.services.ingestion_service._get_vectorstore",
                        lambda: type("S", (), {"_collection": EmptyCollection()})())

    assert read_indexed_file("ghost.py") == ""
