"""Shared row fetch: node rows decorated with author and (readable) hub.

Visibility discipline: the hub shown for a row is only ever a hub the viewer can
read itself; a note may sit in other hubs the viewer cannot see, and those never
surface here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.permissions import current_nodes_sql, readable_nodes_sql

Row = dict[str, Any]


def fetch_rows(
    conn: Conn,
    viewer: Account,
    ids: Sequence[int],
    *,
    text_chars: int | None = 400,
) -> dict[int, Row]:
    """Rows for node ids ALREADY known readable by ``viewer`` (callers pass ids
    that came out of a ``readable_nodes`` / ``current_nodes`` restricted query).
    ``text_chars`` cuts the text (None keeps all of it)."""
    if not ids:
        return {}
    text_col = "n.text" if text_chars is None else f"left(n.text, {int(text_chars)})"
    rows = conn.execute(
        f"""
        SELECT n.id, n.address, n.kind, n.title, {text_col} AS text, n.author,
               n.audience, n.created_at, n.updated_at, n.ext_key, n.tombstoned_at,
               n.head_version, n.tags,
               a.handle AS author_handle, a.status AS author_status,
               hub.id AS hub_id, hub.title AS hub_title, hub.text AS hub_text,
               hub.address AS hub_address
        FROM node n
        LEFT JOIN account a ON a.person_id = n.author
        LEFT JOIN LATERAL (
            SELECT h.id, h.title, h.text, h.address
            FROM membership m JOIN node h ON h.id = m.hub_node_id
            WHERE m.node_id = n.id AND NOT m.withdrawn
              AND (m.audience = 'hub' OR m.author = %(person)s)
              AND h.kind = 'hub'
              AND h.id IN ({readable_nodes_sql("%(viewer)s")})
            ORDER BY m.id LIMIT 1
        ) hub ON true
        WHERE n.id = ANY(%(ids)s)
        """,
        {"viewer": viewer.id, "person": viewer.person_id, "ids": list(ids)},
    ).fetchall()
    return {r["id"]: r for r in rows}


def visible_subset(
    conn: Conn, viewer: Account, ids: Sequence[int], *, current: bool = False
) -> set[int]:
    """The ids among ``ids`` that ``viewer`` may read (``current``: and that no
    readable atom supersedes)."""
    if not ids:
        return set()
    fn = current_nodes_sql if current else readable_nodes_sql
    rows = conn.execute(
        f"SELECT node_id FROM ({fn('%(viewer)s')}) v WHERE node_id = ANY(%(ids)s)",
        {"viewer": viewer.id, "ids": list(ids)},
    ).fetchall()
    return {r["node_id"] for r in rows}
