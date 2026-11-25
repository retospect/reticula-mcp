"""Which connection a tool call came through.

A connection is one way an assistant reaches the server for an account: a
connector sign-in (an OAuth grant; its tokens rotate, the grant stays) or a
token minted on the command line. ``reticula-mcp connections list`` shows
both. Its key here is ``grant:<grant id>`` or ``token:<token row id>``.

The server sets it for the length of one tool call (``server.run``, from the
access token's claims); a handler asks :func:`current`. Outside a tool call
(tests, the command line) there is none.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from reticula_mcp.db.pool import Conn

#: The claim of an access token that carries the connection.
CLAIM = "connection"

_CURRENT: ContextVar[str | None] = ContextVar("reticula_connection", default=None)


def key(*, grant_id: str | None, token_id: int | None) -> str | None:
    """The key of a connection, from the token row it was found by."""
    if grant_id:
        return f"grant:{grant_id}"
    if token_id is not None:
        return f"token:{token_id}"
    return None


def current() -> str | None:
    """The connection of the tool call being handled, or None."""
    return _CURRENT.get()


@contextmanager
def during(claims: dict[str, Any] | None) -> Iterator[None]:
    """Set the connection named in an access token's ``claims`` for a call."""
    value = (claims or {}).get(CLAIM)
    reset = _CURRENT.set(value if isinstance(value, str) and value else None)
    try:
        yield
    finally:
        _CURRENT.reset(reset)


def first_onboarding(conn: Conn, account_id: int) -> bool:
    """True once per connection: the first time ``start`` asks, which records
    that it was shown (migration 0351). False with no connection."""
    found = current()
    if found is None:
        return False
    row = conn.execute(
        "INSERT INTO onboarding_shown (connection, account_id) VALUES (%s, %s)"
        " ON CONFLICT (connection) DO NOTHING RETURNING connection",
        (found, account_id),
    ).fetchone()
    return row is not None
