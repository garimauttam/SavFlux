"""
tree_sitter_langs.py — One place that knows which tree-sitter grammar a file needs.

WHY THIS MODULE EXISTS
----------------------
`ast_chunker.py` gives Python files AST-boundary chunks, and that is what makes
retrieval precise: "how does verify_token work?" returns exactly the
`verify_token` chunk instead of a 300-line window that starts mid-function and
ends in unrelated helper code. Every other language falls back to
`RecursiveCharacterTextSplitter`, which counts characters and will happily cut a
function in half.

This module is the first half of closing that gap: it resolves a file path to a
grammar and hands back a configured parser. `tree_sitter_chunker.py` uses it to
cut chunks. Nothing else needs to know how parsing works.

WHY OFFICIAL PER-LANGUAGE WHEELS, AND NOT `tree-sitter-language-pack`
--------------------------------------------------------------------
`tree-sitter-language-pack` advertises 371 grammars in a ~2.4 MB wheel, and the
arithmetic is the tell: it does not ship them. Version 1.20.0 downloads a
manifest and then each grammar on first use, so `get_language("typescript")`
fails outright on a machine that cannot reach GitHub releases:

    tree_sitter_language_pack.DownloadError: Download error: Failed to fetch
    manifest from https://github.com/xberg-io/.../v1.20.0/parsers.json

Chunking quality would then depend on network access at ingest time — in a
project whose entire pitch is "$0, works offline", where the *fallback* is
silent. You would get character-split chunks and no error telling you why.

The official `tree-sitter-<lang>` wheels from the tree-sitter org compile the
grammar into the wheel, so parsing never touches the network. The trade is
explicit: one dependency per language instead of one for all of them. That is the
right side of the trade, because a grammar that is missing degrades to the old
splitter (below) rather than to a network call.

WHY THE PARSER IS THREAD-LOCAL
------------------------------
`Language` is immutable, so it is cached process-wide and shared freely.
`Parser` is **not** thread-safe — it holds mutable state for the parse in
progress. Two threads sharing one Parser corrupt each other's trees instead of
raising, which is the worst possible failure mode here: wrong chunks, indexed
silently, discovered only as "retrieval feels bad".

Ingestion is concurrent (`asyncio.to_thread` behind the bulk endpoints), so
parsers live in `threading.local()`. Constructing one costs microseconds;
sharing one is a data race.

GRACEFUL DEGRADATION
--------------------
Every accessor returns `None` rather than raising when tree-sitter or a grammar
wheel is absent. Callers fall back to `RecursiveCharacterTextSplitter`, which is
exactly the pre-existing behaviour — so a missing grammar can only ever be as
bad as the code we already had, never worse. `capabilities()` reports what
actually loaded, so diagnostics can say so out loud instead of guessing.

COST: no model, no network, no API key. Parsing a file is CPU-only and runs at
roughly 1 MB/s–10 MB/s per core depending on grammar.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Extension → grammar ───────────────────────────────────────────────────────
#
# Keys are lowercase extensions WITH the dot, matching `Path.suffix` output.
#
# Note `.tsx` maps to its own grammar, not to `typescript`. That is a
# correctness requirement, not a preference: JSX is not valid TypeScript, so the
# `typescript` grammar produces ERROR nodes on any `.tsx` file that returns JSX —
# which is most components in a React codebase. `tree_sitter_typescript` ships
# both grammars (`language_typescript` / `language_tsx`) for exactly this reason.
# test_tree_sitter_langs.py asserts this with a real JSX fixture.
#
# Data and documentation formats are deliberately ABSENT. `.md`, `.json`,
# `.yaml` and `.txt` have no meaningful function/class boundaries, so
# AST-chunking them would produce one giant "module" chunk per file — strictly
# worse than the text splitter they use today. Absence here is the signal to fall
# back, which is why `test_data_and_doc_formats_have_no_grammar` pins it.
EXTENSION_TO_GRAMMAR: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
}


# ── Grammar → where it comes from ─────────────────────────────────────────────
#
# `(importable module, attribute returning the language)`. Grammar wheels differ
# in naming: most expose `language()`, but the TypeScript wheel exposes
# `language_typescript()` and `language_tsx()` because it ships two grammars.
#
# Grammars listed here whose wheel is not installed resolve to `None` and fall
# back — that is the intended, tested behaviour, not an oversight. Python, JS and
# TS are pinned in requirements.txt; the rest are one `pip install` away.
_GRAMMAR_LOADERS: dict[str, tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
}

#: Every grammar this module can name, whether or not it is installed.
KNOWN_GRAMMARS: tuple[str, ...] = tuple(sorted(_GRAMMAR_LOADERS))

#: Per-thread parser cache. See "WHY THE PARSER IS THREAD-LOCAL" above — this is
#: the one piece of state in the module that must not be shared.
_thread_state = threading.local()


def _language_class() -> Any:
    """Import `tree_sitter.Language`, or return None when the core lib is absent."""
    try:
        from tree_sitter import Language  # noqa: PLC0415 - optional dependency

        return Language
    except Exception as exc:  # noqa: BLE001 - any import failure means "unavailable"
        logger.debug("tree-sitter core unavailable: %s", exc)
        return None


@lru_cache(maxsize=None)
def get_language(grammar: str) -> Any:
    """
    Return the `Language` for a grammar name, or `None` if it cannot be loaded.

    Cached process-wide: a `Language` is immutable, so one instance serves every
    thread. Failures are cached too — a missing wheel will not appear at runtime,
    and retrying the import on every file would turn a one-off miss into a
    per-file cost on a large repo.

    Returning `None` instead of raising is deliberate: the caller's fallback
    (character splitting) is the behaviour we already shipped, so an unavailable
    grammar degrades rather than breaks.
    """
    module_name, attr = _GRAMMAR_LOADERS.get(grammar, ("", ""))
    if not module_name:
        logger.debug("no loader registered for grammar %r", grammar)
        return None

    Language = _language_class()
    if Language is None:
        return None

    try:
        module = __import__(module_name, fromlist=[attr])
        raw = getattr(module, attr)()
    except Exception as exc:  # noqa: BLE001 - missing wheel, ABI mismatch, ...
        logger.info("grammar %r unavailable (%s): %s", grammar, module_name, exc)
        return None

    # Grammar wheels return a PyCapsule that `Language` wraps. Accept an already
    # wrapped `Language` too, so a future wheel that returns one directly does
    # not silently break here.
    if isinstance(raw, Language):
        return raw
    try:
        return Language(raw)
    except Exception as exc:  # noqa: BLE001 - ABI mismatch between lib and wheel
        logger.warning("grammar %r failed to load: %s", grammar, exc)
        return None


def get_parser(grammar: str) -> Any:
    """
    Return a `Parser` for a grammar name, or `None` if the grammar is unavailable.

    Parsers are cached **per thread**. py-tree-sitter's `Parser` carries mutable
    parse state, so sharing one between threads yields corrupted trees rather
    than an exception. Construction is cheap enough that per-thread copies cost
    nothing measurable.
    """
    language = get_language(grammar)
    if language is None:
        return None

    cache: dict[str, Any] = getattr(_thread_state, "parsers", None)
    if cache is None:
        cache = {}
        _thread_state.parsers = cache

    parser = cache.get(grammar)
    if parser is None:
        try:
            from tree_sitter import Parser  # noqa: PLC0415 - optional dependency

            parser = Parser(language)
        except Exception as exc:  # noqa: BLE001 - core lib present, API mismatch
            logger.warning("could not build a %r parser: %s", grammar, exc)
            return None
        cache[grammar] = parser
    return parser


def grammar_for_path(path: str | Path) -> str | None:
    """
    Grammar name for a file path, or `None` when the file should not be AST-parsed.

    Accepts plain paths, `Path` objects, and SavFlux source ids
    (`"https://github.com/o/r::src/App.tsx"`) — all three end in the extension, so
    suffix matching handles them uniformly. Extension matching is case-insensitive
    because `.TSX` is valid on a case-insensitive filesystem and Windows users do
    commit such files.
    """
    suffix = Path(str(path)).suffix.lower()
    if not suffix:
        return None
    return EXTENSION_TO_GRAMMAR.get(suffix)


def is_supported(path: str | Path) -> bool:
    """True when this path maps to a grammar, regardless of whether it loaded."""
    return grammar_for_path(path) is not None


def capabilities() -> dict[str, Any]:
    """
    What actually loaded, for diagnostics — never raises.

    Reported so a UI or `/health` can state the truth ("JS/TS chunking: AST" vs
    "character") instead of implying equal quality everywhere. Silently getting
    worse chunks is how a retrieval regression survives to production.
    """
    grammars = {name: get_language(name) is not None for name in KNOWN_GRAMMARS}
    core = _language_class() is not None
    return {
        "tree_sitter": core,
        "grammars": grammars,
        "available": sorted(n for n, ok in grammars.items() if ok),
        "unavailable": sorted(n for n, ok in grammars.items() if not ok),
    }
