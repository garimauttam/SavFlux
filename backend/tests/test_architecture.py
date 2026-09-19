"""
test_architecture.py — Unit tests for deterministic diagram building.
"""

from app.api.architecture import build_diagram


def _graph():
    nodes = [
        {"id": "r::api/app.py", "label": "app.py", "language": "py", "val": 3},
        {"id": "r::api/auth.py", "label": "auth.py", "language": "py", "val": 2},
        {"id": "r::db/models.py", "label": "models.py", "language": "py", "val": 2},
    ]
    edges = [
        {"source": "r::api/app.py", "target": "r::api/auth.py"},
        {"source": "r::api/auth.py", "target": "r::db/models.py"},
    ]
    return {"nodes": nodes, "edges": edges}


def test_diagram_selects_hubs_and_layers():
    d = build_diagram(_graph(), max_nodes=10)
    assert d["stats"] == {"files": 3, "shown": 3, "dependencies": 2}
    # auth.py has degree 2 → ranked first
    assert d["nodes"][0]["label"] == "auth.py"
    assert {layer["name"] for layer in d["layers"]} == {"api", "db"}


def test_mermaid_is_valid_graph_td():
    d = build_diagram(_graph(), max_nodes=10)
    mmd = d["mermaid"]
    assert mmd.startswith("graph TD")
    assert "subgraph" in mmd
    assert "-->" in mmd
    assert "auth.py" in mmd


def test_empty_graph_placeholder():
    d = build_diagram({"nodes": [], "edges": []})
    assert d["mermaid"].startswith("graph TD")
    assert d["nodes"] == []
