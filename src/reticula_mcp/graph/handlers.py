"""Tool handlers: put, edit, link, delete, share. OWNED BY the graph lane.

See ``content/handlers.py`` for the handler contract. Signatures here mirror
the tool signatures in ``server.py`` and are frozen.
"""

from __future__ import annotations

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import not_implemented


def put(
    conn: Conn,
    viewer: Account,
    *,
    text: str,
    kind: str = "note",
    hub: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    private: bool = False,
    supersedes: list[str] | None = None,
    ext_id: str | None = None,
    anchor: str | None = None,
    preset: str | None = None,
    model: str | None = None,
) -> str:
    raise not_implemented("graph", "put")


def edit(
    conn: Conn,
    viewer: Account,
    *,
    ref: str,
    text: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    private: bool | None = None,
    preset: str | None = None,
) -> str:
    raise not_implemented("graph", "edit")


def link(
    conn: Conn,
    viewer: Account,
    *,
    ref: str,
    to: str,
    rel: str = "part-of",
    remove: bool = False,
) -> str:
    raise not_implemented("graph", "link")


def delete(conn: Conn, viewer: Account, *, ref: str) -> str:
    raise not_implemented("graph", "delete")


def share(
    conn: Conn,
    viewer: Account,
    *,
    hub: str,
    person: str | None = None,
    mode: str = "add",
) -> str:
    raise not_implemented("graph", "share")
