"""
test_tree_sitter_langs.py — Grammar resolution, and the two failure modes that
would be silent.

WHAT THESE TESTS PROTECT
------------------------
1. **`.tsx` must not be parsed with the `typescript` grammar.** JSX is not valid
   TypeScript, so the `typescript` grammar emits ERROR nodes on any component
   that returns markup — which is most of a React codebase. This is asserted
   behaviourally (against a real JSX fixture), not by reading a dict, because the
   dict could be correct while the loader handed back the wrong grammar.

2. **A missing grammar must degrade, never raise.** The fallback is
   `RecursiveCharacterTextSplitter` — the behaviour we already shipped. So an
   unavailable wheel can only be as bad as the old code, never worse. If a
   missing wheel raised instead, one absent dependency would break ingestion for
   every language, including the ones that work.

3. **Parsers must not be shared between threads.** `Parser` holds mutable parse
   state; sharing one corrupts trees without raising, so the symptom would be
   "retrieval feels bad" rather than an error. `Language` is immutable and SHOULD
   be shared, so the two are asserted in opposite directions.

The whole module is pure CPU: no model, no network, no API key.
"""

from __future__ import annotations

import threading

import pytest

from app.services import tree_sitter_langs as tsl


@pytest.fixture(autouse=True)
def _clear_language_cache():
    """
    Drop the process-wide `Language` cache around every test.

    `get_language` is `lru_cache`d on purpose (a grammar import must happen once,
    not per file), which means a test that monkeypatches a loader would otherwise
    poison every test after it.
    """
    tsl.get_language.cache_clear()
    yield
    tsl.get_language.cache_clear()


def _require(grammar: str):
    """Skip with a clear reason when a grammar wheel is not installed."""
    parser = tsl.get_parser(grammar)
    if parser is None:
        pytest.skip(f"grammar {grammar!r} is not installed")
    return parser


def _walk_types(node) -> set[str]:
    """Every node type in the tree — how a chunker finds functions and classes."""
    found = {node.type}
    for child in node.children:
        found |= _walk_types(child)
    return found


def _has_error(node) -> bool:
    """True when any node in the tree is an ERROR or a missing token."""
    if node.type == "ERROR" or node.is_missing:
        return True
    return any(_has_error(child) for child in node.children)


# ── Extension → grammar ───────────────────────────────────────────────────────


def test_tsx_maps_to_its_own_grammar():
    """
    MUTATION CHECK: point `.tsx` at "typescript" and this fails, along with the
    JSX behavioural test below. That pairing is the point — the mapping is only
    correct because the loader can actually deliver that grammar.
    """
    assert tsl.grammar_for_path("src/components/App.tsx") == "tsx"
    assert tsl.grammar_for_path("App.TSX") == "tsx"


def test_typescript_and_javascript_families_map_to_distinct_grammars():
    """
    The repo previously collapsed `.ts`/`.tsx` onto `Language.JS` ("TS shares JS
    splitter rules"). They are separate grammars now, so pin each family.
    """
    for path in ("a.ts", "b.mts", "c.cts"):
        assert tsl.grammar_for_path(path) == "typescript"
    for path in ("a.js", "b.jsx", "c.mjs", "d.cjs"):
        assert tsl.grammar_for_path(path) == "javascript"


def test_python_paths_resolve_to_the_python_grammar():
    """The registry must cover Python too, so Step 2 has one entry point."""
    assert tsl.grammar_for_path("backend/app/services/auth.py") == "python"
    assert tsl.grammar_for_path("stubs/types.pyi") == "python"


@pytest.mark.parametrize("path", ["README.md", "data.json", "ci.yaml", "x.yml", "notes.txt"])
def test_data_and_doc_formats_have_no_grammar(path):
    """
    NEGATIVE CASE: absent from the map on purpose.

    A markdown or JSON file has no function boundaries, so AST chunking would
    produce a single "module" chunk covering the whole file — strictly worse than
    the text splitter it uses today. `None` here is the signal to fall back.
    """
    assert tsl.grammar_for_path(path) is None
    assert tsl.is_supported(path) is False


@pytest.mark.parametrize("path", ["Makefile", "LICENSE", "script", "weird.zzz", ".gitignore"])
def test_unknown_and_extensionless_paths_have_no_grammar(path):
    """NEGATIVE CASE: unknown input must not be guessed at."""
    assert tsl.grammar_for_path(path) is None


def test_source_ids_and_path_objects_resolve(tmp_path):
    """
    Ingestion hands round SavFlux source ids, not just paths:
    `"{repo_url}::{rel_path}"`. All three shapes must resolve the same way, or
    citation spans would work for one caller and not another.
    """
    from pathlib import Path

    assert tsl.grammar_for_path("https://github.com/o/r::src/App.tsx") == "tsx"
    assert tsl.grammar_for_path(Path("src/App.tsx")) == "tsx"
    assert tsl.grammar_for_path(tmp_path / "main.go") == "go"


def test_extension_matching_is_case_insensitive():
    """`.PY` is valid on a case-insensitive filesystem and does get committed."""
    assert tsl.grammar_for_path("AUTH.PY") == "python"
    assert tsl.grammar_for_path("Component.TsX") == "tsx"


# ── The claim that matters: JSX needs the tsx grammar ─────────────────────────

JSX_FIXTURE = b"""export function Button({ label }: Props) {
  return <button className="primary">{label}</button>;
}
"""


def test_jsx_parses_cleanly_through_the_registry():
    """
    End-to-end through our own accessors: a `.tsx` path resolves to a grammar
    that can actually parse JSX.
    """
    parser = _require(tsl.grammar_for_path("src/Button.tsx"))
    tree = parser.parse(JSX_FIXTURE)
    assert _has_error(tree.root_node) is False


def test_jsx_fails_to_parse_under_the_typescript_grammar():
    """
    The counterfactual that justifies a separate `tsx` entry.

    If this ever stops failing, the mapping is not load-bearing and the extra
    grammar can be dropped. Until then, `typescript` on a `.tsx` file destroys
    the file's structure — and a chunker silently falls back to character
    splitting or, worse, cuts chunks along ERROR boundaries.
    """
    ts_parser = _require("typescript")
    tsx_parser = _require("tsx")

    assert _has_error(ts_parser.parse(JSX_FIXTURE).root_node) is True
    assert _has_error(tsx_parser.parse(JSX_FIXTURE).root_node) is False


def test_registry_yields_trees_with_function_nodes():
    """
    Step 2's precondition: parsing must expose function boundaries as nodes.

    If the tree only ever contained `program` and `ERROR`, an AST chunker built on
    it would produce the same single-chunk-per-file output as today.
    """
    parser = _require("typescript")
    tree = parser.parse(b"export function verifyToken(t: string): boolean {\n  return t.length > 0;\n}\n")
    assert _has_error(tree.root_node) is False
    assert "function_declaration" in _walk_types(tree.root_node)


# ── Degradation ───────────────────────────────────────────────────────────────


def test_a_grammar_without_its_wheel_degrades_to_none():
    """
    NEGATIVE CASE, and a real one: only python/js/ts wheels are pinned, so `go`
    legitimately has no grammar installed. It must resolve to `None` without
    raising, so Go files keep using the text splitter exactly as before.
    """
    if tsl.get_language("go") is not None:
        pytest.skip("tree-sitter-go is installed, so there is nothing to degrade")
    assert tsl.get_parser("go") is None


def test_unknown_grammar_name_returns_none():
    """NEGATIVE CASE: a name with no loader entry is not an exception."""
    assert tsl.get_language("cobol") is None
    assert tsl.get_parser("cobol") is None


def test_broken_loader_entry_returns_none(monkeypatch):
    """A wheel that is listed but fails to import degrades instead of raising."""
    monkeypatch.setitem(tsl._GRAMMAR_LOADERS, "python", ("tree_sitter_does_not_exist", "language"))
    tsl.get_language.cache_clear()
    assert tsl.get_language("python") is None
    assert tsl.get_parser("python") is None


def test_capabilities_survives_a_missing_core_library(monkeypatch):
    """
    Degradation of the whole subsystem: with no tree-sitter at all, every accessor
    still answers, and `capabilities()` reports the truth rather than raising into
    a health check.
    """
    monkeypatch.setattr(tsl, "_language_class", lambda: None)
    tsl.get_language.cache_clear()

    caps = tsl.capabilities()
    assert caps["tree_sitter"] is False
    assert caps["available"] == []
    assert sorted(caps["unavailable"]) == sorted(tsl.KNOWN_GRAMMARS)
    assert tsl.get_parser("python") is None


def test_invalid_source_bytes_never_raise():
    """
    NEGATIVE CASE: ingestion reads whatever is on disk. A truncated or binary file
    must not take down an ingest run.
    """
    parser = _require("python")
    for source in (b"", b"\x00\xff\xfe", b"def broken(:\n  pass", b"\xf0\x9f\x92\xa9" * 50):
        tree = parser.parse(source)
        assert tree.root_node is not None


# ── Caching and thread safety ─────────────────────────────────────────────────


def test_parser_is_reused_within_a_thread():
    """A parser is not rebuilt per file — that would be a per-file cost on a repo."""
    _require("python")
    assert tsl.get_parser("python") is tsl.get_parser("python")


def test_language_is_shared_across_threads():
    """`Language` is immutable, so one instance should serve every thread."""
    _require("python")
    barrier = threading.Barrier(2, timeout=10)
    results: dict[str, object] = {}

    def grab(key: str) -> None:
        barrier.wait()
        results[key] = tsl.get_language("python")

    threads = [threading.Thread(target=grab, args=(f"t{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert results["t0"] is results["t1"]


def test_parser_is_not_shared_across_threads():
    """
    The failure mode this prevents is silent, so assert it directly.

    py-tree-sitter's `Parser` is not thread-safe. Sharing one across threads gives
    corrupted trees rather than an exception, which would surface as inexplicably
    bad retrieval. Each thread must therefore get its own.
    """
    _require("python")
    barrier = threading.Barrier(2, timeout=10)
    # Hold the parsers, not just their ids: a collected object's id can be reused,
    # which would let this test pass (or fail) for the wrong reason.
    results: dict[str, object] = {}

    def grab(key: str) -> None:
        barrier.wait()
        results[key] = tsl.get_parser("python")

    threads = [threading.Thread(target=grab, args=(f"t{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert results["t0"] is not None
    assert results["t0"] is not results["t1"]


def test_capabilities_reports_every_known_grammar():
    """
    `available` and `unavailable` must together partition the known set, so a
    diagnostic can name what is missing instead of implying uniform quality.
    """
    caps = tsl.capabilities()
    assert set(caps["grammars"]) == set(tsl.KNOWN_GRAMMARS)
    assert set(caps["available"]) | set(caps["unavailable"]) == set(tsl.KNOWN_GRAMMARS)
    assert not (set(caps["available"]) & set(caps["unavailable"]))


def test_pinned_grammars_are_available():
    """
    Guards the promise requirements.txt makes. If a pinned wheel is missing, the
    JS/TS chunking this work exists to deliver is silently absent — so this fails
    loudly instead of skipping.
    """
    for grammar in ("python", "javascript", "typescript", "tsx"):
        assert tsl.get_language(grammar) is not None, f"{grammar} grammar is not installed"
