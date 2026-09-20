"""
test_share.py — Unit tests for share links (service level, no DB/LLM).
"""

import pytest


@pytest.fixture()
def _isolated_storage(isolated_data_dir):
    """All share-link state redirected to a tmp dir (see conftest)."""
    return isolated_data_dir


def test_share_crud(_isolated_storage):
    from app.services.share_service import (
        create_share, get_share, list_shares, delete_share,
    )
    s = create_share("What does auth.py do?", "It verifies JWTs.",
                     sources=[{"file_name": "auth.py"}], repo_url="https://github.com/x/y",
                     ledger={"sources": [{"file_name": "auth.py"}]},
                     chat_history=[{"role": "user", "content": "hi"}])
    assert len(s["id"]) == 10
    assert s["url"] == f"/s/{s['id']}"
    assert s["ledger"] == {"sources": [{"file_name": "auth.py"}]}
    assert s["chat_history"] == [{"role": "user", "content": "hi"}]

    assert get_share(s["id"])["question"].startswith("What does")
    assert get_share("missing") is None
    assert len(list_shares()) == 1
    assert len(list_shares(repo_url="https://github.com/other")) == 0

    assert delete_share(s["id"]) is True
    assert delete_share(s["id"]) is False


def test_share_validation(_isolated_storage):
    from app.services.share_service import create_share
    with pytest.raises(ValueError):
        create_share("   ")
