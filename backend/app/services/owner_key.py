"""
owner_key — the one secret that says "this instance is mine".

WHY AN OWNER KEY AND NOT USERNAMES AND PASSWORDS
------------------------------------------------
SavFlux is $0, local-first, and single-tenant: one person (or one small team)
runs an instance, indexes their repositories, and connects their own GitHub
account. A registration system would mean a database, password hashing, breach
response, reset flows, and email delivery — all before a single useful feature
ships, and all permanent liabilities to maintain. It would also put user
passwords inside a product whose stated rule is that it never handles anyone's
credentials.

The threat this has to stop is much narrower than "multi-user SaaS". It is:
*someone finds the URL of a deployed instance and reads the indexed source
code, the connected GitHub token, and the review cache.* One long random
token stops exactly that, and costs nothing to store because there is one
owner, not a table of them.

So: no account, no password, no email, no personal data, no database. One
secret, generated on first run, shown to the operator once.

WHAT THIS DELIBERATELY DOES NOT GIVE YOU
----------------------------------------
This is NOT multi-tenancy. There is one ChromaDB, one review cache, one
`model_selection.json`, and one GitHub token, and they are shared by everyone
who holds the key. Running unrelated users on one instance is a separate and
much larger project — it needs per-partition storage for the index, the cache
and the chat history, and a real session system. Do not read a working owner
key as "ready for public signups".
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from app.core.config import get_settings
from app.core.paths import data_file

_OWNER_KEY_FILE = "owner_key"

# 32 bytes → 256 bits of entropy. token_urlsafe gives 43 chars, which survives
# being copy-pasted out of a terminal and into a browser field without the
# usual base64 hazards ('+', '/', '='). This is not a password a human chose,
# so there is no dictionary to attack and no strength meter to lie about.
_KEY_BYTES = 32


def owner_key_path() -> Path:
    """Where the generated key lives. Same trust boundary as the rest of state."""
    return data_file(_OWNER_KEY_FILE)


def generate_owner_key() -> str:
    return secrets.token_urlsafe(_KEY_BYTES)


def read_stored_key() -> str:
    """The key on disk, or "" if there isn't one yet."""
    try:
        return owner_key_path().read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def write_owner_key(key: str) -> Path:
    """
    Persist the key, 0600, before it is ever shown.

    Mode matters: the file sits in the same directory as the connected GitHub
    token and the ChromaDB index, and a key that any local user can read is not
    a key. Written after the content is written and before the function
    returns, so there is no window where the secret exists only in a log buffer.
    """
    path = owner_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Apply restrictive permissions at creation time; chmod-after-write would
    # briefly expose the secret under the process umask (often 0644).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Some filesystems cannot express POSIX modes; fail to start rather
        # than claim the credential is protected when it is not.
        path.unlink(missing_ok=True)
        raise
    return path


def load_or_create_owner_key() -> tuple[str, bool]:
    """
    Return `(key, created)`.

    `created` is True on the run that generated it, and that is the only moment
    the operator can be shown the key. Afterwards it is only ever *read*.
    """
    existing = read_stored_key()
    if existing:
        return existing, False
    key = generate_owner_key()
    try:
        write_owner_key(key)
    except OSError:
        # Read-only data directory. Hand back the key anyway so the process
        # can print it; it simply will not survive a restart, and the startup
        # banner says so rather than pretending otherwise.
        return key, True
    return key, True


def effective_owner_key() -> str:
    """
    The key a request must present.

    `API_KEY` from the environment wins, for the same reason `GITHUB_TOKEN`
    does: CI and an existing deployment configure secrets in the platform, and
    the generated file is the fallback that makes the product usable with no
    shell at all.
    """
    env_key = (get_settings().api_key or "").strip()
    if env_key:
        return env_key
    return read_stored_key()


def rotate_owner_key() -> str:
    """Replace the stored key. The previous one stops working immediately."""
    key = generate_owner_key()
    write_owner_key(key)
    return key


__all__ = [
    "owner_key_path",
    "generate_owner_key",
    "read_stored_key",
    "write_owner_key",
    "load_or_create_owner_key",
    "effective_owner_key",
    "rotate_owner_key",
]
