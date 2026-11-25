"""Tool handlers: start, profile. OWNED BY the content lane.

Handler contract (all lanes): a plain sync function
``handler(conn, viewer, **params) -> str``. ``conn`` is a psycopg connection
inside a transaction owned by the caller (do not commit); ``viewer`` is the
authenticated :class:`~reticula_mcp.accounts.Account`. Parameters are exactly
the tool's parameters, keyword-only, same names and defaults as in
``server.py`` (a test compares the signatures). Return compact markdown; raise
:class:`~reticula_mcp.errors.ReticulaError` for failures.
"""

from __future__ import annotations

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import not_implemented


def start(conn: Conn, viewer: Account) -> str:
    raise not_implemented("content", "start")


def profile(conn: Conn, viewer: Account, *, handle: str | None = None) -> str:
    raise not_implemented("content", "profile")
