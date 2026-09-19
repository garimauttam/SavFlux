"""
architecture.py — Auto architecture diagram from the RAG index ($0).

GET /architecture/diagram?repo_url=&max_nodes=40 returns:
  {mermaid, nodes, edges, layers, stats}

The diagram is derived deterministically from the dependency graph
(hub-first node selection, directory-based layering) — no LLM involved,
so it is free, instant, and stable across renders.
"""

import asyncio
import re
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key

router = APIRouter(prefix="/architecture", tags=["architecture"])


def _safe_id(text: str, prefix: str = "n") -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", text or "")
    if not slug or slug[0].isdigit():
        slug = f"{prefix}_{slug}"
    return slug[:60] or f"{prefix}_x"


def _top_dir(node_id: str, label: str) -> str:
    """Layer key: top-level directory of the file, or '(root)'."""
    path = node_id or ""
    if "::" in path:
        path = path.split("::", 1)[1]
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return parts[0]
    return "(root)"


def build_diagram(graph: dict, max_nodes: int = 40) -> dict:
    """Select hub files, layer by directory, emit Mermaid + structured data."""
    nodes = graph.get("nodes", []) or []
    edges = graph.get("edges", []) or []
    max_nodes = max(5, min(int(max_nodes or 40), 120))

    if not nodes:
        return {
            "mermaid": "graph TD\n    empty[\"No indexed files — ingest a repo first\"]\n",
            "nodes": [], "edges": [], "layers": [],
            "stats": {"files": 0, "shown": 0, "dependencies": 0},
        }

    # Degree = in + out; hubs first, then alphabetical for stability
    degree: Counter = Counter()
    for e in edges:
        degree[e.get("source", "")] += 1
        degree[e.get("target", "")] += 1
    ranked = sorted(nodes, key=lambda n: (-degree.get(n.get("id", ""), 0),
                                         n.get("label", "")))
    kept = ranked[:max_nodes]
    keep_ids = {n.get("id") for n in kept}
    kept_edges = [e for e in edges
                  if e.get("source") in keep_ids and e.get("target") in keep_ids]

    # Layers = top-level directories, biggest first (cap 8, rest → "(other)")
    by_layer: dict[str, list[dict]] = {}
    for n in kept:
        layer = _top_dir(n.get("id", ""), n.get("label", ""))
        by_layer.setdefault(layer, []).append(n)
    ordered = sorted(by_layer.items(), key=lambda kv: -len(kv[1]))
    layers: list[dict] = []
    other: list[dict] = []
    for name, members in ordered:
        if len(layers) < 8:
            layers.append({"name": name,
                           "files": [m.get("label", "") for m in members]})
        else:
            other.extend(members)
    if other:
        layers.append({"name": "(other)",
                       "files": [m.get("label", "") for m in other]})

    node_ids = {_safe_id(n.get("label", "") + "_" + str(i))
                for i, n in enumerate(kept)}
    id_of = {n.get("id"): nid for n, nid in zip(kept, sorted(node_ids))}

    # Mermaid: one subgraph per layer, edges between node ids
    lines = ["graph TD"]
    layer_of = {}
    for layer in layers:
        lname = layer["name"]
        for fname in layer["files"]:
            layer_of[fname] = lname
    for idx, layer in enumerate(layers):
        lines.append(f'    subgraph L{idx}["{layer["name"]}"]')
        for fname in layer["files"]:
            node = next((n for n in kept if n.get("label") == fname), None)
            nid = id_of.get(node.get("id") if node else "", f"u{idx}")
            label = fname.replace('"', "'")
            lines.append(f'        {nid}["{label}"]')
        lines.append("    end")
    for e in kept_edges[:200]:
        a, b = id_of.get(e.get("source")), id_of.get(e.get("target"))
        if a and b and a != b:
            lines.append(f"    {a} --> {b}")

    return {
        "mermaid": "\n".join(lines) + "\n",
        "nodes": [{"id": n.get("id"), "label": n.get("label"),
                   "language": n.get("language", ""),
                   "degree": degree.get(n.get("id", ""), 0),
                   "layer": layer_of.get(n.get("label", ""), "(root)")}
                  for n in kept],
        "edges": [{"source": e.get("source"), "target": e.get("target")}
                  for e in kept_edges],
        "layers": layers,
        "stats": {"files": len(nodes), "shown": len(kept),
                  "dependencies": len(kept_edges)},
    }


@router.get("/diagram")
async def get_diagram(
    repo_url: str | None = None,
    max_nodes: int = Query(40, ge=5, le=120),
    _: None = Depends(require_api_key),
):
    try:
        from app.services.dep_graph import build_dependency_graph
        graph = await asyncio.to_thread(build_dependency_graph, repo_url)
        return await asyncio.to_thread(build_diagram, graph, max_nodes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
