"""
test_agent_prompts_analytics.py — Unit tests for the restored P0 modules.

Pure service-level tests (no TestClient, no LLM, no ChromaDB):
  - prompt_service: CRUD + history + clear hooks used by activity_service
  - analytics_service: record_latency / summary / clear hooks used by main.py
  - agent router: tool catalogue shape + request validation

Each test redirects the JSON stores to tmp_path so the real chroma_data
directory is never touched.
"""

import json

import pytest


@pytest.fixture()
def _isolated_storage(isolated_data_dir, monkeypatch):
    """Prompt + analytics stores redirected to a tmp dir (see conftest)."""
    import app.services.token_counter as tc

    monkeypatch.setattr(tc, "get_totals", lambda: {"total_tokens": 10, "llm_calls": 2})
    return isolated_data_dir


def test_prompt_crud_and_use(_isolated_storage):
    from app.services.prompt_service import (
        create_prompt, list_prompts, use_prompt, delete_prompt,
        list_history, clear_history, clear_prompts,
    )
    p = create_prompt("Explain @file in one paragraph", title="Explain", kind="chat",
                      tags=["docs"])
    assert p["id"] and p["use_count"] == 0
    assert len(list_prompts()) == 1
    assert len(list_prompts(kind="review")) == 0

    used = use_prompt(p["id"])
    assert used and used["use_count"] == 1
    assert len(list_history()) == 1  # use() records a history row

    assert use_prompt("missing") is None
    assert delete_prompt(p["id"]) is True
    assert delete_prompt(p["id"]) is False

    assert clear_history() == 1
    assert clear_history() == 0
    assert clear_prompts() == 0


def test_prompt_validation(_isolated_storage):
    from app.services.prompt_service import create_prompt
    with pytest.raises(ValueError):
        create_prompt("   ")
    with pytest.raises(ValueError):
        create_prompt("x" * 4001)


def test_prompt_activity_contract(_isolated_storage):
    """activity_service reads the prompt library file — keep the schema stable."""
    from app.services.prompt_service import create_prompt, record_history, _library_path
    create_prompt("Summarise this diff", kind="review")
    record_history("ad hoc question", kind="chat")
    # Ask the owning service where its file lives rather than rebuilding the
    # path here — that is the same contract activity_service now relies on.
    data = json.loads(_library_path().read_text())
    assert set(data.keys()) == {"prompts", "history"}
    assert isinstance(data["prompts"][0]["created_at"], float)
    assert isinstance(data["history"][0]["ts"], float)


def test_analytics_record_and_summary(_isolated_storage):
    from app.services.analytics_service import (
        record_latency, get_history, get_summary, clear_history,
    )
    record_latency("/api/v1/chat/stream", 120.5, 200)
    record_latency("/api/v1/chat/stream", 80.0, 200)
    record_latency("/api/v1/review/file", 900.0, 500)

    rows = get_history()
    assert len(rows) == 3
    assert rows[0]["total_tokens"] == 10  # snapshot from token_counter
    assert rows[0]["avg_latency_ms"] > 0

    summary = get_summary()
    assert summary["total_requests"] == 3
    assert summary["errors"] == 1
    chat = next(e for e in summary["endpoints"] if "chat" in e["path"])
    assert chat["count"] == 2
    assert chat["avg_latency_ms"] == pytest.approx(100.25)

    assert clear_history() == 3
    assert get_history() == []


def test_analytics_record_never_raises(_isolated_storage, monkeypatch):
    import app.services.analytics_service as ans
    monkeypatch.setattr(ans, "_history_path", lambda: (_ for _ in ()).throw(OSError("disk gone")))
    ans.record_latency("/x", 1.0, 200)  # must not raise (called from middleware)


def test_agent_tool_catalogue_shape():
    from app.api.agent import TOOL_CATALOGUE
    names = {t["name"] for t in TOOL_CATALOGUE}
    assert {"retrieve_context", "read_file", "dependency_graph", "blast_radius"} <= names
    for tool in TOOL_CATALOGUE:
        assert tool["description"] and isinstance(tool["args"], list)


def test_agent_request_validation():
    from pydantic import ValidationError
    from app.api.agent import AgentRunRequest
    with pytest.raises(ValidationError):
        AgentRunRequest(goal="   ")
    req = AgentRunRequest(goal="  find auth  ", max_steps=99)
    assert req.goal == "find auth"
    assert req.max_steps == 12  # clamped
