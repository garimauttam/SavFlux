"""
test_slash.py — P2 Slash Commands ($0, local templates) tests.

Covers:
  - slash_service list/expand/search
  - API GET /slash/commands and POST /slash/expand
"""

import pytest
from unittest.mock import patch


def test_list_commands():
    from app.services.slash_service import list_commands
    cmds = list_commands()
    assert len(cmds) >= 10
    assert any(c["command"] == "/explain" for c in cmds)
    assert any(c["command"] == "/help" for c in cmds)


def test_expand_command():
    from app.services.slash_service import expand_command
    # Basic
    r = expand_command("/explain", "def foo(): pass")
    assert "Explain" in r["prompt"]
    assert "def foo" in r["prompt"]
    assert r["command"] == "/explain"
    # Without slash prefix
    r2 = expand_command("review", "src/a.py")
    assert r2["command"] == "/review"
    # Help
    r3 = expand_command("/help", "")
    assert "/explain" in r3["prompt"]
    # Empty args
    r4 = expand_command("/explain", "")
    assert "Explain" in r4["prompt"]
    # Unknown
    try:
        expand_command("/unknown", "")
        assert False, "should raise"
    except ValueError as e:
        assert "Unknown command" in str(e)
    # Empty command
    try:
        expand_command("", "")
        assert False
    except ValueError:
        pass


def test_search_commands():
    from app.services.slash_service import search_commands
    results = search_commands("exp", limit=5)
    assert any(c["command"] == "/explain" for c in results)
    results2 = search_commands("", limit=3)
    assert len(results2) == 3
    results3 = search_commands("nonexistentxyz", limit=5)
    assert len(results3) == 0


def test_slash_api(client):
    # List all
    resp = client.get("/api/v1/slash/commands?limit=20")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] >= 10
    # Search
    resp2 = client.get("/api/v1/slash/commands?q=explain&limit=5")
    assert resp2.status_code == 200
    assert any("explain" in c["command"] for c in resp2.json()["commands"])
    # Expand success
    resp3 = client.post("/api/v1/slash/expand", json={"command": "/explain", "args": "def foo(): pass"})
    assert resp3.status_code == 200
    assert "prompt" in resp3.json()
    assert "def foo" in resp3.json()["prompt"]
    # Expand without slash
    resp4 = client.post("/api/v1/slash/expand", json={"command": "review", "args": "src/a.py"})
    assert resp4.status_code == 200
    # Missing command
    resp5 = client.post("/api/v1/slash/expand", json={"args": "hello"})
    assert resp5.status_code == 400
    # Unknown command
    resp6 = client.post("/api/v1/slash/expand", json={"command": "/unknown", "args": ""})
    assert resp6.status_code == 400
    # Alt keys
    resp7 = client.post("/api/v1/slash/expand", json={"cmd": "/test", "text": "src/utils.py"})
    assert resp7.status_code == 200
