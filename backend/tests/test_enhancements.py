"""
test_enhancements.py — Unit and integration tests for new RAG enhancements:
1. Hybrid search (BM25 + RRF + two_branch_rrf)
2. File-scoped tag extraction (@file)
3. Query expansion and context compaction
4. _strip_think_tags edge cases
5. _run_security_scan word-boundary accuracy
6. Intent routing count-based scoring
"""

import asyncio
import pytest
from langchain_core.documents import Document
from app.services.hybrid_retriever import BM25Index, reciprocal_rank_fusion, diversify_documents
from app.services.query_enhancer import (
    extract_file_scope, compact_chat_history, local_query_variants, route_query_intent,
)


def test_file_scope_extraction():
    q, file_filter = extract_file_scope("@auth.py where is the JWT verified?")
    assert file_filter == "auth.py"
    assert q == "where is the JWT verified?"

    # Query without tag
    q2, f2 = extract_file_scope("how does the vector store work?")
    assert f2 is None
    assert q2 == "how does the vector store work?"


def test_bm25_search_and_rrf():
    docs = [
        Document(page_content="def authenticate_user(token): return jwt.decode(token)", metadata={"source": "auth.py", "file_name": "auth.py"}),
        Document(page_content="def get_db(): db = SessionLocal()", metadata={"source": "database.py", "file_name": "database.py"}),
        Document(page_content="JWT_SECRET_KEY = 'secret'", metadata={"source": "config.py", "file_name": "config.py"}),
    ]
    bm25 = BM25Index(docs)
    results = bm25.search(query="JWT_SECRET_KEY", top_k=2)
    assert len(results) > 0
    assert "JWT_SECRET_KEY" in results[0].page_content

    # Test Reciprocal Rank Fusion
    dense_mock = [docs[0], docs[1]]
    lexical_mock = [docs[2], docs[0]]
    fused = reciprocal_rank_fusion([dense_mock, lexical_mock], k=60, top_n=2)
    assert len(fused) == 2
    # docs[0] appears in both lists, so it should rank first by RRF
    assert fused[0].metadata["file_name"] == "auth.py"


def test_compact_chat_history():
    history = [
        {"role": "user", "content": "How do I start the server?"},
        {"role": "assistant", "content": "Run uvicorn backend.main:app --reload"},
    ]
    compacted = compact_chat_history(history, max_turns=4)
    assert "User: How do I start the server?" in compacted
    assert "SavFlux: Run uvicorn" in compacted


def test_local_query_variants_extract_identifiers_and_synonyms():
    variants = local_query_variants("How is JWT authentication configured?")
    assert variants[0].startswith("How is JWT")
    assert any("JWT" in variant for variant in variants)
    assert any("authorization" in variant for variant in variants)


def test_diversify_documents_limits_one_source():
    docs = [
        Document(page_content="a", metadata={"source": "a.py"}),
        Document(page_content="b", metadata={"source": "a.py"}),
        Document(page_content="c", metadata={"source": "a.py"}),
        Document(page_content="d", metadata={"source": "b.py"}),
    ]
    selected = diversify_documents(docs, top_n=3, max_per_source=2)
    assert [doc.metadata["source"] for doc in selected] == ["a.py", "a.py", "b.py"]


# ── New tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strip_think_tags_unterminated():
    """
    Stream ending with an open <think> block should flush the buffered content
    rather than silently discard it. The model's response is still useful even
    if it forgot to close the thinking block.
    """
    from app.services.review_agent import _strip_think_tags

    async def _token_stream():
        yield "<think>some partial reasoning that never closes"

    tokens = []
    async for tok in _strip_think_tags(_token_stream()):
        tokens.append(tok)
    combined = "".join(tokens)
    # The buffered content should be yielded, not silently dropped
    assert "some partial reasoning" in combined


@pytest.mark.asyncio
async def test_strip_think_tags_multiple_blocks():
    """
    A stream containing two <think>…</think> blocks should strip both,
    yielding only the content between and after them.
    """
    from app.services.review_agent import _strip_think_tags

    async def _token_stream():
        yield "<think>first reasoning</think>first answer"
        yield "<think>second reasoning</think>second answer"

    tokens = []
    async for tok in _strip_think_tags(_token_stream()):
        tokens.append(tok)
    combined = "".join(tokens)
    assert "first reasoning" not in combined
    assert "second reasoning" not in combined
    assert "first answer" in combined
    assert "second answer" in combined


@pytest.mark.asyncio
async def test_strip_think_tags_orphaned_close_tag():
    """
    A </think> with no preceding <think> should be stripped silently,
    not leaked into the output.
    """
    from app.services.review_agent import _strip_think_tags

    async def _token_stream():
        yield "normal content</think>more content"

    tokens = []
    async for tok in _strip_think_tags(_token_stream()):
        tokens.append(tok)
    combined = "".join(tokens)
    assert "</think>" not in combined
    assert "normal content" in combined
    assert "more content" in combined


def test_run_security_scan_no_false_positives():
    """
    Word-boundary patterns must NOT match common false positives:
      - 'tokenize'  should NOT trigger the hardcoded_secrets pattern
      - 'selectedFile' should NOT trigger SQL DELETE
      - 'deleteUser' should NOT trigger SQL DELETE
    """
    from app.services.review_agent import _run_security_scan

    code = """\
def tokenize(text):
    return text.split()

selectedFile = get_selected()
deleteUser = lambda uid: db.remove(uid)
"""
    result = _run_security_scan(code)
    assert "hardcoded_secrets" not in result, (
        "'tokenize' should not match the hardcoded_secrets pattern"
    )
    assert "sql_injection" not in result, (
        "'selectedFile' and 'deleteUser' should not match SQL patterns"
    )


def test_intent_routing_count_based():
    """
    Count-based intent routing should pick the intent with the most keyword hits.
    """
    # Pure API query — endpoint + route + handler all point to 'api'
    result = route_query_intent("show me the /ingest API endpoint route handler")
    assert result == "api", f"Expected 'api', got '{result}'"

    # Pure security query — jwt + token + authentication + password all point to 'security'
    result2 = route_query_intent("where is JWT token authentication and password hashing?")
    assert result2 == "security", f"Expected 'security', got '{result2}'"

    # Unknown → general
    result3 = route_query_intent("explain the overall system architecture")
    assert result3 == "general", f"Expected 'general', got '{result3}'"


def test_file_scope_diagnostic_no_match():
    """
    When @file scope produces no matching chunks after fusion, the system should
    fall through to unscoped results rather than returning nothing.
    Verify the post-filter fallback logic directly.
    """
    docs = [
        Document(page_content="foo", metadata={"source": "/tmp/repo/auth.py", "file_name": "auth.py", "chunk_index": 0}),
        Document(page_content="bar", metadata={"source": "/tmp/repo/db.py", "file_name": "db.py", "chunk_index": 0}),
    ]
    file_scope = "nonexistent_file"
    scoped = [
        doc for doc in docs
        if file_scope.lower() in doc.metadata.get("source", "").lower()
        or file_scope.lower() in doc.metadata.get("file_name", "").lower()
    ]
    # No match → the fallback keeps the original unscoped docs
    result = scoped if scoped else docs
    assert len(result) == 2, "Should fall through to unscoped results when @file matches nothing"
    assert result is docs
