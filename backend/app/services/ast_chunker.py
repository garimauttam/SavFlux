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
    """Extract the source text for an AST node using line numbers."""
    start = node.lineno - 1   # ast is 1-indexed
    end   = node.end_lineno   # exclusive upper bound when used as slice
    return "".join(lines[start:end])


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


def _split_class_by_methods(
    class_source: str,
    class_name: str,
    class_header: str,
    file_path: str,
    language: str,
    repo_url: str,
    base_metadata: dict,
    chunk_index_start: int,
) -> List[Document]:
    """
    When a class body is too large for one chunk, split by method.
    Prepend `class_header` (class Foo:  + class docstring) to each method chunk
    so the LLM always knows which class the method belongs to.
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

        docs.append(Document(
            page_content=combined[:MAX_CHUNK_CHARS],
            metadata={
                **base_metadata,
                "symbol_name": f"{class_name}.{method.name}",
                "symbol_type": "method",
                "chunk_index": chunk_idx,
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
    for line_number, line in enumerate(lines, start=1):
        if line_number not in definition_line_set:
            module_level_lines.append(line)

    module_content = "".join(module_level_lines).strip()
    if len(module_content) >= MIN_CHUNK_CHARS:
        docs.append(Document(
            page_content=module_content[:MAX_CHUNK_CHARS],
            metadata={
                **base_metadata,
                "symbol_name": "<module>",
                "symbol_type": "module",
                "chunk_index": chunk_idx,
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
                    docs.append(Document(
                        page_content=part,
                        metadata={
                            **base_metadata,
                            "symbol_name": symbol_name,
                            "symbol_type": symbol_type,
                            "chunk_index": chunk_idx,
                        },
                    ))
                    chunk_idx += 1

    return docs
