"""
test_pr_webhook.py — Test automated GitHub PR webhook review endpoint.
"""

import pytest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app.services.multi_review_agent import _static_triage, _triage_score
from app.services.multi_review_agent import stream_multi_review


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


def test_pr_webhook_rejects_empty_diff(client):
    res = client.post(
        "/api/v1/review/pr-webhook",
        json={"repo": "owner/repo", "pr_number": 42, "title": "Test PR", "diff": "   "},
    )
    assert res.status_code == 400


def test_pr_webhook_valid_diff_returns_structured_review(client):
    mock_review_stream = ["## 📁 File Overview\n", "Looks good!\n"]

    async def fake_stream(*args, **kwargs):
        for token in mock_review_stream:
            yield token

    with patch("app.api.review.stream_code_review", side_effect=fake_stream):
        res = client.post(
            "/api/v1/review/pr-webhook",
            json={
                "repo": "owner/repo",
                "pr_number": 101,
                "title": "Add auth endpoint",
                "diff": "+ def login(): pass",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["pr_number"] == 101
        assert "Looks good!" in data["review"]
        assert "impact" in data
        assert data["impact"]["changed_symbols"] == ["login"]


def test_pr_impact_extracts_risk_and_changed_files(client):
    res = client.post(
        "/api/v1/review/impact",
        json={
            "repo": "owner/repo",
            "pr_number": 7,
            "title": "Auth change",
            "diff": "diff --git a/auth.py b/auth.py\n+++ b/auth.py\n+def login():\n+    password = 'hardcoded'\n",
        },
    )
    assert res.status_code == 200
    impact = res.json()["impact"]
    assert impact["changed_files"] == ["auth.py"]
    assert impact["changed_symbols"] == ["login"]
    assert impact["risk_score"] >= 3
    assert impact["security_flags"]


def test_large_review_triage_prioritizes_source_files():
    source = {"file_name": "auth_service.py", "language": "py", "content": "password = value"}
    lockfile = {"file_name": "package-lock.json", "language": "json", "content": "{}"}
    assert _triage_score(source) > _triage_score(lockfile)
    # _static_triage outputs a full structured review — check for the footer sentinel
    assert "Static analysis" in _static_triage(lockfile)


def test_static_triage_ignores_ui_words_that_look_like_sql():
    content = "\n".join([
        "const label = 'repo selector';",
        "const isSelected = selectedNode?.id === n.id;",
        "const isSearchDim = matchingIds !== null;",
    ])
    review = _static_triage({"file_name": "App.tsx", "language": "tsx", "content": content})
    # `selectedNode`/`isSelected` contain the substring "select"; a keyword scan
    # reads that as SQL. Asserting on the all-clear marker rather than the
    # absence of one phrase keeps this honest if rule wording changes.
    security = review.split("## 🔒 Security")[1].split("## ⚠️")[0]
    assert "✅" in security, f"clean TSX should have no security findings, got: {security}"


def test_static_triage_flags_real_dynamic_sql():
    content = 'cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")'
    review = _static_triage({"file_name": "db.py", "language": "py", "content": content})
    security = review.split("## 🔒 Security")[1].split("## ⚠️")[0]
    assert "SQL injection" in security
    assert "CWE-89" in security, "a security finding should carry its CWE id"
    assert "L1" in security, "the finding should name the offending line"


def test_static_triage_ignores_placeholder_secret_config():
    content = "\n".join([
        "OPENAI_API_KEY: sk-ci-placeholder",
        "GEMINI_API_KEY=your_free_key_from_aistudio",
        "api_key = settings.api_key",
    ])
    review = _static_triage({"file_name": "ci.yml", "language": "yml", "content": content})
    # No hardcoded credential should be flagged (all are placeholders / env-var refs)
    assert "Hardcoded credential" not in review


def test_concurrent_review_emits_section_before_its_tokens():
    async def fake_stream(file_name, content, language, repo_context="", model_override=""):
        yield f"review for {file_name}"

    files = [
        {"file_name": "a.py", "content": "def a(): pass", "language": "py"},
        {"file_name": "b.py", "content": "def b(): pass", "language": "py"},
    ]
    fake_settings = SimpleNamespace(
        review_mode="fast",
        review_max_full_files=2,
        review_concurrency=2,
        llm_provider="ollama",
        summary_mixture_models="",
    )

    async def collect():
        with patch("app.services.multi_review_agent.stream_fast_code_review", fake_stream), \
             patch("app.services.multi_review_agent.get_settings", return_value=fake_settings), \
             patch("app.services.multi_review_agent.get_chat_llm"):
            return [token async for token in stream_multi_review(files)]

    output = asyncio.run(collect())
    for name in ("a.py", "b.py"):
        marker = f'"file_name": "{name}"'
        assert any(marker in token for token in output)
        marker_index = next(i for i, token in enumerate(output) if marker in token)
        token_index = next(i for i, token in enumerate(output) if f"review for {name}" in token)
        assert marker_index < token_index


def test_fast_review_mode_uses_single_pass_reviewer():
    async def fake_fast_stream(file_name, content, language, repo_context="", model_override=""):
        yield f"fast review for {file_name}"

    async def fake_agentic_stream(file_name, content, language, repo_context="", model_override=""):
        yield f"agentic review for {file_name}"

    files = [{"file_name": "service.py", "content": "def run(): pass", "language": "py"}]
    fake_settings = SimpleNamespace(
        review_mode="fast",
        review_max_full_files=1,
        review_concurrency=1,
        llm_provider="ollama",
        summary_mixture_models="",
    )

    async def collect():
        with patch("app.services.multi_review_agent.stream_fast_code_review", fake_fast_stream), \
             patch("app.services.multi_review_agent.stream_code_review", fake_agentic_stream), \
             patch("app.services.multi_review_agent.get_settings", return_value=fake_settings):
            return "".join([token async for token in stream_multi_review(files)])

    output = asyncio.run(collect())
    assert "fast review for service.py" in output
    assert "agentic review" not in output


def test_multi_review_falls_back_to_static_triage_when_provider_review_fails():
    async def failing_stream(file_name, content, language, repo_context="", model_override=""):
        yield "__ERROR__Error code: 402 - Insufficient Balance__ERROR_END__\n"

    files = [{"file_name": "billing.py", "content": "password = value", "language": "py"}]
    fake_settings = SimpleNamespace(
        review_mode="fast",
        review_max_full_files=1,
        review_concurrency=1,
        llm_provider="ollama",
        summary_mixture_models="",
    )

    async def collect():
        with patch("app.services.multi_review_agent.stream_fast_code_review", failing_stream), \
             patch("app.services.multi_review_agent.get_settings", return_value=fake_settings):
            return "".join([token async for token in stream_multi_review(files)])

    output = asyncio.run(collect())
    # _static_triage emits "Static analysis" in its footer sentinel
    assert "Static analysis" in output
    assert "Insufficient Balance" not in output
    assert "Static fallback" in output
