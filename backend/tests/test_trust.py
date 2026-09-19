"""
test_trust.py — Unit tests for the commit-verification ledger.
"""

import pytest

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture()
def _isolated_storage(tmp_path, monkeypatch):
    import app.services.trust_service as ts
    monkeypatch.setattr(ts.settings, "chroma_persist_directory", str(tmp_path))
    return tmp_path


def test_verified_stale_unknown(_isolated_storage, monkeypatch):
    import app.services.trust_service as ts
    ts.record_index("https://github.com/x/y", SHA_A, files_indexed=12)

    monkeypatch.setattr(ts, "upstream_head", lambda url: SHA_A)
    assert ts.get_entry("https://github.com/x/y")["status"] == "verified"

    monkeypatch.setattr(ts, "upstream_head", lambda url: SHA_B)
    entry = ts.get_entry("https://github.com/x/y")
    assert entry["status"] == "stale"
    assert entry["files_indexed"] == 12

    monkeypatch.setattr(ts, "upstream_head", lambda url: None)
    assert ts.get_entry("https://github.com/x/y")["status"] == "unknown"

    assert ts.get_entry("https://github.com/other") is None


def test_record_never_raises(_isolated_storage, monkeypatch):
    import app.services.trust_service as ts
    monkeypatch.setattr(ts, "_ledger_path", lambda: (_ for _ in ()).throw(OSError("no disk")))
    ts.record_index("https://github.com/x/y", SHA_A)  # must not raise


def test_upstream_head_rejects_bad_urls(_isolated_storage):
    import app.services.trust_service as ts
    assert ts.upstream_head("file:///etc/passwd") is None
    assert ts.upstream_head("ftp://example.com/x") is None
    assert ts.upstream_head("") is None
