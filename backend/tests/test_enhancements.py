"""
test_enhancements.py — Unit and integration tests for new RAG enhancements:
1. Hybrid search (BM25 + RRF)
2. File-scoped tag extraction (@file)
3. Query expansion and context compaction
"""

import pytest
from langchain_core.documents import Document
from app.services.hybrid_retriever import BM25Index, reciprocal_rank_fusion, diversify_documents
from app.services.query_enhancer import extract_file_scope, compact_chat_history, local_query_variants


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
    assert "CodeSage: Run uvicorn" in compacted


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
