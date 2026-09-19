"""
test_blast_radius.py — Unit tests for get_blast_radius (pure graph logic).
"""

from app.services.dep_graph import get_blast_radius


def _graph():
    # app.py imports auth.py and db.py; auth.py imports crypto.py
    nodes = [
        {"id": "/repo/app.py", "label": "app.py", "language": "py", "val": 3},
        {"id": "/repo/auth.py", "label": "auth.py", "language": "py", "val": 2},
        {"id": "/repo/db.py", "label": "db.py", "language": "py", "val": 2},
        {"id": "/repo/crypto.py", "label": "crypto.py", "language": "py", "val": 1},
        {"id": "/repo/unused.py", "label": "unused.py", "language": "py", "val": 1},
    ]
    edges = [
        {"source": "/repo/app.py", "target": "/repo/auth.py"},
        {"source": "/repo/app.py", "target": "/repo/db.py"},
        {"source": "/repo/auth.py", "target": "/repo/crypto.py"},
    ]
    return {"nodes": nodes, "edges": edges}


def test_blast_matches_basename_and_walks_transitive():
    impact = get_blast_radius(_graph(), "crypto.py")
    assert impact["matched_id"] == "/repo/crypto.py"
    assert impact["direct_dependents"] == ["/repo/auth.py"]
    # transitive: auth.py <- app.py
    assert set(impact["impacted_files"]) == {"/repo/auth.py", "/repo/app.py"}
    assert impact["risk_level"] in ("low", "medium", "high")


def test_blast_accepts_full_id_and_suffix():
    assert get_blast_radius(_graph(), "/repo/db.py")["matched_id"] == "/repo/db.py"
    assert get_blast_radius(_graph(), "repo/auth.py")["matched_id"] == "/repo/auth.py"


def test_blast_no_dependents_is_low_risk():
    impact = get_blast_radius(_graph(), "unused.py")
    assert impact["impacted_files"] == []
    assert impact["risk_level"] == "low"


def test_blast_unknown_file():
    impact = get_blast_radius(_graph(), "nope.py")
    assert impact["matched_id"] is None
    assert impact["impacted_files"] == []


def test_blast_max_depth_limits_hops():
    impact = get_blast_radius(_graph(), "crypto.py", max_depth=1)
    assert set(impact["impacted_files"]) == {"/repo/auth.py"}
