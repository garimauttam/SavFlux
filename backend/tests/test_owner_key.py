"""Owner credential generation and storage invariants."""

import os

from app.services import owner_key


def test_generated_owner_key_has_256_bit_url_safe_secret():
    first = owner_key.generate_owner_key()
    second = owner_key.generate_owner_key()
    assert len(first) >= 40
    assert first != second
    assert all(character.isalnum() or character in "-_" for character in first)


def test_owner_key_is_created_once_and_file_is_private(tmp_path, monkeypatch):
    key_file = tmp_path / "state" / "owner_key"
    monkeypatch.setattr(owner_key, "owner_key_path", lambda: key_file)

    first, created_first_time = owner_key.load_or_create_owner_key()
    second, created_second_time = owner_key.load_or_create_owner_key()

    assert created_first_time is True
    assert created_second_time is False
    assert first == second == key_file.read_text()
    assert os.stat(key_file).st_mode & 0o777 == 0o600


def test_failed_persist_is_reported_as_unstored_key(tmp_path, monkeypatch):
    # Use a directory as the would-be file so the write fails deterministically.
    path = tmp_path / "owner_key"
    path.mkdir()
    monkeypatch.setattr(owner_key, "owner_key_path", lambda: path)

    key, created = owner_key.load_or_create_owner_key()
    assert created is True
    assert len(key) >= 40
    assert owner_key.read_stored_key() == ""
