"""
Tests for grounding_check.

The cases that matter are the ones seen in a real run on a local 14B model, not
invented ones: a citation to a file that was never in the context, a citation
to the right file at lines that were never shown, and a model that declares the
evidence insufficient while citing files that were in it.
"""

from __future__ import annotations

import json

import pytest

from app.services.grounding_check import (
    check_grounding,
    format_grounding_notice,
)


def _sources(*specs):
    """Build the `__SOURCES__` shape the chat path actually streams.

    Each spec is `(file_name, start_line, end_line)` plus an optional fourth
    element of extra keys to merge in.
    """
    out = []
    for spec in specs:
        file_name, start, end = spec[:3]
        extra = spec[3] if len(spec) > 3 else {}
        entry = {
            "file_name": file_name,
            "source": f"local://x::{file_name}",
            "language": "python",
            "trust_level": "high",
            "trust_score": 0.9,
            "chunk_count": 1,
            "start_line": start,
            "end_line": end,
        }
        entry.update(extra)
        out.append(entry)
    return out


SERVICES = _sources(
    ("README.md", 175, 380),
    ("test_embedding_config.py", 67, 85),
    ("test_index_build_identity.py", 100, 116),
    ("eval_rag.py", 331, 392),
)


# ── the real failure from the local 14B run ────────────────────────────────

def test_a_citation_to_a_file_that_was_never_retrieved_is_flagged():
    answer = (
        "The embedding config is validated in `test_embedding_config.py:70-80`.\n\n"
        "- ⚠️ **Line 76–83** in `test_tree_sitter_langs.py`: this would break.\n"
    )
    report = check_grounding(answer, SERVICES)

    assert [c.file_name for c in report.unverified] == ["test_tree_sitter_langs.py"]
    assert report.unverified[0].reason == "not_in_context"
    assert not report.is_clean


def test_the_citations_that_were_real_are_left_alone():
    answer = "See `test_embedding_config.py:70-80` and `eval_rag.py:331-392`."
    report = check_grounding(answer, SERVICES)

    assert report.unverified == []
    assert report.citations_claimed == 2
    assert report.is_clean


def test_an_answer_that_declined_and_cited_nothing_else_is_reported():
    answer = (
        "To investigate further: the provided code snippets do not contain enough "
        "information to fully determine what would break."
    )
    report = check_grounding(answer, SERVICES)

    assert report.insufficient_evidence
    assert not report.delivered_anyway
    assert not report.is_clean
    assert "not enough to answer" in format_grounding_notice(report)


def test_declining_but_still_answering_is_not_called_a_failure():
    """The prompt asks for this. A model that hedges and then answers is correct."""
    answer = (
        "The snippets do not contain enough information to cover every caller, but "
        "here is what they do show:\n\n"
        "- `test_embedding_config.py:67-85` validates the model name.\n"
    )
    report = check_grounding(answer, SERVICES)

    assert report.insufficient_evidence
    assert report.delivered_anyway
    assert report.is_clean


# ── line ranges ───────────────────────────────────────────────────────────

def test_lines_beyond_the_served_span_are_flagged():
    answer = "Defined in `eval_rag.py:900-950`."
    report = check_grounding(answer, SERVICES)

    assert report.unverified[0].reason == "lines_outside_context"
    assert "331-392" in report.unverified[0].detail


def test_a_single_line_inside_the_span_is_accepted():
    assert check_grounding("see `eval_rag.py:350`", SERVICES).unverified == []


def test_discontinuous_line_ranges_are_respected():
    """A module chunk shows imports plus scattered constants, not a solid hull."""
    services = _sources(("mod.py", 1, 200, {"line_ranges": "1-30,88-92"}))

    assert check_grounding("`mod.py:90-92`", services).unverified == []
    # 40-60 is inside the 1-200 hull but was never actually shown.
    assert check_grounding("`mod.py:40-60`", services).unverified[0].reason == (
        "lines_outside_context"
    )


def test_a_source_with_no_line_information_is_not_called_a_fabrication():
    services = [{"file_name": "notes.md", "source": "local://x::notes.md"}]
    report = check_grounding("per `notes.md:12`", services)

    # Reporting this would blame the model for a gap in our own metadata.
    assert report.unverified == []
    assert report.citations_claimed == 1


def test_a_reversed_range_is_read_as_written_not_as_a_miss():
    assert check_grounding("`eval_rag.py:392-331`", SERVICES).unverified == []


# ── not flagging things that are not citations ────────────────────────────

@pytest.mark.parametrize(
    "answer",
    [
        "This is unrelated to a file, e.g. 1.5 seconds and 2.3 MB.",
        "See https://example.com/docs/page:1 for background.",
        "The version is 3:2 in the ratio sense, not a line reference.",
        "No citations at all here, just prose.",
    ],
)
def test_text_that_is_not_a_citation_is_not_matched(answer):
    report = check_grounding(answer, SERVICES)

    assert report.unverified == []
    assert report.citations_claimed == 0


def test_a_bare_filename_without_lines_is_allowed_by_the_prompt():
    assert check_grounding("It lives in `test_embedding_config.py`", SERVICES).is_clean


def test_path_prefixed_citations_resolve_to_their_basename():
    assert check_grounding("`app/api/prompts.py:1-5`", _sources(("prompts.py", 1, 9))).is_clean


# ── edges ─────────────────────────────────────────────────────────────────

def test_an_empty_answer_is_clean_rather_than_an_error():
    assert check_grounding("", SERVICES).is_clean
    assert check_grounding("   \n ", SERVICES).is_clean


def test_no_sources_means_every_citation_is_out_of_context():
    report = check_grounding("see `a.py:1-2`", [])

    assert report.unverified[0].reason == "not_in_context"
    assert "0 file(s)" in report.unverified[0].detail


def test_the_same_citation_twice_is_reported_once():
    report = check_grounding("`ghost.py:1-2` and again `ghost.py:1-2`", SERVICES)

    assert len(report.unverified) == 1


def test_the_notice_names_the_files_rather_than_saying_something_vague():
    report = check_grounding("`ghost.py:1-2` and `phantom.py:3`", SERVICES)
    notice = format_grounding_notice(report)

    assert "ghost.py" in notice
    assert "phantom.py" in notice
    assert "Answer check" in notice


def test_a_backticked_citation_to_a_missing_file_is_still_caught():
    """
    Regression: a fallback that stripped the matched text left an empty string,
    and `"anything".endswith("")` is True — so every source matched and the
    not-in-context check silently stopped firing for backticked citations,
    which is how models normally write them.
    """
    report = check_grounding("see `ghost.py:1-2` for the change", SERVICES)

    assert [c.file_name for c in report.unverified] == ["ghost.py"]
    assert not report.is_clean


def test_both_notations_report_the_same_normalised_reference():
    colon = check_grounding("`eval_rag.py:331-392`", SERVICES)
    written = check_grounding("**Lines 331-392** in `eval_rag.py`", SERVICES)

    assert colon.citations_claimed == written.citations_claimed == 1
    assert check_grounding("`eval_rag.py:900-950`", SERVICES).unverified[0].reference == (
        "eval_rag.py:900-950"
    )


# ── the wire: does the check actually reach the reader? ────────────────────
#
# Everything above tests `check_grounding` as a function. If the chat stream
# never called it, every one of those tests would still pass and the reader
# would still be shown a fabricated citation as though it were evidence. This
# drives the real `stream_answer` the way production does.


class _FakeRetriever:
    def __init__(self, docs):
        self._docs = docs

    async def ainvoke(self, query, **kwargs):
        return list(self._docs)


class _FakeBM25:
    """Empty results, so the dense leg supplies all the context."""

    def search(self, query, top_k=15, repo_urls=None, file_filter=None):
        return []


class _FakeCollectionCount:
    def count(self):
        return 1


class _FakeVectorstore:
    def __init__(self, docs):
        self._docs = docs
        self._collection = _FakeCollectionCount()

    def as_retriever(self, **kwargs):
        return _FakeRetriever(self._docs)


class _CitingLLM:
    """Streams a fixed reply, the way a model that ignored the prompt would."""

    def __init__(self, reply):
        self._reply = reply

    def with_config(self, **kwargs):
        return self

    async def astream(self, messages):
        yield type("Chunk", (), {"content": self._reply})()


def _serve(monkeypatch, reply, doc_name="real.py"):
    from langchain_core.documents import Document

    from app.services import retrieval_service as rs

    doc = Document(
        page_content="def verify(token):\n    return bool(token)\n",
        metadata={
            "source": f"https://github.com/o/r::{doc_name}",
            "file_name": doc_name,
            "language": "python",
            "start_line": 1,
            "end_line": 2,
        },
    )
    monkeypatch.setattr(rs, "_get_vectorstore", lambda: _FakeVectorstore([doc]))
    monkeypatch.setattr(rs, "_get_bm25_index", lambda store: _FakeBM25())
    monkeypatch.setattr(rs, "_get_dep_graph_hints", lambda *a, **k: [])

    async def _passthrough_rerank(query, docs, top_n=5):
        return list(docs)

    monkeypatch.setattr(rs, "rerank", _passthrough_rerank)
    monkeypatch.setattr(rs, "get_chat_llm", lambda **kwargs: _CitingLLM(reply))
    return rs


@pytest.mark.asyncio
async def test_the_stream_carries_a_report_naming_the_fabricated_citation(monkeypatch):
    reply = (
        "Verified in `real.py:1-2`, and also broken in `never_shown.py:76-83`."
    )
    rs = _serve(monkeypatch, reply)

    pieces = [
        piece
        async for piece in rs.stream_answer("where is it verified?", chat_history=[])
    ]
    body = "".join(pieces)

    assert "__GROUNDING__" in body, "the reader is never told what was checked"
    report = json.loads(
        body.split("__GROUNDING__", 1)[1].split("__GROUNDING_END__", 1)[0]
    )
    assert [c["file_name"] for c in report["unverified"]] == ["never_shown.py"]
    assert report["unverified_count"] == 1
    assert report["is_clean"] is False
    # The answer itself still reaches the reader: the check reports, it does not
    # hide an answer the reader might still be able to use.
    assert "Verified in `real.py:1-2`" in body


@pytest.mark.asyncio
async def test_a_fully_grounded_answer_still_reports_that_it_was_checked(monkeypatch):
    """
    The marker is emitted even when clean, so the UI can tell "checked, nothing
    wrong" from "the check never ran". Those are different states and showing
    nothing for both is how a check quietly stops existing.
    """
    rs = _serve(monkeypatch, "Verified in `real.py:1-2`.")

    body = "".join(
        [
            piece
            async for piece in rs.stream_answer("where is it verified?", chat_history=[])
        ]
    )

    assert "__GROUNDING__" in body
    report = json.loads(
        body.split("__GROUNDING__", 1)[1].split("__GROUNDING_END__", 1)[0]
    )
    assert report["is_clean"] is True
    assert report["citations_claimed"] == 1


def test_a_clean_answer_produces_no_notice():
    assert format_grounding_notice(check_grounding("ok `eval_rag.py:331-332`", SERVICES)) == ""


def test_the_written_out_citation_form_is_also_checked():
    """`Line 76-83 in file.py` is the same claim as `file.py:76-83`."""
    answer = "- ⚠️ **Line 76–83** in `test_tree_sitter_langs.py`: this would break."
    report = check_grounding(answer, SERVICES)

    assert [c.file_name for c in report.unverified] == ["test_tree_sitter_langs.py"]
    assert report.unverified[0].line_start == 76


def test_the_written_out_form_checks_the_line_range_too():
    answer = "**Lines 900-950** in `eval_rag.py` define the gate."
    report = check_grounding(answer, SERVICES)

    assert report.unverified[0].reason == "lines_outside_context"


def test_the_written_out_form_accepts_a_range_inside_the_evidence():
    report = check_grounding("**Lines 331-392** in `eval_rag.py`", SERVICES)

    assert report.unverified == []
    assert report.is_clean


def test_the_two_notation_of_one_claim_are_not_counted_twice():
    answer = "See `eval_rag.py:331-392`; **Lines 331-392** in `eval_rag.py`."
    report = check_grounding(answer, SERVICES)

    assert report.citations_claimed == 1
    assert report.is_clean


def test_a_version_number_is_not_a_line_citation():
    report = check_grounding("version 3 in config.py is fine", SERVICES)

    assert report.citations_claimed == 0
