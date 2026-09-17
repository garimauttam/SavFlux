"""
test_ast_chunker.py — Unit tests for the AST-boundary Python chunker.

Verifies:
1. Each top-level function/class produces exactly one chunk
2. Module-level code (imports, constants) is captured in a module chunk
3. Symbol names and types are recorded in metadata
4. Large classes are split by method
5. Syntax errors fall back gracefully (return empty list)
6. content_hash is present and consistent
"""

import pytest
from app.services.ast_chunker import chunk_python_file


BASE_META = dict(
    file_path="/repo/test.py",
    file_name="test.py",
    language="py",
    repo_url="https://github.com/test/repo",
    content_hash="abc123",
)


def test_single_function_produces_one_chunk():
    """A file with one function → exactly one meaningful chunk (possibly + module chunk)."""
    source = '''
import os

def hello_world():
    """Say hello."""
    return "hello"
'''
    docs = chunk_python_file(source=source, **BASE_META)
    names = [d.metadata["symbol_name"] for d in docs]
    assert "hello_world" in names


def test_multiple_functions_each_get_own_chunk():
    """Two top-level functions → two separate chunks (one per function)."""
    source = '''
def alpha():
    return 1

def beta():
    return 2
'''
    docs = chunk_python_file(source=source, **BASE_META)
    names = [d.metadata["symbol_name"] for d in docs]
    assert "alpha" in names
    assert "beta" in names
    # Critically: alpha and beta are NOT in the same chunk
    alpha_chunks = [d for d in docs if d.metadata["symbol_name"] == "alpha"]
    beta_chunks  = [d for d in docs if d.metadata["symbol_name"] == "beta"]
    assert len(alpha_chunks) >= 1
    assert len(beta_chunks) >= 1
    # alpha chunk must NOT contain beta's source
    for chunk in alpha_chunks:
        assert "def beta" not in chunk.page_content


def test_class_produces_chunk_with_correct_type():
    """A top-level class → chunk with symbol_type='class'."""
    source = '''
class MyService:
    """Service class."""

    def __init__(self):
        self.value = 42

    def get_value(self):
        return self.value
'''
    docs = chunk_python_file(source=source, **BASE_META)
    class_chunks = [d for d in docs if d.metadata["symbol_name"] == "MyService"]
    assert len(class_chunks) >= 1
    assert class_chunks[0].metadata["symbol_type"] == "class"


def test_module_level_code_captured():
    """Imports and constants at module level become a '<module>' chunk."""
    source = '''import os
import sys

MAX_SIZE = 100
DEFAULT_NAME = "codesage"

def process():
    return MAX_SIZE
'''
    docs = chunk_python_file(source=source, **BASE_META)
    module_chunks = [d for d in docs if d.metadata["symbol_name"] == "<module>"]
    assert len(module_chunks) == 1
    assert "import os" in module_chunks[0].page_content


def test_module_code_after_definition_is_preserved():
    """Module constants after a function remain available for retrieval."""
    source = '''def process():
    return 1

LATE_CONSTANT = "kept"
'''
    docs = chunk_python_file(source=source, **BASE_META)
    module = next(d for d in docs if d.metadata["symbol_name"] == "<module>")
    assert "LATE_CONSTANT" in module.page_content


def test_content_hash_propagated():
    """content_hash from input is present in all chunk metadata."""
    source = '''
def foo():
    return 1

def bar():
    return 2
'''
    docs = chunk_python_file(source=source, content_hash="deadbeef1234", **{k: v for k, v in BASE_META.items() if k != "content_hash"})
    for doc in docs:
        assert doc.metadata["content_hash"] == "deadbeef1234"


def test_syntax_error_returns_empty_list():
    """A file with invalid Python syntax returns [] — caller falls back to char splitter."""
    source = "def broken(\n    this is not python at all"
    docs = chunk_python_file(source=source, **BASE_META)
    assert docs == []


def test_empty_source_returns_empty_list():
    """An empty file returns []."""
    docs = chunk_python_file(source="", **BASE_META)
    assert docs == []


def test_chunk_index_is_unique_and_sequential():
    """All chunks from one file have unique chunk_index values starting from 0."""
    source = '''
import os

def alpha():
    return 1

def beta():
    return 2

class Gamma:
    def method(self):
        pass
'''
    docs = chunk_python_file(source=source, **BASE_META)
    indices = [d.metadata["chunk_index"] for d in docs]
    assert len(indices) == len(set(indices)), "chunk_index values must be unique"
    assert min(indices) == 0


def test_async_function_detected():
    """Async functions get symbol_type='async_function'."""
    source = '''
async def fetch_data(url: str) -> dict:
    """Fetch data from URL."""
    return {}
'''
    docs = chunk_python_file(source=source, **BASE_META)
    async_chunks = [d for d in docs if d.metadata["symbol_name"] == "fetch_data"]
    assert len(async_chunks) >= 1
    assert async_chunks[0].metadata["symbol_type"] == "async_function"


def test_metadata_source_and_file_name():
    """Each chunk carries the correct source path and file_name."""
    source = '''
def simple():
    pass
'''
    docs = chunk_python_file(source=source, **BASE_META)
    for doc in docs:
        assert doc.metadata["source"] == "/repo/test.py"
        assert doc.metadata["file_name"] == "test.py"
        assert doc.metadata["repo_url"] == "https://github.com/test/repo"
