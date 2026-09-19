"""
file_tree_service.py — P2 File Tree Explorer ($0, local aggregation)

Builds a folder tree from indexed files (from Chroma) for fast navigation.
No DB, no LLM — pure python grouping, $0.

Tree node shape:
  {
    "name": "src",
    "path": "src",
    "type": "dir",
    "children": [...],
    "file_count": 3,
    "languages": {"python": 2, "text":1}
  }
  File leaf:
  {
    "name": "app.py",
    "path": "src/app.py" or "https://github.com/o/r::src/app.py",
    "type": "file",
    "language": "python",
    "source": "src/app.py",
    "repo_url": "https://github.com/o/r",
    "chunk_count": 12
  }
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings

settings = get_settings()

def _get_indexed_files_sync() -> list[dict[str, Any]]:
    """Try to get indexed files via retrieval_service, fallback to Chroma direct."""
    try:
        from app.services.retrieval_service import get_indexed_files
        import asyncio
        try:
            # If no running loop, use asyncio.run
            return asyncio.run(get_indexed_files())
        except RuntimeError:
            # Already in loop — fallback to chroma direct
            raise
    except Exception:
        pass
    # Fallback: direct Chroma read
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings
        client = chromadb.PersistentClient(
            path=settings.chroma_persist_directory,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        coll = client.get_or_create_collection(settings.chroma_collection_name)
        res = coll.get(include=["metadatas"])
        metas = res.get("metadatas", []) if isinstance(res, dict) else getattr(res, "metadatas", [])
        files: dict[str, dict] = {}
        for m in metas:
            src = (m or {}).get("source", "")
            if not src:
                continue
            if src not in files:
                files[src] = {
                    "source": src,
                    "file_name": src.split("/")[-1].split("::")[-1],
                    "language": (m or {}).get("language", "text"),
                    "repo_url": (m or {}).get("repo_url", ""),
                    "chunk_count": 0,
                }
            files[src]["chunk_count"] = files[src].get("chunk_count", 0) + 1
        return list(files.values())
    except Exception:
        return []

def build_file_tree(files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if files is None:
        files = _get_indexed_files_sync()

    # Normalize file paths: handle "https://github.com/o/r::src/a.py" -> "src/a.py"
    normalized: list[dict[str, Any]] = []
    for f in files:
        src = f.get("source", "")
        # Extract path part after ::
        path = src.split("::")[-1] if "::" in src else src
        path = path.strip().lstrip("/")
        if not path:
            path = f.get("file_name", "unknown")
        normalized.append({
            **f,
            "path": path,
            "display_path": path,
        })

    # Build nested dict
    root: dict[str, Any] = {"name": "", "path": "", "type": "dir", "children": {}, "file_count": 0, "languages": {}}

    for f in normalized:
        parts = f["path"].split("/")
        cur = root
        cur_path = ""
        for i, part in enumerate(parts):
            is_file = i == len(parts) - 1
            cur_path = f"{cur_path}/{part}" if cur_path else part
            if is_file:
                # File leaf
                cur["children"][part] = {
                    "name": part,
                    "path": cur_path,
                    "type": "file",
                    "language": f.get("language", "text"),
                    "source": f.get("source", cur_path),
                    "repo_url": f.get("repo_url", ""),
                    "chunk_count": f.get("chunk_count", 1),
                    "file_name": f.get("file_name", part),
                }
                # Propagate file_count up (we'll compute via traversal after)
            else:
                if part not in cur["children"]:
                    cur["children"][part] = {"name": part, "path": cur_path, "type": "dir", "children": {}, "file_count": 0, "languages": {}}
                cur = cur["children"][part]

    # Convert children dict to sorted list and compute aggregates
    def finalize(node: dict) -> dict:
        if node["type"] == "dir":
            children_dict = node.pop("children", {})
            # Recurse
            children_list: list[dict] = []
            file_count = 0
            langs: dict[str, int] = {}
            for child in children_dict.values():
                finalized = finalize(child)
                children_list.append(finalized)
                if finalized["type"] == "file":
                    file_count += 1
                    lang = finalized.get("language", "text")
                    langs[lang] = langs.get(lang, 0) + 1
                else:
                    file_count += finalized.get("file_count", 0)
                    for k, v in finalized.get("languages", {}).items():
                        langs[k] = langs.get(k, 0) + v
            # Sort: dirs first, then files, each alphabetically
            children_list.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
            node["children"] = children_list
            node["file_count"] = file_count
            node["languages"] = langs
        return node

    tree = finalize(root)
    # Build flat list for search
    flat: list[dict] = []
    def collect(n: dict):
        if n["type"] == "file":
            flat.append(n)
        else:
            for c in n.get("children", []):
                collect(c)
    collect(tree)

    return {
        "tree": tree,
        "flat": flat,
        "total_files": len(flat),
        "total_dirs": len([n for n in _iter_dirs(tree)]),
    }

def _iter_dirs(node: dict):
    if node["type"] == "dir":
        yield node
        for c in node.get("children", []):
            if c["type"] == "dir":
                yield from _iter_dirs(c)

def search_files(query: str, limit: int = 20) -> list[dict[str, Any]]:
    if not query or not query.strip():
        files = _get_indexed_files_sync()
        return files[:limit]
    q = query.lower().strip()
    files = _get_indexed_files_sync()
    scored: list[tuple[int, dict]] = []
    for f in files:
        hay = f"{f.get('source','')} {f.get('file_name','')} {f.get('language','')}".lower()
        score = -1
        if q == hay.strip():
            score = 100
        elif f.get("source","").lower().startswith(q) or f.get("file_name","").lower().startswith(q):
            score = 80
        elif q in hay:
            score = 60
        else:
            # subsequence
            qi = 0
            for ch in hay:
                if qi < len(q) and ch == q[qi]:
                    qi += 1
            if qi == len(q):
                score = 30
        if score >= 0:
            scored.append((score, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored[:limit]]
