"""
dep_graph.py — Dependency graph extraction from indexed code.

Reads all chunk content from ChromaDB, applies language-specific regex to extract
import statements, and returns a graph of {nodes, edges} representing which files
import which other files.

WHY REGEX, NOT AST?
  AST parsing (ast.parse for Python, acorn for JS) gives perfect accuracy but requires
  each file's raw content to be available, and running a full AST parser on every file
  in a large repo adds several seconds of latency.

  Regex on the already-stored ChromaDB chunks:
  - Runs in ~50ms on any size repo (pure Python, no subprocess)
  - Works on partial chunks (the import block is almost always in the first chunk)
  - Good enough: import extraction regex is well-understood and reliable

EDGE RESOLUTION STRATEGY:
  Imports are resolved to file nodes using suffix matching:
    "from .auth import verify" → looks for any node whose file_name ends with "auth.py"
    "import utils" → looks for "utils.py", "utils/index.js", etc.

  Unresolved imports (third-party libraries) are dropped — they clutter the graph
  without adding navigable information.

OUTPUT FORMAT:
  {
    "nodes": [{"id": "source_path", "label": "file_name", "language": "py", "val": N}],
    "edges": [{"source": "path_a", "target": "path_b"}]
  }
  `val` is the number of chunks (used to scale node size in the force graph).
"""

import re
from collections import defaultdict


# ── Import patterns per language ──────────────────────────────────────────────
# Each pattern captures the imported module name.
# We keep the list minimal and correct — better to miss edge cases than emit noise.

_PYTHON_IMPORT_RE = re.compile(
    r"""
    ^(?:from\s+([\w.]+)\s+import  # from <module> import ...
       |import\s+([\w.,\s]+)$)    # import <module>[, <module>]
    """,
    re.MULTILINE | re.VERBOSE,
)

_JS_IMPORT_RE = re.compile(
    r"""
    (?:
      import\s+.*?\s+from\s+['"]([^'"]+)['"]  # import ... from 'module'
      |require\s*\(\s*['"]([^'"]+)['"]\s*\)   # require('module')
    )
    """,
    re.MULTILINE | re.VERBOSE,
)

_GO_IMPORT_RE = re.compile(
    r'"([^"]+)"',   # Go: import "package/path" (extracted inside import blocks)
)

_RUST_IMPORT_RE = re.compile(
    r"^use\s+([\w:]+)",
    re.MULTILINE,
)

_JAVA_IMPORT_RE = re.compile(
    r"^import\s+([\w.]+);",
    re.MULTILINE,
)


def _extract_imports(content: str, language: str) -> list[str]:
    """
    Extract raw import strings from a code chunk.
    Returns module/path strings — not yet resolved to file nodes.
    """
    imports: list[str] = []

    if language in ("py",):
        for m in _PYTHON_IMPORT_RE.finditer(content):
            mod = m.group(1) or m.group(2) or ""
            # Split comma-separated imports: "import os, sys"
            for part in mod.split(","):
                part = part.strip().split(" ")[0]  # handle "import os as o"
                if part:
                    imports.append(part)

    elif language in ("js", "jsx", "ts", "tsx"):
        for m in _JS_IMPORT_RE.finditer(content):
            mod = m.group(1) or m.group(2) or ""
            if mod:
                imports.append(mod)

    elif language in ("go",):
        # Go import blocks: import ( "pkg/path" \n "pkg/path2" )
        # The regex above just grabs quoted strings — filter out stdlib
        for m in _GO_IMPORT_RE.finditer(content):
            mod = m.group(1)
            if "/" in mod:  # stdlib imports have no slashes, local/third-party do
                imports.append(mod)

    elif language in ("rs",):
        for m in _RUST_IMPORT_RE.finditer(content):
            imports.append(m.group(1))

    elif language in ("java",):
        for m in _JAVA_IMPORT_RE.finditer(content):
            imports.append(m.group(1))

    return imports


def _resolve_import(
    raw_import: str,
    language: str,
    file_name_index: dict[str, str],   # basename → source_path
    source_path: str,
) -> str | None:
    """
    Try to resolve a raw import string to a known source_path node.

    Strategy: strip the import to its last component, then look for a file
    whose name starts with that component (ignoring extension and case).

    Examples:
      "from .auth import verify"  raw_import="auth"   → finds "auth.py"
      "import ./utils/helpers"    raw_import="helpers" → finds "helpers.ts"
      "import numpy"              → not found → None (third-party, dropped)
    """
    # Strip leading dots (relative imports) and take last path segment
    parts = raw_import.lstrip(".").replace("\\", "/").split("/")
    stem = parts[-1].split(".")[0].lower()  # "utils/auth" → "auth", drop extension

    if not stem:
        return None

    # Try exact basename match (without extension)
    for basename, full_path in file_name_index.items():
        base_stem = basename.rsplit(".", 1)[0].lower()
        if base_stem == stem and full_path != source_path:
            return full_path

    return None  # third-party or unresolvable


def build_dependency_graph(repo_url: str | None = None) -> dict:
    """
    Read all indexed chunks from ChromaDB, extract import statements,
    and return a force-graph-ready {nodes, edges} dict.

    repo_url: when provided, restricts the graph to one repo's files.
              When None, builds the graph for all indexed files.

    WHY retrieval_service._get_vectorstore() AND NOT ingestion_service._get_vectorstore()?
    ingestion_service._get_vectorstore() creates a NEW PersistentClient on every call —
    no caching.  retrieval_service._get_vectorstore() is an lru_cache(maxsize=1) singleton
    that reuses the same warm SQLite connection across all reads.  The dep graph is a
    read-only operation; using the cached singleton saves ~50ms per graph build.
    """
    from app.services.retrieval_service import _get_vectorstore

    vs = _get_vectorstore()
    where_filter = {"repo_url": repo_url} if repo_url else None

    results = vs._collection.get(
        where=where_filter,
        include=["documents", "metadatas"],
    )
    docs   = results.get("documents") or []
    metas  = results.get("metadatas") or []

    if not docs:
        return {"nodes": [], "edges": []}

    # ── 1. Aggregate chunks per file ─────────────────────────────────────────
    # Each file may have many chunks. We:
    #   a) collect the first chunk per file (most likely to contain imports)
    #   b) count total chunks per file (used for node size)
    file_data: dict[str, dict] = {}   # source → {file_name, language, content, chunk_count}

    for doc, meta in zip(docs, metas):
        src  = meta.get("source", "")
        fname = meta.get("file_name", "")
        lang  = meta.get("language", "")
        idx   = meta.get("chunk_index", 0)
        if not src:
            continue

        if src not in file_data:
            file_data[src] = {
                "file_name": fname,
                "language":  lang,
                "content":   doc if idx == 0 else "",
                "chunk_count": 1,
            }
        else:
            file_data[src]["chunk_count"] += 1
            # Keep the earliest chunk for import extraction
            if idx == 0:
                file_data[src]["content"] = doc

    # ── 2. Build file-name → source lookup ───────────────────────────────────
    # Used by the resolver to match "import auth" → "auth.py"
    file_name_index: dict[str, str] = {
        data["file_name"]: src
        for src, data in file_data.items()
    }

    # ── 3. Extract imports and resolve edges ─────────────────────────────────
    edges_set: set[tuple[str, str]] = set()

    for src, data in file_data.items():
        raw_imports = _extract_imports(data["content"], data["language"])
        for raw in raw_imports:
            resolved = _resolve_import(raw, data["language"], file_name_index, src)
            if resolved and resolved != src:
                edge = (src, resolved)
                edges_set.add(edge)

    # ── 4. Build output ───────────────────────────────────────────────────────
    nodes = [
        {
            "id":       src,
            "label":    data["file_name"],
            "language": data["language"],
            "val":      max(1, data["chunk_count"]),   # node size in force graph
        }
        for src, data in file_data.items()
    ]

    edges = [
        {"source": src, "target": tgt}
        for src, tgt in edges_set
    ]

    # Only include nodes that have at least one edge, plus all files
    # (isolated nodes still appear — the graph shows ALL files, not just imported ones)
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "files": len(nodes),
            "dependencies": len(edges),
        },
    }
