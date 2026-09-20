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
DEFAULT_NAME = "savflux"

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


# ── Line-precise citation spans ───────────────────────────────────────────────
# Every chunk carries start_line/end_line so the UI can cite "auth.py:42-58"
# and scroll to the exact evidence. These tests verify the numbers by slicing
# the ORIGINAL source with them — a span that does not round-trip is useless.

def _slice(source: str, doc) -> str:
    """Extract the lines a chunk claims to cover from the original source."""
    lines = source.splitlines()
    return "\n".join(lines[doc.metadata["start_line"] - 1 : doc.metadata["end_line"]])


def test_every_chunk_carries_a_valid_span():
    source = (
        "import os\n"
        "\n"
        "CONSTANT = 1\n"
        "\n"
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "class Beta:\n"
        "    def gamma(self):\n"
        "        return 2\n"
    )
    docs = chunk_python_file(source=source, **BASE_META)
    total_lines = len(source.splitlines())

    assert docs
    for doc in docs:
        start = doc.metadata["start_line"]
        end = doc.metadata["end_line"]
        assert isinstance(start, int) and isinstance(end, int)
        assert 1 <= start <= end <= total_lines, f"{doc.metadata['symbol_name']} {start}-{end}"


def test_span_round_trips_to_the_symbol_it_cites():
    """Slicing the original file with a chunk's span must yield that symbol."""
    source = (
        "import os\n"
        "\n"
        "def alpha():\n"
        "    return 'a'\n"
        "\n"
        "def beta():\n"
        "    return 'b'\n"
        "\n"
        "def gamma():\n"
        "    return 'c'\n"
    )
    docs = chunk_python_file(source=source, **BASE_META)

    for name, expected in (("alpha", "'a'"), ("beta", "'b'"), ("gamma", "'c'")):
        doc = next(d for d in docs if d.metadata["symbol_name"] == name)
        cited = _slice(source, doc)
        assert f"def {name}()" in cited, f"{name} span does not cover its own def"
        assert expected in cited
        # Critically: the span must not bleed into a neighbouring function.
        others = {"alpha", "beta", "gamma"} - {name}
        for other in others:
            assert f"def {other}()" not in cited, f"{name} span leaks into {other}"


def test_decorated_function_span_includes_its_decorators():
    """A reader citing a route handler expects the @app.get line included."""
    source = (
        "import app\n"
        "\n"
        "@app.get('/health')\n"
        "@require_auth\n"
        "def handler():\n"
        "    return {}\n"
    )
    docs = chunk_python_file(source=source, **BASE_META)
    doc = next(d for d in docs if d.metadata["symbol_name"] == "handler")

    cited = _slice(source, doc)
    assert "@app.get('/health')" in cited
    assert "@require_auth" in cited
    # And the span must agree with the text actually indexed, or the citation
    # would point somewhere other than what the LLM was shown.
    assert cited.strip() == doc.page_content.strip()


def test_method_spans_are_rebased_onto_the_original_file():
    """Methods of an oversized class must cite real file lines, not 1-N."""
    filler = "\n".join(f"        x{i} = {i}" for i in range(120))
    source = (
        "import os\n"
        "\n"
        "class Huge:\n"
        '    """A class too large for a single chunk."""\n'
        "    def first(self):\n"
        f"{filler}\n"
        "        return 'first'\n"
        "\n"
        "    def second(self):\n"
        f"{filler}\n"
        "        return 'second'\n"
    )
    docs = chunk_python_file(source=source, **BASE_META)
    methods = [d for d in docs if d.metadata.get("symbol_type") == "method"]
    assert methods, "expected the oversized class to be split by method"

    for doc in methods:
        name = doc.metadata["symbol_name"].split(".")[-1]
        cited = _slice(source, doc)
        assert f"def {name}(self)" in cited, (
            f"{doc.metadata['symbol_name']} cites lines "
            f"{doc.metadata['start_line']}-{doc.metadata['end_line']}, "
            "which do not contain its definition"
        )

    # `second` starts well past line 1 — proves the rebase actually happened.
    second = next(d for d in methods if d.metadata["symbol_name"].endswith(".second"))
    assert second.metadata["start_line"] > 100


def test_windowed_function_chunks_have_distinct_spans():
    """A function split into windows must not have every window cite the same lines."""
    body = "\n".join(f"    value_{i} = compute({i})" for i in range(400))
    source = f"def enormous():\n{body}\n    return value_0\n"

    docs = chunk_python_file(source=source, **BASE_META)
    windows = [d for d in docs if d.metadata["symbol_name"] == "enormous"]
    assert len(windows) > 1, "expected the oversized function to be window-split"

    spans = [(d.metadata["start_line"], d.metadata["end_line"]) for d in windows]
    assert len(set(spans)) == len(spans), "windows share identical spans"
    # Windows advance monotonically through the file.
    assert spans == sorted(spans)
    total_lines = len(source.splitlines())
    for start, end in spans:
        assert 1 <= start <= end <= total_lines


def test_decorator_lines_are_indexed_somewhere():
    """
    Regression: decorator lines must appear in the indexed text.

    `node.lineno` points at the `def`, so slicing from it dropped decorators
    from the symbol chunk. The module-level pass separately treats decorator
    lines as "inside a definition" and skips them. Together, decorator lines
    landed in NO chunk and vanished from the index — searching for a route
    path like "/api/v1/users" returned nothing, and the LLM could not tell a
    route handler from a plain function.
    """
    source = (
        "import app\n"
        "\n"
        '@app.get("/api/v1/users/{user_id}")\n'
        "def read_user(user_id: int):\n"
        "    return db.get(user_id)\n"
        "\n"
        "@celery.task(name='billing.charge')\n"
        "class ChargeTask:\n"
        "    pass\n"
    )
    docs = chunk_python_file(source=source, **BASE_META)
    indexed = "\n".join(d.page_content for d in docs)

    assert "/api/v1/users/{user_id}" in indexed, "route path is not searchable"
    assert "billing.charge" in indexed, "task name is not searchable"
    assert "@app.get" in indexed
    assert "@celery.task" in indexed


# ── Discontinuous module spans ────────────────────────────────────────────────

def test_module_chunk_records_exact_ranges_not_just_the_hull():
    """
    Module-level code is gathered from scattered regions, so a single span
    covers the whole file. Highlighting all of it to point at the imports is
    useless as evidence, so the chunk also carries the precise ranges.
    """
    from app.services.ast_chunker import parse_line_ranges

    source = (
        "import os\n"          # 1
        "import sys\n"         # 2
        "\n"                   # 3
        "def alpha():\n"       # 4
        "    return 1\n"       # 5
        "\n"                   # 6
        "TIMEOUT = 30\n"       # 7
        "\n"                   # 8
        "def beta():\n"        # 9
        "    return 2\n"       # 10
    )
    docs = chunk_python_file(source=source, **BASE_META)
    module = next(d for d in docs if d.metadata["symbol_name"] == "<module>")

    ranges = parse_line_ranges(module.metadata["line_ranges"])
    covered = {n for start, end in ranges for n in range(start, end + 1)}

    # The imports and the module constant are in; the function bodies are not.
    assert {1, 2, 7} <= covered
    assert 5 not in covered, "alpha's body must not be attributed to the module chunk"
    assert 10 not in covered, "beta's body must not be attributed to the module chunk"

    # The hull still exists for consumers that only understand a single span.
    assert module.metadata["start_line"] == 1
    assert module.metadata["end_line"] >= 7


def test_line_range_encoding_round_trips():
    from app.services.ast_chunker import _encode_line_ranges, parse_line_ranges

    assert _encode_line_ranges([]) == ""
    assert _encode_line_ranges([5]) == "5"
    assert _encode_line_ranges([1, 2, 3]) == "1-3"
    assert _encode_line_ranges([1, 2, 3, 8, 9, 20]) == "1-3,8-9,20"
    assert parse_line_ranges("1-3,8-9,20") == [(1, 3), (8, 9), (20, 20)]
    # Malformed segments are skipped rather than raising.
    assert parse_line_ranges("1-3,garbage,7") == [(1, 3), (7, 7)]
    assert parse_line_ranges("") == []
