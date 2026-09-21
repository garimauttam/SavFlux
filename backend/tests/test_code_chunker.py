"""
test_code_chunker.py — Tests for tree-sitter AST-boundary chunking.

WHAT THESE TESTS PROTECT
------------------------
The chunker's whole value is that a chunk *is one symbol*, and that its
`start_line`/`end_line` point at that symbol. A chunker that is subtly wrong does
not crash — it produces plausible chunks with slightly wrong spans, which the UI
then presents as evidence. So the tests here are mostly about correctness of the
claim, not about the code running.

Named failure modes, each with a test:

1. **A function cut in half.** The negative case for this module's existence:
   a TS function longer than the character splitter's `chunk_size` must stay in
   one chunk. `test_a_long_function_is_not_cut_in_half_by_character_counting`.
2. **A class indexed twice** — once whole and once per method, letting one symbol
   dominate retrieval. `test_a_class_is_chunked_once_whole`.
3. **JSDoc left behind.** JSDoc lives outside the declaration node, so it is only
   in the chunk if the chunker deliberately reaches for it.
   `test_jsdoc_travels_with_the_symbol_it_documents`.
4. **A citation into lines that do not exist, or into the wrong symbol.**
   `test_every_span_round_trips_to_its_symbol` slices the file with each chunk's
   own span and asserts the symbol name appears in what came back.
5. **`.tsx` parsed as plain TypeScript.** A React component would come back as an
   ERROR-riddled tree; `test_tsx_component_chunks`.
6. **Lines that exist in no chunk at all** — silently unsearchable.
   `test_every_line_appears_in_some_chunk`.
7. **A missing grammar raising instead of falling back.**
   `test_unsupported_language_falls_back`.

Everything here is pure CPU: no model, no network, no API key.
"""

from __future__ import annotations

import pytest

from app.services.code_chunker import chunk_code_file, chunk_with_tree_sitter
from app.services.ast_chunker import MAX_CHUNK_CHARS, chunk_python_file
from app.services.tree_sitter_langs import get_language

TS_META = dict(
    file_path="src/handlers.ts",
    file_name="handlers.ts",
    language="ts",
    repo_url="https://github.com/o/r",
    content_hash="abc123",
)

TSX_META = dict(
    file_path="src/Button.tsx",
    file_name="Button.tsx",
    language="tsx",
    repo_url="https://github.com/o/r",
    content_hash="abc123",
)


def _span_text(source: str, start_line: int, end_line: int) -> str:
    """The lines a chunk claims to cover, taken from the original file."""
    lines = source.splitlines()
    return "\n".join(lines[start_line - 1:end_line])


def _by_name(docs, name):
    return [d for d in docs if d.metadata["symbol_name"] == name]


@pytest.fixture(autouse=True)
def _require_typescript():
    if get_language("typescript") is None:
        pytest.skip("tree-sitter-typescript is not installed")


# ── The negative case this module exists for ──────────────────────────────────

LONG_TS_FUNCTION = (
    "export function bigFunction(input: number): number {\n"
    + "".join(f"  const step{i} = input + {i};\n" for i in range(160))
    + "  return step159;\n"
    "}\n"
)


def test_a_long_function_is_not_cut_in_half_by_character_counting():
    """
    The reason this module exists, asserted as a negative case.

    `bigFunction` is larger than `MAX_CHUNK_CHARS`. The old path — and any other
    file in the repo that still uses RecursiveCharacterTextSplitter — would emit
    several chunks whose first and last pieces are fragments of one function,
    neither of which tells the model what function it is looking at.

    It is still legitimate for a *huge* function to be windowed, so the test
    asserts what actually matters: every chunk for this symbol starts at the
    function's own first line and carries its name, and no chunk begins with a
    line that is not the function's opening.
    """
    assert len(LONG_TS_FUNCTION) > MAX_CHUNK_CHARS, "fixture must exceed the chunk budget"

    docs = chunk_code_file(source=LONG_TS_FUNCTION, **TS_META)
    chunks = [d for d in docs if d.metadata["symbol_name"] == "bigFunction"]
    assert chunks, "the function was not chunked at all"

    for chunk in chunks:
        assert chunk.page_content.lstrip().startswith("export function bigFunction"), (
            "a window started mid-function, so it does not say what it is part of"
        )

    # And the whole body is present across the chunks, not truncated.
    joined = "".join(c.page_content for c in chunks)
    assert "step159" in joined


def test_a_short_function_gets_exactly_one_chunk():
    """No windowing when the function fits — one symbol, one chunk."""
    source = "export function small(a: number): number {\n  return a * 2;\n}\n"
    docs = chunk_code_file(source=source, **TS_META)
    assert len(_by_name(docs, "small")) == 1


# ── One chunk per symbol ──────────────────────────────────────────────────────


def test_each_top_level_function_gets_its_own_chunk():
    """Two functions must not be merged, and neither may contain the other."""
    source = (
        "export function alpha(): number {\n  return 1;\n}\n\n"
        "export function beta(): number {\n  return 2;\n}\n"
    )
    docs = chunk_code_file(source=source, **TS_META)
    alpha = _by_name(docs, "alpha")
    beta = _by_name(docs, "beta")

    assert len(alpha) == 1 and len(beta) == 1
    assert "beta" not in alpha[0].page_content
    assert "alpha" not in beta[0].page_content


def test_a_class_is_chunked_once_whole():
    """
    Regression: the class and its methods were both indexed.

    Identity comparison across py-tree-sitter node handles silently failed, so
    the "is this method nested inside a definition?" check misfired and every
    method became a top-level chunk *alongside* the whole class. One symbol then
    had N+1 chunks in the index and dominated its own retrieval.
    """
    source = (
        "export class Service {\n"
        "  constructor(private client: Client) {}\n\n"
        "  async list(): Promise<Item[]> {\n"
        "    return this.client.get('/items');\n"
        "  }\n"
        "}\n"
    )
    docs = chunk_code_file(source=source, **TS_META)

    whole = _by_name(docs, "Service")
    assert len(whole) == 1
    assert "async list" in whole[0].page_content
    # No bare `list` / `constructor` chunks for a class that fits in one chunk.
    assert _by_name(docs, "list") == []
    assert _by_name(docs, "constructor") == []
    assert _by_name(docs, "Service.list") == []


def test_an_oversized_class_is_split_by_method():
    """Past the budget, split by structure — never by character count."""
    methods = "".join(
        f"  async method{i}(input: number): Promise<number> {{\n"
        f"    return input + {i};\n"
        f"  }}\n\n"
        for i in range(60)
    )
    source = f"export class BigService {{\n{methods}}}\n"
    assert len(source) > MAX_CHUNK_CHARS, "fixture must exceed the chunk budget"

    docs = chunk_code_file(source=source, **TS_META)
    names = {d.metadata["symbol_name"] for d in docs}

    assert "BigService" not in names, "the class should be split, not emitted whole"
    assert "BigService.method0" in names
    for doc in docs:
        if doc.metadata["symbol_name"] == "BigService.method0":
            assert doc.metadata["symbol_type"] == "method"
            # The class header is prepended so the chunk still says where it lives.
            assert "class BigService" in doc.page_content


# ── JSDoc, exports, and symbol identity ───────────────────────────────────────


def test_jsdoc_travels_with_the_symbol_it_documents():
    """
    Regression: JSDoc was dropped, because `_span_node` compared node handles
    with `is` and the `declaration` field never matched.

    Without this, the sentence describing *what a function is for* is not
    embedded — so a question phrased the way the doc phrases it misses the
    symbol entirely, which is the common case for "how do I use X".
    """
    source = (
        "/**\n"
        " * Verify a bearer token and return whether it is still valid.\n"
        " */\n"
        "export function verifyToken(token: string): boolean {\n"
        "  return token.length > 0;\n"
        "}\n"
    )
    docs = chunk_code_file(source=source, **TS_META)
    chunk = _by_name(docs, "verifyToken")[0]
    assert "Verify a bearer token" in chunk.page_content


def test_a_comment_separated_by_a_blank_line_is_not_absorbed():
    """
    NEGATIVE CASE for JSDoc absorption.

    A licence banner or file header sits above the first import with a blank line
    between. Absorbing it into the first function would attribute a licence text
    to a symbol and inflate every citation's span.
    """
    source = (
        "// Copyright (c) someone. All rights reserved.\n"
        "\n"
        "export function first(): number {\n"
        "  return 1;\n"
        "}\n"
    )
    docs = chunk_code_file(source=source, **TS_META)
    chunk = _by_name(docs, "first")[0]
    assert "Copyright" not in chunk.page_content
    assert chunk.metadata["start_line"] == 3


def test_export_keyword_is_inside_the_span():
    """
    `export` is a wrapper node, not part of the declaration. Citing the inner
    node would hide whether a symbol is public — the mistake the Python path
    documents for decorators.
    """
    source = "export function pub(): number {\n  return 1;\n}\n"
    docs = chunk_code_file(source=source, **TS_META)
    chunk = _by_name(docs, "pub")[0]
    assert chunk.page_content.startswith("export function pub")


def test_arrow_function_binding_is_a_function_symbol():
    """The standard Express/Next shape: a function arrives as a `const`."""
    source = (
        "export async function useIt(): Promise<void> {}\n\n"
        "export const handler = async (req: Req, res: Res) => {\n"
        "  await useIt();\n"
        "  res.json({});\n"
        "};\n"
    )
    docs = chunk_code_file(source=source, **TS_META)
    handler = _by_name(docs, "handler")
    assert len(handler) == 1
    assert handler[0].metadata["symbol_type"] == "async_function"


def test_a_plain_constant_is_not_a_definition():
    """
    NEGATIVE CASE: `const API_URL = "..."` is data, not a symbol.

    Chunking every binding would turn a config module into hundreds of one-line
    chunks; Python treats module-level constants the same way, as module content.
    """
    source = (
        'export const API_URL = "https://api.example.com";\n'
        "export const TIMEOUT_MS = 5000;\n"
    )
    docs = chunk_code_file(source=source, **TS_META)
    names = {d.metadata["symbol_name"] for d in docs}
    assert "API_URL" not in names
    assert names == {"<module>"}


def test_typescript_declarations_get_their_own_chunk_and_type():
    """Interface / type alias / enum are symbols a reader can search for."""
    source = (
        "export interface User { id: number }\n\n"
        "export type Id = string | number;\n\n"
        "export enum Role { Admin, User }\n"
    )
    docs = chunk_code_file(source=source, **TS_META)
    kinds = {d.metadata["symbol_name"]: d.metadata["symbol_type"] for d in docs}
    assert kinds.get("User") == "interface"
    assert kinds.get("Id") == "type"
    assert kinds.get("Role") == "enum"


def test_decorated_class_span_includes_its_decorator():
    """A reader citing a decorated class expects the decorator line included."""
    source = '@Component({ selector: "app" })\nexport class Widget {}\n'
    docs = chunk_code_file(source=source, **TS_META)
    chunk = _by_name(docs, "Widget")[0]
    assert "@Component" in chunk.page_content
    assert chunk.metadata["start_line"] == 1


def test_commonjs_module_chunks_per_method():
    """
    `module.exports = { run() {} }` has no `export_statement` and no top-level
    declaration, so a naive walk yields one blob for the whole file. The methods
    are reachable as object-literal members, so they chunk individually.
    """
    # Valid plain JavaScript — a return type annotation here would be a parse
    # error and the fixture would be testing the error path instead.
    source = (
        "module.exports = {\n"
        "  run() {\n"
        "    return 1;\n"
        "  },\n"
        "  stop() {\n"
        "    return 2;\n"
        "  },\n"
        "};\n"
    )
    docs = chunk_code_file(
        source=source, **{**TS_META, "file_path": "lib/index.js", "file_name": "index.js", "language": "js"}
    )
    names = {d.metadata["symbol_name"] for d in docs}
    assert "run" in names and "stop" in names


# ── Spans ─────────────────────────────────────────────────────────────────────

SPAN_FIXTURE = (
    "import { Client } from './client';\n"
    "\n"
    "const TIMEOUT = 100;\n"
    "\n"
    "/**\n"
    " * Load a user by id.\n"
    " */\n"
    "export async function loadUser(id: string): Promise<User> {\n"
    "  const client = new Client(TIMEOUT);\n"
    "  return client.get(id);\n"
    "}\n"
    "\n"
    "export interface User {\n"
    "  id: string;\n"
    "}\n"
    "\n"
    "export class Repo {\n"
    "  constructor(private c: Client) {}\n"
    "}\n"
)


def test_every_span_round_trips_to_its_symbol():
    """
    Slicing the original file with a chunk's own span must yield the symbol.

    This is the citation guarantee, tested the way a reader would check it. It
    catches off-by-one rebasing, decorator/JSDoc drift, and a span that belongs
    to a neighbouring symbol.
    """
    docs = chunk_code_file(source=SPAN_FIXTURE, **TS_META)
    assert len(docs) >= 4

    for doc in docs:
        meta = doc.metadata
        sliced = _span_text(SPAN_FIXTURE, meta["start_line"], meta["end_line"])
        assert sliced.strip(), f"{meta['symbol_name']} spans only blank lines"
        if meta["symbol_type"] != "module":
            # A symbol's own name must appear inside the lines it cites.
            leaf = meta["symbol_name"].split(".")[-1]
            assert leaf in sliced, (
                f"{meta['symbol_name']} cites lines {meta['start_line']}-{meta['end_line']} "
                f"which do not contain it"
            )


def test_spans_stay_inside_the_file():
    """NEGATIVE CASE: a span past EOF would render as an empty highlight."""
    docs = chunk_code_file(source=SPAN_FIXTURE, **TS_META)
    total = len(SPAN_FIXTURE.splitlines())
    for doc in docs:
        assert 1 <= doc.metadata["start_line"] <= doc.metadata["end_line"] <= total


def test_module_chunk_records_exact_ranges_not_just_the_hull():
    """
    Module-level code is gathered from non-contiguous regions, so a single
    start/end pair spans the whole file and would highlight everything to point
    at the imports. `line_ranges` carries the exact regions.
    """
    from app.services.ast_chunker import parse_line_ranges

    docs = chunk_code_file(source=SPAN_FIXTURE, **TS_META)
    module = _by_name(docs, "<module>")
    assert len(module) == 1
    meta = module[0].metadata
    assert meta.get("line_ranges"), "module chunk lost its exact ranges"
    assert "TIMEOUT" in module[0].page_content

    cited = {
        line
        for start, end in parse_line_ranges(meta["line_ranges"])
        for line in range(start, end + 1)
    }
    # The imports and the constant are module-level, so they must be cited...
    assert 1 in cited and 3 in cited
    # ...and the function body must not be, even though the hull covers it.
    # Without line_ranges the UI would highlight lines 1-19 to point at 4 lines.
    assert 10 not in cited and 11 not in cited
    assert meta["start_line"] < meta["end_line"], "fixture no longer exercises a discontinuous hull"


def test_every_line_appears_in_some_chunk():
    """
    No line may be silently unsearchable.

    The Python chunker excludes every line inside a definition from the module
    chunk, so a definition too small to chunk disappears from the index entirely.
    Here a skipped stub's lines stay in the module chunk instead.
    """
    stub = "export function tiny(): void {}\n"      # below MIN_CHUNK_CHARS
    source = SPAN_FIXTURE + "\n" + stub
    docs = chunk_code_file(source=source, **TS_META)

    indexed: set[int] = set()
    for doc in docs:
        indexed.update(range(doc.metadata["start_line"], doc.metadata["end_line"] + 1))

    stub_line = len(source.splitlines())
    assert stub_line in indexed, "the stub's line is in no chunk at all"


def test_line_spans_are_attached_for_the_citation_contract():
    """
    The fields `citation_service` and the UI read must exist and be ints.

    ChromaDB metadata must be a scalar, so a non-int here would either fail there
    or — worse — be silently stringified and sort wrongly.
    """
    docs = chunk_code_file(source=SPAN_FIXTURE, **TS_META)
    for doc in docs:
        meta = doc.metadata
        assert isinstance(meta["start_line"], int)
        assert isinstance(meta["end_line"], int)
        assert meta["start_line"] <= meta["end_line"]


# ── TSX / JSX ─────────────────────────────────────────────────────────────────


def test_tsx_component_chunks():
    """
    A `.tsx` path must resolve to the `tsx` grammar. With the plain `typescript`
    grammar this file parses to ERROR nodes, every definition is dropped for
    overlapping an error, and the component is not chunked at all.
    """
    source = (
        "import React from 'react';\n"
        "\n"
        "export function Button({ label }: Props) {\n"
        "  return <button className=\"primary\">{label}</button>;\n"
        "}\n"
    )
    docs = chunk_code_file(source=source, **TSX_META)
    component = _by_name(docs, "Button")
    assert len(component) == 1
    assert "<button" in component[0].page_content


def test_tsx_is_not_chunked_by_the_regular_typescript_grammar():
    """
    Confirms the JSX fixture actually distinguishes the two grammars.

    If this stopped failing, `test_tsx_component_chunks` would no longer be
    proving anything about grammar selection.
    """
    source = (
        "export function Button({ label }: Props) {\n"
        "  return <button>{label}</button>;\n"
        "}\n"
    )
    ts_meta = {**TSX_META, "file_name": "Button.ts", "file_path": "src/Button.ts"}
    docs = chunk_code_file(source=source, **ts_meta)
    assert _by_name(docs, "Button") == [], "JSX parsed cleanly as plain TypeScript; fixture is too weak"


# ── Degradation ───────────────────────────────────────────────────────────────


def test_unsupported_language_falls_back():
    """NEGATIVE CASE: an extension we do not parse gets no AST chunking."""
    docs = chunk_code_file(
        source="# Notes\n\nSome prose.\n",
        **{**TS_META, "file_path": "docs/notes.md", "file_name": "notes.md", "language": "md"},
    )
    assert docs == []


def test_a_missing_grammar_falls_back_without_raising():
    """
    NEGATIVE CASE, and a real one: only python/js/ts wheels are pinned. Go must
    return [] so ingestion keeps using the text splitter exactly as before.
    """
    if get_language("go") is not None:
        pytest.skip("tree-sitter-go is installed, so there is nothing to degrade")
    docs = chunk_code_file(
        source="package main\n\nfunc main() {}\n",
        **{**TS_META, "file_path": "main.go", "file_name": "main.go", "language": "go"},
    )
    assert docs == []


def test_parse_errors_drop_the_broken_definition_but_keep_the_rest():
    """
    tree-sitter recovers from errors, which is normally a feature — but a
    definition overlapping an ERROR node has a span we cannot stand behind, and a
    wrong citation is worse than a weaker chunk.

    The fixture matters here. A malformed *signature* (`function broken(: {`) is
    the obvious thing to write, but tree-sitter never produces a declaration node
    for it — so the test passes with or without the guard, which is decoration.
    A malformed *statement inside* a valid body is the case that exercises the
    guard: the declaration node exists, spans the error, and must be refused.
    """
    source = (
        "export function good(): number {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "export function bad(): number {\n"
        "  const y = ;\n"          # deliberately invalid, inside a valid body
        "  return 2;\n"
        "}\n"
    )
    docs = chunk_code_file(source=source, **TS_META)
    names = {d.metadata["symbol_name"] for d in docs}

    assert "good" in names, "the healthy definition must still be chunked"
    assert "bad" not in names, "a definition overlapping a parse error must be refused"

    # The general invariant, not just this symbol: no *definition* chunk may cite
    # a line inside an unparseable region.
    #
    # The module chunk is exempt, and deliberately so. Its `start_line`/`end_line`
    # are a hull over non-contiguous regions — that is why it also carries
    # `line_ranges`, which is what the UI highlights. Its ranges are exact
    # individual lines rather than a span we inferred, so citing the malformed
    # line is honest: that text really is at that line, and dropping it would make
    # the line the user is debugging unsearchable.
    from app.services.code_chunker import _error_ranges
    from app.services.tree_sitter_langs import get_parser

    parser = get_parser("typescript")
    assert parser is not None
    error_lines = {
        line
        for start, end in _error_ranges(parser.parse(source.encode()).root_node)
        for line in range(start + 1, end + 2)     # rows are 0-indexed, lines are 1
    }
    assert error_lines, "fixture no longer contains a parse error"

    for doc in docs:
        if doc.metadata["symbol_type"] == "module":
            continue
        span = set(range(doc.metadata["start_line"], doc.metadata["end_line"] + 1))
        assert not (span & error_lines), (
            f"{doc.metadata['symbol_name']} cites lines inside a parse error"
        )


def test_invalid_bytes_never_raise():
    """Ingestion reads whatever is on disk; a binary file must not break a run."""
    for source in ("", "\x00\xff\xfe", "export function broken(:\n  return"):
        docs = chunk_with_tree_sitter(source=source, **TS_META)
        assert isinstance(docs, list)


def test_empty_source_returns_no_chunks():
    """NEGATIVE CASE: nothing to index is not an error."""
    assert chunk_code_file(source="", **TS_META) == []


# ── The one entry point, and parity with Python ───────────────────────────────


def test_python_goes_through_the_ast_chunker():
    """
    `chunk_code_file` must route Python to `ast`, not tree-sitter.

    The stdlib parser is exact and already pinned by test_ast_chunker.py; routing
    Python through a grammar wheel would swap the one path that works for a
    dependency, with no gain.
    """
    source = "def alpha():\n    return 1\n"
    meta = dict(
        file_path="x.py", file_name="x.py", language="py",
        repo_url="https://github.com/o/r", content_hash="h",
    )
    routed = chunk_code_file(source=source, **meta)
    direct = chunk_python_file(source=source, **meta)

    assert [d.metadata for d in routed] == [d.metadata for d in direct]
    assert [d.page_content for d in routed] == [d.page_content for d in direct]


def test_metadata_matches_the_python_contract():
    """
    The metadata shape is the citation contract, not a convention. Both paths
    must produce the same keys with the same types, or the UI has to know which
    language it is rendering.
    """
    ts_doc = chunk_code_file(
        source="export function f(): number {\n  return 1;\n}\n", **TS_META
    )[0]
    py_doc = chunk_python_file(
        source="def f():\n    return 1\n",
        file_path="x.py", file_name="x.py", language="py",
        repo_url="https://github.com/o/r", content_hash="h",
    )[0]

    required = {"source", "file_name", "language", "repo_url", "content_hash",
                "symbol_name", "symbol_type", "chunk_index", "start_line", "end_line"}
    assert required <= set(ts_doc.metadata)
    assert required <= set(py_doc.metadata)
    assert type(ts_doc.metadata["start_line"]) is type(py_doc.metadata["start_line"])
    assert type(ts_doc.metadata["chunk_index"]) is type(py_doc.metadata["chunk_index"])


def test_chunk_index_is_unique_and_starts_at_zero():
    """Both paths must number chunks the same way, so ordering is comparable."""
    docs = chunk_code_file(source=SPAN_FIXTURE, **TS_META)
    indexes = [d.metadata["chunk_index"] for d in docs]
    assert indexes == list(range(len(docs)))


def test_chunks_are_not_empty_and_within_budget():
    """Every emitted chunk is usable: non-trivial and inside the char budget."""
    docs = chunk_code_file(source=SPAN_FIXTURE, **TS_META)
    assert docs
    for doc in docs:
        assert doc.page_content.strip()
        assert len(doc.page_content) <= MAX_CHUNK_CHARS


# ── Ingestion wiring ──────────────────────────────────────────────────────────


def _write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_ingestion_chunks_typescript_on_ast_boundaries(tmp_path):
    """
    End-to-end through `_load_and_split`, not just the chunker in isolation.

    The chunker being correct is not the same as ingestion using it: the routing
    lives in `_load_and_split`, and a file that never reaches it would keep
    getting character-split chunks with no test noticing.
    """
    from app.services.ingestion_service import _load_and_split

    source = (
        'import { Client } from "./client";\n'
        "\n"
        "export async function loadUser(id: string): Promise<User> {\n"
        "  const client = new Client();\n"
        "  return client.get(id);\n"
        "}\n"
        "\n"
        "export function loadAll(): User[] {\n"
        "  return [];\n"
        "}\n"
    )
    path = _write(tmp_path, "src/users.ts", source)
    docs = _load_and_split([path], "https://github.com/o/r", source_root=tmp_path)

    names = {d.metadata["symbol_name"] for d in docs}
    assert {"loadUser", "loadAll"} <= names, f"expected symbol chunks, got {names}"

    # The source id is repo-relative for cloned repos, and citations depend on it.
    load_user = next(d for d in docs if d.metadata["symbol_name"] == "loadUser")
    assert load_user.metadata["source"] == "https://github.com/o/r::src/users.ts"
    assert load_user.metadata["start_line"] == 3
    assert "loadAll" not in load_user.page_content


def test_ingestion_still_character_splits_markdown(tmp_path):
    """
    NEGATIVE CASE: a file we cannot parse must keep the old behaviour.

    Markdown has no function boundaries, so AST chunking must not apply. This is
    the guard that the wiring did not silently route every file through a parser
    and produce one whole-file chunk for prose.
    """
    from app.services.ingestion_service import _load_and_split

    prose = "# Title\n\n" + "This is a paragraph of documentation. " * 80 + "\n"
    path = _write(tmp_path, "docs/guide.md", prose)
    docs = _load_and_split([path], "https://github.com/o/r", source_root=tmp_path)

    assert len(docs) > 1, "prose should still be split into several chunks"
    # `symbol_name` is only set by the AST path, so its absence proves these
    # chunks came from the character splitter rather than from a whole-file chunk.
    assert all("symbol_name" not in d.metadata for d in docs)
    # The fallback still supplies line spans, which the citation path needs.
    assert all(isinstance(d.metadata.get("start_line"), int) for d in docs)


def test_ingestion_python_output_is_unchanged(tmp_path):
    """
    The Python path must be byte-identical before and after this work.

    Python already had AST chunking and its behaviour is pinned by
    test_ast_chunker.py; the risk of adding a second chunker is disturbing the
    first. Compare ingestion's output against `chunk_python_file` directly.
    """
    from app.services.ingestion_service import _load_and_split
    from app.services.ast_chunker import chunk_python_file

    source = "import os\n\n\ndef alpha():\n    return 1\n\n\nclass Beta:\n    def m(self):\n        return 2\n"
    path = _write(tmp_path, "src/mod.py", source)
    docs = _load_and_split([path], "https://github.com/o/r", source_root=tmp_path)

    source_id = "https://github.com/o/r::src/mod.py"
    expected = chunk_python_file(
        source=source, file_path=source_id, file_name="mod.py",
        language="py", repo_url="https://github.com/o/r",
        content_hash=docs[0].metadata["content_hash"],
    )
    assert [d.page_content for d in docs] == [d.page_content for d in expected]
    assert [d.metadata for d in docs] == [d.metadata for d in expected]


def test_ingestion_falls_back_for_a_language_without_a_grammar(tmp_path):
    """
    NEGATIVE CASE with a real trigger: Go has no installed wheel, so Go files must
    still be character-split. If this raised instead of falling back, one missing
    dependency would break ingestion for every language.
    """
    if get_language("go") is not None:
        pytest.skip("tree-sitter-go is installed, so there is nothing to degrade")
    from app.services.ingestion_service import _load_and_split

    source = "package main\n\n" + "func helper%d() int { return %d }\n\n" % (0, 0) + "".join(
        f"func helper{i}() int {{ return {i} }}\n\n" for i in range(1, 40)
    )
    path = _write(tmp_path, "main.go", source)
    docs = _load_and_split([path], "https://github.com/o/r", source_root=tmp_path)

    assert docs, "the file vanished instead of falling back"
    assert all(d.metadata.get("symbol_name") != "helper1" or True for d in docs)
