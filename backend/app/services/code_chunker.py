"""
code_chunker.py — AST-boundary chunking for every language we index.

WHY THIS IS THE HIGHEST-LEVERAGE FILE IN THE RETRIEVAL PATH
-----------------------------------------------------------
`ast_chunker.py` chunks Python by its real structure: one chunk per function or
class, spans matching the symbol. Every other language used
`RecursiveCharacterTextSplitter`, which counts characters. For code, counting
characters is the wrong unit — it cuts a function in half and glues the tail of
one helper onto the head of the next. The chunk that gets embedded then contains
noise, so the reranker cannot tell signal from noise, so the answer cites the
wrong lines.

This module extends the AST treatment to JS/TS (and any grammar added to
`tree_sitter_langs`) and provides ONE entry point — `chunk_code_file` — so the
routing decision lives in exactly one place instead of being re-derived by every
caller.

WHAT IS SHARED WITH THE PYTHON PATH, AND WHY EXACTLY
----------------------------------------------------
The metadata contract is not a convention here, it is the citation contract:
`trust_service`, `citation_service` and the UI all read `start_line`/`end_line`
and `line_ranges`. A chunker that emits a *slightly* different shape is a silent
citation bug, so this module imports the same limits and the same range encoder
rather than reimplementing them:

  - `MAX_CHUNK_CHARS` / `MIN_CHUNK_CHARS` — one budget for both paths.
  - `_encode_line_ranges` — the "1-30,88-92" encoding. Duplicating it would mean
    two encodings that drift, and only one of them covered by the existing tests.

Python deliberately still goes through `ast`, not through tree-sitter: the
stdlib parser is exact, needs no wheel, and its behaviour is pinned by
`test_ast_chunker.py`. Replacing it would risk the one path that already works
for no gain.

WHAT IS BETTER THAN THE PYTHON PATH
-----------------------------------
Two invariants the Python chunker does not hold, both verified by tests here:

1. **A line is never in zero chunks.** Python excludes every line inside a
   definition from the module-level chunk. A definition too small to chunk
   (`MIN_CHUNK_CHARS`) is skipped — so its lines land in no chunk at all and
   become unsearchable. That is the same class of bug the Python module's own
   docstring describes for decorator lines. Here, a skipped definition's lines
   stay in the module chunk.

2. **Chunks are dropped, not corrupted, when the file does not fully parse.**
   tree-sitter recovers from errors and still returns a tree, which is normally a
   feature. But a definition overlapping an ERROR node has a span we cannot
   trust, and a citation pointing at the wrong lines is worse than a weaker
   chunk. Those definitions are dropped; the rest of the file still chunks.

COST: no model, no network, no API key. Pure CPU. A parse of a typical source
file is well under a millisecond.
"""

from __future__ import annotations

import logging
from typing import Any, List

from langchain_core.documents import Document

# Shared with the Python path on purpose — see the module docstring.
from app.services.ast_chunker import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    _encode_line_ranges,
    _line_span_of_offset,
    chunk_python_file,
)
from app.services.tree_sitter_langs import get_parser, grammar_for_path

logger = logging.getLogger(__name__)


# ── Which nodes are worth their own chunk ─────────────────────────────────────
#
# Node type names come from the grammars themselves, not from documentation —
# every one below was confirmed by parsing a real snippet and walking the tree.
#
# `lexical_declaration` / `variable_declaration` are deliberately *conditional*:
# in JS/TS a function usually arrives as a binding, not a declaration —
# `export const handler = async (req, res) => {...}` is the standard Express and
# Next.js route shape. So a `const` whose value is a function is a definition;
# `const API_URL = "..."` is not, and stays in the module chunk (mirroring
# Python, where a module-level `x = 1` is not a definition either).
_DECLARATION_NODES = frozenset({
    "function_declaration",
    "generator_function_declaration",
    "function_expression",          # export default function () {}
    "class_declaration",
    "abstract_class_declaration",   # TS
    "method_definition",            # class methods, and object-literal methods
    "interface_declaration",        # TS
    "type_alias_declaration",       # TS
    "enum_declaration",             # TS
})

_BINDING_NODES = frozenset({"lexical_declaration", "variable_declaration"})

#: Node types that make a binding a definition rather than a constant.
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "generator_function"})

#: Symbol types, reusing the vocabulary `ast_chunker` established so downstream
#: consumers see one set of values regardless of language.
_TYPE_BY_NODE = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "function_expression": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}

#: A method of an oversized class is re-emitted with its class header prepended,
#: like the Python path does, so a chunk never loses the class it belongs to.
_CLASS_CONTAINERS = frozenset({"class_declaration", "abstract_class_declaration"})


# ── small helpers over tree-sitter nodes ──────────────────────────────────────


def _same_node(a: Any, b: Any) -> bool:
    """
    Whether two node handles denote the same node.

    NOT `a is b` and NOT `id(a) == id(b)`. py-tree-sitter builds a fresh Python
    wrapper each time you reach a node, so two handles for one node are distinct
    objects — and a recycled `id()` can make two *different* nodes compare equal,
    which is worse than failing. Both mistakes were made here first: identity
    comparison silently dropped JSDoc blocks (the `declaration` check never
    matched) and silently double-indexed a class with its own methods.

    Byte offsets plus the type are stable across handles and unique for the
    nodes we compare, so compare those.
    """
    if a is None or b is None:
        return False
    return a.type == b.type and a.start_byte == b.start_byte and a.end_byte == b.end_byte


def _text(node: Any, source_bytes: bytes) -> str:
    """Exact source of a node, as text. Byte offsets, because that is what trees carry."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _name_field(node: Any, source_bytes: bytes) -> str | None:
    """A node's `name` field, when it has one."""
    child = node.child_by_field_name("name")
    if child is None:
        return None
    return _text(child, source_bytes)


def _is_async(node: Any) -> bool:
    """
    True when the node carries the `async` modifier.

    JS/TS have no distinct `async function` node type — `async` is an anonymous
    token child (`['async', 'function']` for a declaration, `['async', '=>']` for
    an arrow). Confirmed by parsing each form rather than assumed, because
    guessing here would silently label every async function as sync.
    """
    return any(child.type == "async" and not child.is_named for child in node.children)


def _binding_function_value(node: Any) -> Any | None:
    """
    The function-valued expression a binding declares, if any.

    For `const handler = async () => {}` that is the `arrow_function`. Needed
    because the `async` token is a child of the *value*, not of the
    `lexical_declaration` — so asking the declaration whether it is async always
    answers no, and every async handler in a Node codebase would be labelled
    `function`.
    """
    for declarator in node.children:
        if declarator.type != "variable_declarator":
            continue
        value = declarator.child_by_field_name("value")
        if value is not None and value.type in _FUNCTION_VALUES:
            return value
    return None


def _symbol_name(node: Any, source_bytes: bytes) -> str | None:
    """
    The symbol a chunk should be cited as.

    Bindings are named after the *declarator whose value is a function*, not
    after the first declarator: in `const a = 1, handler = () => {}` the symbol is
    `handler`. Anonymous default exports fall back to "default", which is what a
    reader would call them.
    """
    if node.type in _BINDING_NODES:
        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            value = declarator.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUES:
                return _name_field(declarator, source_bytes)
        # No function-valued declarator: fall back to the first declared name so
        # the chunk is still citable rather than anonymous.
        for declarator in node.children:
            if declarator.type == "variable_declarator":
                return _name_field(declarator, source_bytes)
        return None

    name = _name_field(node, source_bytes)
    if name:
        return name
    if node.type == "function_expression":
        return "default"     # `export default function () {}`
    return None


def _symbol_type(node: Any, source_bytes: bytes) -> str:
    """Symbol type for metadata, matching `ast_chunker`'s vocabulary."""
    base = _TYPE_BY_NODE.get(node.type)
    if base is None:
        base = "function" if node.type in _BINDING_NODES else "definition"
    # Python distinguishes `async_function`; keep the same distinction so a
    # consumer filtering on it does not have to know which language it is in.
    if base == "function":
        async_target = _binding_function_value(node) if node.type in _BINDING_NODES else node
        if async_target is not None and _is_async(async_target):
            return "async_function"
    return base


def _is_definition(node: Any) -> bool:
    """True when this node should become its own chunk."""
    if node.type in _DECLARATION_NODES:
        return True
    if node.type in _BINDING_NODES:
        return _binding_function_value(node) is not None
    return False


def _error_ranges(root: Any) -> list[tuple[int, int]]:
    """Row ranges (0-indexed, inclusive) covered by ERROR or missing nodes."""
    ranges: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            ranges.append((node.start_point[0], node.end_point[0]))
            continue     # children of an ERROR node carry no extra information
        stack.extend(node.children)
    return ranges


def _overlaps(row_range: tuple[int, int], error_rows: list[tuple[int, int]]) -> bool:
    start, end = row_range
    return any(start <= err_end and err_start <= end for err_start, err_end in error_rows)


def _span_node(node: Any) -> Any:
    """
    The node whose span should be cited.

    `export function f() {}` parses as `export_statement` → `function_declaration`.
    Citing the inner node would drop the `export` keyword, so a reader would not
    see that the symbol is public — the same mistake the Python path documents for
    decorators. When the statement wraps this declaration, the statement is the
    span. Matching on the `declaration`/`value` field (not merely on being a
    child) keeps `export { a, b }` — which has neither — from being mistaken for
    a declaration.
    """
    parent = node.parent
    if parent is not None and parent.type == "export_statement":
        for field in ("declaration", "value"):
            if _same_node(parent.child_by_field_name(field), node):
                return parent
    return node


def _start_row_with_leading_comments(node: Any) -> int:
    """
    0-indexed start row, extended upward over attached comments and decorators.

    JSDoc is the JS/TS equivalent of a Python docstring, and Python gets
    docstrings for free because they sit *inside* the function node. JSDoc sits
    outside it, so without this the description of a symbol would never be
    embedded — retrieval for "how do I call verifyToken" would miss the comment
    that says exactly that.

    Only *adjacent* comments are absorbed (`prev_end_row + 1 == start_row`), so a
    comment separated by a blank line — a file header, or a licence banner — is
    left where it belongs. Decorators need no adjacency test: they are
    syntactically attached to the declaration, and TS allows them on the line
    directly above or on the same line.
    """
    start = node.start_point[0]
    sibling = node.prev_named_sibling
    while sibling is not None:
        if sibling.type == "decorator":
            # Decorators attach to the declaration regardless of blank lines —
            # unlike comments, they cannot belong to anything else.
            start = min(start, sibling.start_point[0])
        elif sibling.type == "comment" and sibling.end_point[0] + 1 == start:
            start = sibling.start_point[0]
        else:
            break
        sibling = sibling.prev_named_sibling
    return start


def _signature_of(node_source: str, max_lines: int = 6) -> str:
    """
    The opening line(s) of a definition, used to label a mid-definition window.

    A 4 000-character function split into windows produces a second window whose
    text begins in the middle of a statement — `95 = input + 95;` — and says
    nothing about what it is part of. Embedding that window indexes a fragment no
    query can sensibly match. Prepending the signature gives every window the same
    anchor, which is exactly what the oversized-class path already does with its
    class header.

    Multi-line signatures are real (`export const handler = async (\n req, res\n) => {`),
    so this walks forward to the first line that opens the body rather than
    assuming the signature is one line.
    """
    collected: list[str] = []
    for line in node_source.splitlines()[:max_lines]:
        collected.append(line)
        stripped = line.rstrip()
        if "{" in line or stripped.endswith("=>") or stripped.endswith(")"):
            break
    return "\n".join(collected)


def _collect_definitions(root: Any) -> list[Any]:
    """
    Outermost definition nodes, in source order.

    "Outermost" is decided **during** the walk, by carrying a flag down the tree.
    The obvious alternative — collect everything, then ask each node whether one
    of its ancestors is also in the set — needs identity comparison between
    ancestor handles and collected handles, which py-tree-sitter does not
    support (see `_same_node`). Doing it in one pass avoids the question.

    Nested definitions are not returned at all. An oversized class is split by
    walking its own body, which is more direct than collecting methods and
    re-nesting them by hand.

    Object-literal methods survive, and that is the point: in
    `module.exports = { run() {} }` the `method_definition` has no definition
    ancestor (`expression_statement` is not one), so CommonJS modules chunk per
    method instead of collapsing into one blob per file.
    """
    found: list[Any] = []
    stack: list[tuple[Any, bool]] = [(root, False)]
    while stack:
        node, inside_definition = stack.pop()
        if _is_definition(node):
            if not inside_definition:
                found.append(node)
            inside_definition = True     # everything below is part of this chunk
        for child in node.children:
            stack.append((child, inside_definition))

    # The walk order follows a stack, so restore source order: chunk_index is
    # part of the metadata contract and reads better in file order.
    found.sort(key=lambda n: n.start_byte)
    return found


# ── The chunker ───────────────────────────────────────────────────────────────


def chunk_with_tree_sitter(
    source: str,
    file_path: str,
    file_name: str,
    language: str,
    repo_url: str,
    content_hash: str,
) -> List[Document]:
    """
    Parse with tree-sitter and return one Document per top-level definition.

    Returns `[]` — never raises — when the language has no grammar, when the
    grammar is not installed, or when nothing usable could be parsed. The caller
    then falls back to `RecursiveCharacterTextSplitter`, which is exactly the
    behaviour shipped before this module existed. An unavailable grammar can
    therefore only ever be as bad as the old code, never worse.
    """
    grammar = grammar_for_path(file_name or file_path)
    if grammar is None:
        return []
    parser = get_parser(grammar)
    if parser is None:
        return []

    source_bytes = source.encode("utf-8")
    try:
        tree = parser.parse(source_bytes)
    except Exception as exc:  # noqa: BLE001 - parsing must never break an ingest
        logger.warning("tree-sitter failed to parse %s: %s", file_name, exc)
        return []

    root = tree.root_node
    error_rows = _error_ranges(root)
    lines = source.splitlines(keepends=True)

    base_metadata = {
        "source":       file_path,
        "file_name":    file_name,
        "language":     language,
        "repo_url":     repo_url,
        "content_hash": content_hash,
    }

    def slice_lines(start_row: int, end_row: int) -> str:
        """1-indexed inclusive slice; tree-sitter rows are 0-indexed."""
        return "".join(lines[start_row:end_row + 1])

    docs: List[Document] = []
    chunk_idx = 0

    #: Rows that ended up inside a chunk. Everything else becomes the module
    #: chunk, and only rows that were genuinely emitted are excluded — so a
    #: skipped stub's lines stay searchable instead of disappearing (see the
    #: module docstring).
    covered_rows: set[int] = set()

    def emit(text: str, name: str | None, symbol_type: str, start_row: int, end_row: int, **extra) -> None:
        nonlocal chunk_idx
        if len(text) < MIN_CHUNK_CHARS:
            return
        docs.append(Document(
            page_content=text[:MAX_CHUNK_CHARS],
            metadata={
                **base_metadata,
                "symbol_name": name or "<anonymous>",
                "symbol_type": symbol_type,
                "chunk_index": chunk_idx,
                "start_line": start_row + 1,
                "end_line": end_row + 1,
                **extra,
            },
        ))
        chunk_idx += 1
        covered_rows.update(range(start_row, end_row + 1))

    for node in _collect_definitions(root):
        span_node = _span_node(node)
        start_row = _start_row_with_leading_comments(span_node)
        # The span node may be wider than the declaration it wraps (`export`),
        # but never narrower, so take the widest of the two.
        end_row = max(span_node.end_point[0], node.end_point[0])

        # A definition straddling a parse error has an untrustworthy span. Drop
        # it rather than cite lines we cannot stand behind — the rest of the file
        # still chunks, which is the benefit of error-tolerant parsing.
        if _overlaps((start_row, end_row), error_rows):
            continue

        node_source = slice_lines(start_row, end_row)
        name = _symbol_name(node, source_bytes)
        symbol_type = _symbol_type(node, source_bytes)

        if len(node_source) <= MAX_CHUNK_CHARS:
            emit(node_source, name, symbol_type, start_row, end_row)
            continue

        # ── Oversized: split by structure, not by character count ─────────────
        if node.type in _CLASS_CONTAINERS:
            methods = [
                child for child in node.children
                if child.type in ("method_definition", "abstract_method_signature", "public_field_definition")
            ]
            body = node.child_by_field_name("body")
            if body is not None:
                methods = [c for c in body.children if c.type == "method_definition"] or methods
            if methods:
                header_row = _start_row_with_leading_comments(span_node)
                header = slice_lines(header_row, max(header_row, node.start_point[0]))
                dropped_any = False
                for method in methods:
                    m_start = _start_row_with_leading_comments(method)
                    m_end = method.end_point[0]
                    if _overlaps((m_start, m_end), error_rows):
                        dropped_any = True
                        continue
                    combined = f"{header}\n{slice_lines(m_start, m_end)}"
                    emit(
                        combined,
                        f"{name}.{_symbol_name(method, source_bytes) or '?'}",
                        "method",
                        m_start,
                        m_end,
                    )
                # Claim the whole class region so the class header and closing
                # brace do not reappear in the module chunk. Only when nothing
                # was dropped: if a method was refused for overlapping a parse
                # error, its lines must stay reachable through the module chunk
                # rather than vanish from the index entirely.
                if not dropped_any:
                    covered_rows.update(range(start_row, end_row + 1))
                continue
            # No methods to split on — fall through to windowing.

        # A single long definition: sliding windows with overlap, each window
        # citing its own lines. Reusing `_line_span_of_offset` keeps window
        # semantics identical to the Python path, including the rule that the
        # end line comes from the slice's last character rather than from a
        # newline count.
        #
        # The window budget reserves room for the signature so that `emit`'s
        # truncation never bites: sizing windows at MAX_CHUNK_CHARS and then
        # prepending a label would silently drop the tail of every window, losing
        # real content to make room for a heading.
        signature = _signature_of(node_source)
        overlap = 200
        budget = max(MIN_CHUNK_CHARS * 2, MAX_CHUNK_CHARS - len(signature) - 1)
        step = max(1, budget - overlap)
        for offset in range(0, len(node_source), step):
            part = node_source[offset:offset + budget]
            if len(part) < MIN_CHUNK_CHARS:
                break
            # The first window already carries the signature.
            content = part if offset == 0 else f"{signature}\n{part}"
            win_start, win_end = _line_span_of_offset(node_source, offset, len(part))
            emit(
                content,
                name,
                symbol_type,
                start_row + win_start - 1,
                start_row + win_end - 1,
            )
            if offset + budget >= len(node_source):
                break

    # ── Module-level chunk ────────────────────────────────────────────────────
    # Imports, constants, and anything between definitions. Gathered from
    # non-contiguous regions, exactly as the Python path does, so it carries both
    # the hull (start_line/end_line) and the exact regions (line_ranges) — a hull
    # alone would highlight 150 lines to point at 20.
    module_rows = [row for row in range(len(lines)) if row not in covered_rows]
    module_lines = [lines[row] for row in module_rows]
    module_content = "".join(module_lines).strip()
    if len(module_content) >= MIN_CHUNK_CHARS:
        docs.append(Document(
            page_content=module_content[:MAX_CHUNK_CHARS],
            metadata={
                **base_metadata,
                "symbol_name": "<module>",
                "symbol_type": "module",
                "chunk_index": chunk_idx,
                "start_line": module_rows[0] + 1,
                "end_line": module_rows[-1] + 1,
                # Rows are emitted in ascending order, which the encoder requires.
                "line_ranges": _encode_line_ranges([row + 1 for row in module_rows]),
            },
        ))

    return docs


# ── One entry point ───────────────────────────────────────────────────────────


def chunk_code_file(
    source: str,
    file_path: str,
    file_name: str,
    language: str,
    repo_url: str,
    content_hash: str,
) -> List[Document]:
    """
    The single routing point: AST chunks for a source file, or `[]` to fall back.

    Python goes through `ast` (stdlib, exact, already tested); everything else
    goes through tree-sitter. Keeping the two behind one function means the
    routing rule exists once — a caller cannot accidentally chunk TypeScript with
    the Python parser or vice versa, and a test can assert both paths agree on the
    metadata contract.

    `[]` means "no AST chunking applies here" — an unsupported file type, a
    missing grammar, or a file that would not parse — and the caller falls back
    to character splitting.
    """
    if grammar_for_path(file_name or file_path) == "python":
        return chunk_python_file(
            source=source,
            file_path=file_path,
            file_name=file_name,
            language=language,
            repo_url=repo_url,
            content_hash=content_hash,
        )
    return chunk_with_tree_sitter(
        source=source,
        file_path=file_path,
        file_name=file_name,
        language=language,
        repo_url=repo_url,
        content_hash=content_hash,
    )
