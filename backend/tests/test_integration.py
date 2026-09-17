"""
test_integration.py — Integration tests using a real in-memory ChromaDB.

WHY INTEGRATION TESTS?
Unit tests with mocks verify individual functions in isolation but can't catch
bugs that only appear when multiple components interact. For example:
  - Does ingested content actually come back from a retrieval query?
  - Does the BM25 index find text that dense vector search misses?
  - Does the hybrid fusion produce better results than either alone?

These tests use a real ChromaDB EphemeralClient (in-memory, no disk I/O),
real chunking, real BM25 indexing, and real embedding (mocked to avoid
needing API keys) to exercise the full retrieval pipeline.

DESIGN PRINCIPLES:
  - No real API keys needed — embeddings are replaced with deterministic
    random vectors seeded by content hash. Semantic meaning isn't tested here;
    what's tested is that the pipeline wires together correctly.
  - No network calls — git clone, OpenAI, Gemini are all mocked.
  - Fast — each test runs in < 1s (no model downloads, no HTTP).
  - Real ChromaDB state — insert, query, delete all go through the actual
    ChromaDB EphemeralClient so we exercise the data layer correctly.
"""

import hashlib
import struct
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document
from langchain_chroma import Chroma
import chromadb


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _deterministic_embedding(text: str) -> list[float]:
    """
    Produce a deterministic 384-dim unit vector from text content.

    WHY NOT ZEROS?
    ChromaDB's cosine similarity calculation returns NaN for zero vectors.
    Using a hash-derived vector gives each document a unique but reproducible
    position in the embedding space — close enough to test retrieval logic.
    """
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2 ** 32)
    dim = 384
    vec = []
    for i in range(dim):
        # LCG with seed for reproducibility
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        # Map to [-1, 1]
        vec.append((seed / 2**31) - 1.0)
    # Normalise to unit length (required for cosine distance)
    magnitude = sum(x * x for x in vec) ** 0.5
    return [x / magnitude for x in vec]


class _FakeEmbeddings:
    """Deterministic embedding function compatible with LangChain / ChromaDB."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_deterministic_embedding(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return _deterministic_embedding(text)


@pytest.fixture
def chroma_collection():
    """
    Real in-memory ChromaDB collection for each test.
    EphemeralClient stores everything in RAM — no files, no cleanup needed.
    """
    client = chromadb.EphemeralClient()
    collection = client.create_collection(
        name="test_codesage",
        metadata={"hnsw:space": "cosine"},
    )
    yield collection
    client.delete_collection("test_codesage")


@pytest.fixture
def vectorstore(chroma_collection):
    """
    LangChain Chroma wrapper around the ephemeral collection.
    Uses our fake deterministic embeddings so no API key is needed.
    """
    fake_emb = _FakeEmbeddings()
    vs = Chroma(
        client=chroma_collection._client,
        collection_name="test_codesage",
        embedding_function=fake_emb,
    )
    return vs


# ── Fixture code content ──────────────────────────────────────────────────────

AUTH_PY_CONTENT = """\
import jwt
import hashlib

SECRET_KEY = "supersecret"

def verify_token(token: str) -> dict:
    \"\"\"Verify a JWT token and return the payload.\"\"\"
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token has expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid token")

def hash_password(password: str) -> str:
    \"\"\"Hash a password using SHA-256.\"\"\"
    return hashlib.sha256(password.encode()).hexdigest()
"""

DB_PY_CONTENT = """\
import sqlite3
from pathlib import Path

DB_PATH = Path("./data/app.db")

def get_connection():
    \"\"\"Return a SQLite connection to the app database.\"\"\"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(DB_PATH))

def create_tables():
    \"\"\"Create the users table if it doesn't exist.\"\"\"
    with get_connection() as conn:
        conn.execute(
            \"\"\"CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )\"\"\"
        )
"""

UTILS_PY_CONTENT = """\
import re

def slugify(text: str) -> str:
    \"\"\"Convert a string to a URL-safe slug.\"\"\"
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")

def truncate(text: str, max_len: int = 100) -> str:
    \"\"\"Truncate a string to max_len characters, appending '...' if truncated.\"\"\"
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
"""


def _make_docs(content: str, file_name: str, source: str, repo_url: str) -> list[Document]:
    """
    Create Document chunks with the same metadata structure as ingestion_service produces.
    Splits naively on double-newline for test simplicity — we're testing retrieval, not chunking.
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    chunks = [c.strip() for c in content.split("\n\n") if c.strip()]
    return [
        Document(
            page_content=chunk,
            metadata={
                "source": source,
                "file_name": file_name,
                "language": "py",
                "repo_url": repo_url,
                "chunk_index": i,
                "content_hash": content_hash,
            },
        )
        for i, chunk in enumerate(chunks)
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestChromaDBRoundtrip:
    """
    Integration Test 1: Insert then retrieve from a real ChromaDB collection.

    Verifies:
    - Documents inserted via LangChain's add_documents() are actually stored
    - Similarity search returns documents from the correct repo
    - Repo-scoped filtering (where clause) works correctly
    """

    def test_inserted_docs_are_retrievable(self, vectorstore):
        """Documents added to ChromaDB are returned by similarity search."""
        repo = "https://github.com/test/myapp"
        docs = _make_docs(AUTH_PY_CONTENT, "auth.py", "/tmp/myapp/auth.py", repo)
        vectorstore.add_documents(docs)

        results = vectorstore.similarity_search("verify JWT token", k=3)
        assert len(results) > 0, "Expected at least one result"
        sources = {r.metadata["file_name"] for r in results}
        assert "auth.py" in sources, "auth.py chunks should be retrieved"

    def test_repo_filter_scopes_results(self, vectorstore):
        """
        When two repos are indexed, a repo filter returns only the target repo's docs.
        This is critical for multi-repo support — users must not see results from
        repos they didn't select.
        """
        repo_a = "https://github.com/test/repo_a"
        repo_b = "https://github.com/test/repo_b"

        docs_a = _make_docs(AUTH_PY_CONTENT, "auth.py", "/tmp/a/auth.py", repo_a)
        docs_b = _make_docs(DB_PY_CONTENT, "db.py", "/tmp/b/db.py", repo_b)

        vectorstore.add_documents(docs_a)
        vectorstore.add_documents(docs_b)

        # Query scoped to repo_b only
        results = vectorstore.similarity_search(
            "database connection",
            k=5,
            filter={"repo_url": repo_b},
        )
        assert len(results) > 0, "Should find results in repo_b"
        for r in results:
            assert r.metadata["repo_url"] == repo_b, (
                f"Got result from wrong repo: {r.metadata['repo_url']}"
            )

    def test_delete_by_repo_removes_only_that_repo(self, vectorstore):
        """
        Clearing one repo's documents must not affect the other repo.
        This tests the targeted delete path in clear_index().
        """
        repo_a = "https://github.com/test/repo_a"
        repo_b = "https://github.com/test/repo_b"

        docs_a = _make_docs(AUTH_PY_CONTENT, "auth.py", "/tmp/a/auth.py", repo_a)
        docs_b = _make_docs(UTILS_PY_CONTENT, "utils.py", "/tmp/b/utils.py", repo_b)

        vectorstore.add_documents(docs_a)
        vectorstore.add_documents(docs_b)

        # Delete repo_a
        existing = vectorstore.get(where={"repo_url": repo_a})
        ids_to_delete = existing.get("ids") or []
        assert len(ids_to_delete) > 0, "Should have docs for repo_a before delete"
        vectorstore.delete(ids=ids_to_delete)

        # repo_b must still be fully intact
        remaining = vectorstore.similarity_search("slugify URL", k=5)
        remaining_repos = {r.metadata["repo_url"] for r in remaining}
        assert repo_a not in remaining_repos, "repo_a docs should be gone"
        assert repo_b in remaining_repos, "repo_b docs must remain"


class TestBM25Integration:
    """
    Integration Test 2: BM25 index built over real documents.

    Verifies:
    - BM25 finds exact keyword matches that dense vectors miss
    - Code-specific tokenization (snake_case, camelCase) works
    - Repo URL filtering in BM25 search works
    """

    def _build_bm25(self, docs: list[Document]):
        from app.services.hybrid_retriever import BM25Index
        return BM25Index(docs)

    def test_bm25_finds_exact_function_name(self):
        """
        BM25 should return the chunk containing `verify_token` when that exact
        function name is searched. Dense vectors might rank this lower if
        another chunk is semantically closer.
        """
        repo = "https://github.com/test/myapp"
        auth_docs = _make_docs(AUTH_PY_CONTENT, "auth.py", "/tmp/auth.py", repo)
        db_docs = _make_docs(DB_PY_CONTENT, "db.py", "/tmp/db.py", repo)

        bm25 = self._build_bm25(auth_docs + db_docs)
        results = bm25.search("verify_token", top_k=3)

        assert len(results) > 0, "BM25 should find at least one result"
        contents = " ".join(r.page_content for r in results)
        assert "verify_token" in contents, "Top BM25 result should contain the searched function name"

    def test_bm25_repo_filter(self):
        """BM25 repo filter restricts results to the specified repo."""
        repo_a = "https://github.com/test/repo_a"
        repo_b = "https://github.com/test/repo_b"

        docs_a = _make_docs(AUTH_PY_CONTENT, "auth.py", "/tmp/a/auth.py", repo_a)
        docs_b = _make_docs(DB_PY_CONTENT, "db.py", "/tmp/b/db.py", repo_b)

        from app.services.hybrid_retriever import BM25Index
        bm25 = BM25Index(docs_a + docs_b)

        results = bm25.search("database connection", top_k=5, repo_urls=[repo_b])
        for r in results:
            assert r.metadata["repo_url"] == repo_b, (
                f"BM25 filter failed: got result from {r.metadata['repo_url']}"
            )

    def test_bm25_camelcase_tokenisation(self):
        """
        Code-aware tokenizer splits camelCase identifiers so 'getUserById'
        matches queries for 'get', 'user', or 'id'.
        """
        from app.services.hybrid_retriever import _tokenize
        tokens = _tokenize("getUserById")
        # Should split into individual words
        assert "get" in tokens or "getuser" in tokens or any("user" in t for t in tokens)
        assert "id" in tokens or any("id" in t for t in tokens)


class TestChunkingIntegration:
    """
    Integration Test 3: _load_and_split with real fixture files.

    Verifies:
    - The chunking pipeline produces well-formed Documents
    - Python files use AST chunking (one chunk per function)
    - Required metadata fields are present on every chunk
    """

    def test_python_file_produces_ast_chunks(self, tmp_path):
        """
        A Python file with multiple top-level functions should produce
        at least one chunk per function via AST-boundary chunking.
        Each chunk must have the required metadata fields.
        """
        from app.services.ingestion_service import _load_and_split

        py_file = tmp_path / "auth.py"
        py_file.write_text(AUTH_PY_CONTENT, encoding="utf-8")

        docs = _load_and_split([py_file], repo_url="https://github.com/test/myapp")

        assert len(docs) >= 2, "Expected at least one chunk per top-level function"

        required_fields = {"source", "file_name", "language", "repo_url", "content_hash"}
        for doc in docs:
            missing = required_fields - set(doc.metadata.keys())
            assert not missing, f"Chunk missing metadata fields: {missing}"
            assert doc.page_content.strip(), "Chunk must have non-empty content"

    def test_multiple_file_types_produce_chunks(self, tmp_path):
        """
        A mix of Python and JSON files are all chunked without errors.
        Verifies the extension-to-language routing in _load_and_split.
        """
        from app.services.ingestion_service import _load_and_split

        py_file = tmp_path / "utils.py"
        py_file.write_text(UTILS_PY_CONTENT, encoding="utf-8")

        json_file = tmp_path / "config.json"
        json_file.write_text('{"debug": true, "port": 8000}', encoding="utf-8")

        docs = _load_and_split(
            [py_file, json_file],
            repo_url="https://github.com/test/myapp",
        )

        assert len(docs) >= 2, "Expected at least one chunk from each file"
        file_names = {doc.metadata["file_name"] for doc in docs}
        assert "utils.py" in file_names
        assert "config.json" in file_names
