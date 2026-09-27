"""
test_review.py — Tests for the code review API.

Covers the two most critical security + correctness properties:
  1. Path traversal guard — /etc/passwd must be rejected
  2. Empty paste returns 400, not 500
  3. Oversized paste returns 413
  4. Valid paste starts streaming (SSE format check)
"""

import inspect

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def test_review_file_rejects_path_traversal(client):
    """
    SECURITY: file_path=/etc/passwd must return 422 (validation error),
    not 200 (file contents) or 500 (crash).

    This is the path traversal vulnerability we explicitly fixed.
    Without the allowed-roots validator, this would return the contents
    of any file accessible to the server process.
    """
    response = client.post(
        "/api/v1/review/file",
        json={
            "file_path": "/etc/passwd",
            "file_name": "passwd",
            "language": "",
        },
    )
    # 422 = Pydantic validation rejected the path
    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


def test_review_file_rejects_path_prefix_bypass(client):
    """A sibling path sharing the /tmp prefix must not pass validation."""
    response = client.post(
        "/api/v1/review/file",
        json={
            "file_path": "/tmp-attacker/secret.py",
            "file_name": "secret.py",
            "language": "python",
        },
    )
    assert response.status_code == 422


def test_review_paste_empty_code_returns_400(client):
    """
    Empty code paste returns 400 with a clear error message.
    Without this guard, stream_code_review would receive empty content
    and the LLM would write a review of nothing.
    """
    response = client.post(
        "/api/v1/review/paste",
        json={"code": "   ", "file_name": "test.py", "language": "python"},
    )
    assert response.status_code == 400
    assert "No code" in response.json()["detail"]


def test_review_paste_oversized_code_returns_413(client):
    """
    Pasting 50,001 characters should return 413 (Payload Too Large).

    WHY THIS LIMIT?
    50k chars ≈ 12k tokens. GPT-4o's context window is 128k tokens, but
    the review prompt + tool results easily consume another 8k. Setting a
    hard limit here prevents accidental 40k-token requests that cost $0.60 each.
    """
    oversized = "x" * 50_001
    response = client.post(
        "/api/v1/review/paste",
        json={"code": oversized, "file_name": "big.py", "language": "python"},
    )
    assert response.status_code == 413


def test_review_paste_valid_code_streams(client):
    """
    Happy path: valid code returns a streaming response.
    We don't check the exact content (that's the LLM's job) —
    we check that the response starts and contains our STATUS marker format.

    WHY MOCK stream_fast_code_review?
    /review/paste now uses fast mode by default (1 LLM call, same as multi-file).
    We patch stream_fast_code_review — the function actually called — not the
    agentic stream_code_review which is only used when REVIEW_MODE=agentic.
    """
    async def fake_stream(*args, **kwargs):
        yield '__STATUS__Scanning `test.py`...{"step": "starting", "mode": "fast"}__STATUS_END__\n'
        yield "## 🐛 Bugs & Risks\nNone found.\n"

    with patch(
        "app.api.review.stream_fast_code_review",
        side_effect=fake_stream,
    ):
        response = client.post(
            "/api/v1/review/paste",
            json={"code": "def hello(): pass", "file_name": "test.py", "language": "python"},
        )

    assert response.status_code == 200
    # Streaming response should have text/plain content type
    assert "text/plain" in response.headers.get("content-type", "")
    body = response.text
    assert "__STATUS__" in body
    assert "Bugs & Risks" in body


# ── Stop handshake: the routes must forward the reader's disconnect ────────────
#
# The services take a `should_stop` predicate and refuse to spend a model call once
# it says the reader is gone — but that is dead code unless the route hands them the
# one signal FastAPI has for it. These three tests pin the wiring, because a
# cancellation that exists only in the service layer has never been observed by a
# user: the button works exactly as far as the request goes.

def _disconnect_probe(client, url: str, payload: dict, target: str):
    """Call `url` with a captured streaming function and return what it received."""
    seen: dict = {}

    async def fake(*args, **kwargs):
        seen.update(kwargs)
        if False:
            yield ""

    with patch(target, side_effect=fake):
        response = client.post(url, json=payload)

    assert response.status_code == 200, response.text
    return seen


def test_review_paste_forwards_the_readers_disconnect(client):
    """`/review/paste` must pass `should_stop`, or Stop only stops the printing."""
    seen = _disconnect_probe(
        client, "/api/v1/review/paste",
        {"code": "def hello(): pass", "file_name": "test.py", "language": "python"},
        "app.api.review.stream_fast_code_review",
    )

    assert callable(seen.get("should_stop")), f"no predicate forwarded: {sorted(seen)}"
    # Not just any callable: it has to be the async disconnect check, since awaiting
    # a sync callable inside the stream would block the event loop the stream runs on.
    assert inspect.iscoroutinefunction(seen["should_stop"])


def test_review_file_forwards_the_readers_disconnect(client, tmp_path):
    """`/review/file` shares the call site, so it shares the guarantee."""
    target = tmp_path / "sample.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    seen = _disconnect_probe(
        client, "/api/v1/review/file",
        {"file_path": str(target), "file_name": "sample.py", "language": "python"},
        "app.api.review.stream_fast_code_review",
    )

    assert callable(seen.get("should_stop"))


def test_review_multi_forwards_the_readers_disconnect(client, tmp_path):
    """
    The repo-wide review is where cancellation pays: N files, N model calls.

    The multi route had no `Request` in hand for its streaming body at all — the
    disconnect is threaded through `stream_multi_review`, and this is what catches a
    refactor that drops the keyword while the service keeps its parameter.
    """
    first = tmp_path / "a.py"
    first.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    seen = _disconnect_probe(
        client, "/api/v1/review/multi",
        {"files": [{"file_path": str(first), "file_name": "a.py", "language": "py"}]},
        "app.api.review.stream_multi_review",
    )

    assert callable(seen.get("should_stop"))
