"""
ast_chunker.py — AST-boundary code chunking for Python files.

WHY AST CHUNKING?
RecursiveCharacterTextSplitter splits on character count, which often:
- Cuts a function in half across two chunks
- Puts a class docstring in one chunk and its methods in another
- Merges unrelated helper functions into the same chunk

This hurts retrieval precision: "how does verify_token work?" returns a chunk
containing verify_token's first 300 lines + the start of the next function.
The LLM sees noise; the reranker can't distinguish signal from the unrelated code.

AST-boundary chunking solves this:
- Each top-level function or class definition becomes exactly one chunk
- Module-level code (imports, constants) becomes one chunk
- Functions/classes that are too large for the embedding model are split by
  their inner methods/nested functions

RESULT:
- "how does verify_token work?" → retrieves exactly the verify_token chunk
- No unrelated code noise in the chunk
- `symbol_name` metadata enables precise citation ("In `auth.py::verify_token`:")

LINE-PRECISE CITATIONS:
Every chunk also carries `start_line` / `end_line` (1-indexed, inclusive) —
the exact span in the ORIGINAL file the chunk was cut from. That is what turns
a file-level citation ("auth.py") into evidence ("auth.py:42-58"), and what
lets the UI scroll to and highlight the cited lines. Computing it here is free:
the AST already knows every node's line range.

LIMITATIONS:
- Python only — other languages use RecursiveCharacterTextSplitter as before
- Decorated functions: the decorator is included in the chunk (correct behaviour)
- Very large classes (>MAX_CHUNK_CHARS) are split by inner methods; the class
  docstring and class body are prepended to each inner method chunk as context
"""

import ast
import textwrap
from typing import List
from langchain_core.documents import Document


MAX_CHUNK_CHARS = 3_000   # ~750 tokens — matches typical embedding model context
MIN_CHUNK_CHARS = 10      # skip completely empty stubs (pass / ...)


def _source_lines(source: str) -> List[str]:
    return source.splitlines(keepends=True)


def _extract_node_source(lines: List[str], node: ast.AST) -> str:
    """
    Extract the source text for an AST node, including any decorators.

    WHY DECORATORS MUST BE INCLUDED
    `node.lineno` for a decorated function points at the `def` line, not at the
    first `@`. Slicing from there dropped every decorator from the chunk — and
    because the module-level pass treats decorator lines as "inside a
    definition", they were excluded from the module chunk too. The result was
    that decorator lines existed in *no* chunk and were therefore absent from
    the index entirely:

        @app.get("/api/v1/users/{user_id}")   ← unsearchable
        def read_user(user_id: int): ...

    Searching for a route path, a Celery task name, or a pytest fixture marker
    returned nothing, and the LLM never saw that a function was a route handler
    at all. `_node_span` uses the same start line, so spans and text agree.
    """
    start, end = _node_span(node)
    return "".join(lines[start - 1:end])   # ast is 1-indexed; slice end is exclusive


def _node_name(node: ast.AST) -> str:
    """Return the symbol name for a function or class definition node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    if isinstance(node, ast.ClassDef):
        return node.name
    return "<module>"


def _node_type(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async_function"
    if isinstance(node, ast.FunctionDef):
        return "function"
    return "module"


def _node_span(node: ast.AST) -> tuple[int, int]:
    """
    Return the 1-indexed, inclusive (start_line, end_line) a node occupies.

    Decorators are part of the definition as far as a reader is concerned, so
    `@app.get("/x")` on the line above `def handler():` is included in the span.
    `_extract_node_source` slices from `node.lineno`, so the two must agree or
    the cited line numbers would be off by the decorator count.
    """
    start = node.lineno
    end = getattr(node, "end_lineno", None) or node.lineno
    for decorator in getattr(node, "decorator_list", []):
        start = min(start, decorator.lineno)
    return start, end


def _line_span_of_offset(
    source: str, start_offset: int, length: int
) -> tuple[int, int]:
    """
    Convert a character offset + length inside `source` into 1-indexed line numbers.

    Used when a single oversized function is window-split: each window covers a
    different slice of the same node, so they must not all claim the node's full
    span or every window would cite identical lines.

    The end line is derived from the slice's LAST character rather than from a
    newline count over the whole slice. A slice ending exactly on "\n" closes the
    line it terminates; counting newlines would push end_line one past it and
    report a line that the chunk does not actually contain (and which may not
    exist at all, when the slice ends at end-of-file).
    """
    start_line = source.count("\n", 0, start_offset) + 1
    if length <= 0:
        return start_line, start_line
    last_char_index = min(start_offset + length, len(source)) - 1
    end_line = source.count("\n", 0, last_char_index) + 1
    return start_line, max(start_line, end_line)


def _encode_line_ranges(line_numbers: List[int]) -> str:
    """
    Collapse a sorted list of line numbers into compact ranges: "1-30,88-92".

    ChromaDB metadata values must be scalars (str/int/float/bool) — a list is
    rejected — so discontinuous spans are encoded as a string and parsed back by
    the consumer. Adjacent numbers merge into a single range, which keeps the
    value short even for a file with imports scattered throughout.
    """
    if not line_numbers:
        return ""
    ranges: List[tuple[int, int]] = []
    start = previous = line_numbers[0]
    for line in line_numbers[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append((start, previous))
        start = previous = line
    ranges.append((start, previous))
    return ",".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def parse_line_ranges(encoded: str) -> List[tuple[int, int]]:
    """
    Inverse of :func:`_encode_line_ranges`. Malformed segments are skipped.

    Returns [] for empty input so callers can treat "no detailed ranges" and
    "unparseable ranges" the same way — fall back to start_line/end_line.
    """
    ranges: List[tuple[int, int]] = []
    for part in (encoded or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                ranges.append((int(a), int(b)))
            else:
                value = int(part)
                ranges.append((value, value))
        except ValueError:
            continue
    return ranges


def _split_class_by_methods(
    class_source: str,
    class_name: str,
    class_header: str,
    file_path: str,
    language: str,
    repo_url: str,
    base_metadata: dict,
    chunk_index_start: int,
    class_start_line: int = 1,
) -> List[Document]:
    """
    When a class body is too large for one chunk, split by method.
    Prepend `class_header` (class Foo:  + class docstring) to each method chunk
    so the LLM always knows which class the method belongs to.

    `class_start_line` is the class's first line in the ORIGINAL file. Method
    line numbers are parsed from `class_source` (which starts at line 1), so they
    must be rebased onto the original file or every method in an oversized class
    would cite a line number near the top of the file.
    """
    try:
        tree = ast.parse(class_source)
    except SyntaxError:
        return []

    lines = _source_lines(class_source)
    class_node = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)),
        None,
    )
    if not class_node:
        return []

    docs: List[Document] = []
    chunk_idx = chunk_index_start

    for method in class_node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        method_src = _extract_node_source(lines, method)
        # Indent method source to look correct when prepended with class header
        combined = f"{class_header}\n{textwrap.indent(method_src, '    ')}"
        if len(combined) < MIN_CHUNK_CHARS:
            continue

        # Rebase the method's span from class-relative onto file-absolute.
        # class_source line 1 == class_start_line in the original file.
        method_start, method_end = _node_span(method)
        docs.append(Document(
            page_content=combined[:MAX_CHUNK_CHARS],
            metadata={
                **base_metadata,
                "symbol_name": f"{class_name}.{method.name}",
                "symbol_type": "method",
                "chunk_index": chunk_idx,
                "start_line": class_start_line + method_start - 1,
                "end_line": class_start_line + method_end - 1,
            },
        ))
        chunk_idx += 1

    return docs


def chunk_python_file(
    source: str,
    file_path: str,
    file_name: str,
    language: str,
    repo_url: str,
    content_hash: str,
) -> List[Document]:
    """
    Parse Python source with ast.parse() and return one Document per
    top-level symbol (function, class, or module-level constants block).

    Falls back to returning an empty list if the file cannot be parsed
    (the caller falls back to RecursiveCharacterTextSplitter in that case).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # caller falls back to char splitter

    lines = _source_lines(source)

    base_metadata = {
        "source":       file_path,
        "file_name":    file_name,
        "language":     language,
        "repo_url":     repo_url,
        "content_hash": content_hash,
    }

    docs: List[Document] = []
    chunk_idx = 0

    # ── Collect all module-level non-definition code ─────────────────────────
    # Module imports/constants are valid before, between, and after definitions.
    # The previous implementation stopped at the first function/class and lost
    # later imports and constants from the indexed representation.
    definition_ranges: list[tuple[int, int]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min(
                [node.lineno]
                + [decorator.lineno for decorator in node.decorator_list]
            )
            definition_ranges.append((start, node.end_lineno))

    # Build a set of all line numbers that fall inside a definition.
    # Using a set instead of checking every (start, end) range per line
    # reduces the complexity from O(N×M) to O(N+M) — important for large
    # files with many definitions (e.g. 1000 lines, 50 functions = 50k→1050 ops).
    definition_line_set: set[int] = set()
    for start, end in definition_ranges:
        definition_line_set.update(range(start, end + 1))

    module_level_lines: List[str] = []
    module_line_numbers: List[int] = []
    for line_number, line in enumerate(lines, start=1):
        if line_number not in definition_line_set:
            module_level_lines.append(line)
            module_line_numbers.append(line_number)

    module_content = "".join(module_level_lines).strip()
    if len(module_content) >= MIN_CHUNK_CHARS:
        # Module-level code is gathered from NON-CONTIGUOUS regions: the import
        # header, then constants sitting between function definitions. A single
        # start/end pair would span the whole file and highlight 150 lines to
        # point at 20 — technically true, useless as evidence.
        #
        # So we record both:
        #   start_line/end_line — the hull, for consumers that expect one range
        #   line_ranges         — "1-30,88-92", the exact regions, so the UI can
        #                         highlight only the lines really in this chunk
        # ChromaDB metadata must be a scalar, hence the compact string encoding.
        docs.append(Document(
            page_content=module_content[:MAX_CHUNK_CHARS],
            metadata={
                **base_metadata,
                "symbol_name": "<module>",
                "symbol_type": "module",
                "chunk_index": chunk_idx,
                "start_line": module_line_numbers[0],
                "end_line": module_line_numbers[-1],
                "line_ranges": _encode_line_ranges(module_line_numbers),
            },
        ))
        chunk_idx += 1

    # ── One chunk per top-level function/class ────────────────────────────────
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        node_src = _extract_node_source(lines, node)
        symbol_name = _node_name(node)
        symbol_type = _node_type(node)
        node_start, node_end = _node_span(node)

        if len(node_src) < MIN_CHUNK_CHARS:
            continue  # skip trivial stubs

        if len(node_src) <= MAX_CHUNK_CHARS:
            docs.append(Document(
                page_content=node_src,
                metadata={
                    **base_metadata,
                    "symbol_name": symbol_name,
                    "symbol_type": symbol_type,
                    "chunk_index": chunk_idx,
                    "start_line": node_start,
                    "end_line": node_end,
                },
            ))
            chunk_idx += 1
        else:
            # Too large for one chunk — split by inner methods/nested functions
            if isinstance(node, ast.ClassDef):
                # Build a compact class header (class name + docstring only)
                class_header_lines = [f"class {node.name}:"]
                first_body = node.body[0] if node.body else None
                if isinstance(first_body, ast.Expr) and isinstance(first_body.value, ast.Constant):
                    docstring = ast.get_docstring(node) or ""
                    if docstring:
                        class_header_lines.append(f'    """{docstring[:200]}"""')
                class_header = "\n".join(class_header_lines)

                sub_docs = _split_class_by_methods(
                    class_source=node_src,
                    class_name=symbol_name,
                    class_header=class_header,
                    file_path=file_path,
                    language=language,
                    repo_url=repo_url,
                    base_metadata=base_metadata,
                    chunk_index_start=chunk_idx,
                    class_start_line=node.lineno,
                )
                docs.extend(sub_docs)
                chunk_idx += len(sub_docs)
            else:
                # Large standalone function — split into MAX_CHUNK_CHARS windows with overlap
                OVERLAP = 200
                step = MAX_CHUNK_CHARS - OVERLAP
                for start in range(0, len(node_src), step):
                    part = node_src[start:start + MAX_CHUNK_CHARS]
                    if len(part) < MIN_CHUNK_CHARS:
                        break
                    # Each window covers a different slice of the function, so
                    # derive its own span instead of repeating the whole node's.
                    win_start, win_end = _line_span_of_offset(node_src, start, len(part))
                    docs.append(Document(
                        page_content=part,
                        metadata={
                            **base_metadata,
                            "symbol_name": symbol_name,
                            "symbol_type": symbol_type,
                            "chunk_index": chunk_idx,
                            # node_src starts at node.lineno, so rebase onto the file.
                            "start_line": node.lineno + win_start - 1,
                            "end_line": node.lineno + win_end - 1,
                        },
                    ))
                    chunk_idx += 1

    return docs
