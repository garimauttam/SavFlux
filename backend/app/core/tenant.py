"""Request-scoped tenant identity for Supabase-authenticated users.

The UUID comes only from the verified Supabase user response. It is hashed before
being used as a directory or cache key; clients cannot choose an arbitrary path.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

_current_user_id: ContextVar[str | None] = ContextVar("savflux_current_user_id", default=None)


def current_user_id() -> str | None:
    return _current_user_id.get()


def set_current_user_id(user_id: str) -> Token:
    return _current_user_id.set(user_id)


def reset_current_user_id(token: Token) -> None:
    _current_user_id.reset(token)
