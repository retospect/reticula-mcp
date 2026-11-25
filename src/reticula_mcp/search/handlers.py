"""Tool handlers: search, get. OWNED BY the search lane.

See ``content/handlers.py`` for the handler contract. Signatures here mirror
the tool signatures in ``server.py`` and are frozen.
"""

from __future__ import annotations

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import not_implemented


def search(
    conn: Conn,
    viewer: Account,
    *,
    query: str,
    hub: str | None = None,
    kind: str | None = None,
    limit: int = 10,
    include_superseded: bool = False,
) -> str:
    raise not_implemented("search", "search")


def get(
    conn: Conn,
    viewer: Account,
    *,
    ref: str,
    history: bool = False,
    depth: int = 1,
) -> str:
    raise not_implemented("search", "get")
