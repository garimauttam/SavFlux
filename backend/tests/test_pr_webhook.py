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


def test_concurrent_review_emits_section_before_its_tokens(isolated_data_dir):
    """
    The wire-protocol property: a section header is emitted before that file's
    tokens, so the UI can attach them to the right card.

    The fixtures are realistic files rather than one-line stubs because the
    planner sends a 14-character stub to static analysis — correctly, there is
    nothing in it to review — and then no model stream exists to order against.
    These files score high enough to earn their own review, which is what this
    test is about.
    """
    async def fake_stream(file_name, content, language, repo_context="", model_override=""):
        yield f"review for {file_name}"

    files = [
        {"file_name": "auth_a.py", "content": _REVIEWABLE_SOURCE_A, "language": "py"},
        {"file_name": "auth_b.py", "content": _REVIEWABLE_SOURCE_B, "language": "py"},
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
    for name in ("auth_a.py", "auth_b.py"):
        marker = f'"file_name": "{name}"'
        assert any(marker in token for token in output)
        marker_index = next(i for i, token in enumerate(output) if marker in token)
        token_index = next(i for i, token in enumerate(output) if f"review for {name}" in token)
        assert marker_index < token_index


def test_fast_review_mode_uses_single_pass_reviewer(isolated_data_dir):
    async def fake_fast_stream(file_name, content, language, repo_context="", model_override=""):
        yield f"fast review for {file_name}"

    async def fake_agentic_stream(file_name, content, language, repo_context="", model_override=""):
        yield f"agentic review for {file_name}"

    files = [{"file_name": "auth_service.py", "content": _REVIEWABLE_SOURCE_A, "language": "py"}]
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
    assert "fast review for auth_service.py" in output
    assert "agentic review" not in output


#: Realistic fixture sources. Content must clear the planner's reviewability floor
#: (and score well enough to earn its own model call) for the LLM paths below to
#: be exercised at all.
_REVIEWABLE_SOURCE_A = """\
import hashlib
import requests

TOKEN_SALT = "static-salt"


def fetch_profile(url: str) -> dict:
    response = requests.get(url, verify=False, timeout=10)
    return response.json()


def token_digest(token: str) -> str:
    return hashlib.md5((token + TOKEN_SALT).encode()).hexdigest()


def is_same_token(a: str, b: str) -> bool:
    return a == b
"""

_REVIEWABLE_SOURCE_B = """\
import subprocess


def run_report(path: str) -> str:
    command = f"wc -l {path}"
    completed = subprocess.run(command, shell=True, capture_output=True, text=True)
    return completed.stdout.strip()


def load_settings(payload: str) -> dict:
    import yaml
    return yaml.load(payload)
"""


def test_multi_review_falls_back_to_static_triage_when_provider_review_fails(isolated_data_dir):
    async def failing_stream(file_name, content, language, repo_context="", model_override=""):
        yield "__ERROR__Error code: 402 - Insufficient Balance__ERROR_END__\n"

    files = [{"file_name": "billing_service.py", "content": _REVIEWABLE_SOURCE_A, "language": "py"}]
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
    # A provider's error text must never reach the user's review — it is logged
    # server-side instead. The file still gets a real deterministic report.
    assert "Insufficient Balance" not in output
    assert "Static analysis shown" in output
