"""Shared helpers for the graph lane's tests (not a test module)."""

from __future__ import annotations

import re
from typing import Any

from reticula_mcp import permissions, records
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.graph import api, handlers
from reticula_mcp.resolve import resolve_ref

ADDRESS = re.compile(r"ret:p[a-z2-7]{26}/[0-9a-f-]{36}")


def addr(output: str) -> str:
    """The first address in a handler's text output."""
    m = ADDRESS.search(output)
    assert m, output
    return m.group(0)


def node(conn: Conn, address: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM node WHERE address = %s", (address,)).fetchone()
    assert row is not None, address
    return row


def readable(conn: Conn, viewer: Account) -> set[str]:
    """Addresses ``viewer`` may read."""
    rows = conn.execute(
        "SELECT n.address FROM node n WHERE n.id IN ("
        + permissions.readable_nodes_sql("%(viewer)s")
        + ")",
        {"viewer": viewer.id},
    ).fetchall()
    return {r["address"] for r in rows}


def current(conn: Conn, viewer: Account) -> set[str]:
    rows = conn.execute(
        "SELECT n.address FROM node n WHERE n.id IN ("
        + permissions.current_nodes_sql("%(viewer)s")
        + ")",
        {"viewer": viewer.id},
    ).fetchall()
    return {r["address"] for r in rows}


def can_read(conn: Conn, viewer: Account, address: str) -> bool:
    return address in readable(conn, viewer)


def mk_hub(
    conn: Conn,
    owner: Account,
    title: str,
    *,
    parent: str | None = None,
    preset: str = "group",
) -> str:
    return addr(
        handlers.put(
            conn,
            owner,
            text=f"hub {title}",
            kind="hub",
            title=title,
            hub=parent,
            preset=preset,
        )
    )


def mk_note(
    conn: Conn,
    author: Account,
    text: str,
    *,
    hub: str | None = None,
    private: bool = False,
    **kw: Any,
) -> str:
    return addr(handlers.put(conn, author, text=text, hub=hub, private=private, **kw))


def share(
    conn: Conn, owner: Account, hub: str, person: Account, mode: str = "add"
) -> str:
    return handlers.share(conn, owner, hub=hub, person=f"@{person.handle}", mode=mode)


def sql_node_id(conn: Conn, address: str) -> int:
    return int(node(conn, address)["id"])


def insert_membership(
    conn: Conn,
    atom: str,
    hub: str,
    author: Account,
    *,
    audience: str = "hub",
    withdrawn: bool = False,
) -> None:
    """Forge a membership ROW directly (to attack readable_nodes)."""
    rec = records.head(conn, atom)
    assert rec is not None
    conn.execute(
        "INSERT INTO membership (address, node_id, hub_node_id, author, audience,"
        " withdrawn, record_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            f"forged:{atom}:{hub}:{audience}:{withdrawn}",
            sql_node_id(conn, atom),
            sql_node_id(conn, hub),
            author.person_id,
            audience,
            withdrawn,
            rec.id,
        ),
    )


def resolves(conn: Conn, viewer: Account, ref: str) -> bool:
    from reticula_mcp.errors import ReticulaError

    try:
        resolve_ref(conn, viewer, ref)
    except ReticulaError:
        return False
    return True


__all__ = [
    "addr",
    "api",
    "can_read",
    "current",
    "insert_membership",
    "mk_hub",
    "mk_note",
    "node",
    "readable",
    "resolves",
    "share",
    "sql_node_id",
]
