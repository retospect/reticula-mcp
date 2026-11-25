"""Test support for the search lane.

The graph lane replaces the foundation stub of ``readable_nodes`` in parallel.
These tests must hold for the FINAL semantics documented in
``0005_visibility.sql``, so :func:`install_final_readable` installs, inside the
test's own (rolled-back) transaction, a ``CREATE OR REPLACE FUNCTION
readable_nodes`` that implements exactly that documented rule. It is test
scaffolding, never a migration. State is written through the frozen foundation
modules (``make_atom``) and direct SQL on the frozen tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.search import embedder as embedder_module
from tests.helpers import make_atom

FINAL_READABLE_NODES = """
CREATE OR REPLACE FUNCTION readable_nodes(viewer bigint)
RETURNS TABLE (node_id bigint) LANGUAGE sql STABLE AS $$
    WITH RECURSIVE me AS (SELECT person_id FROM account WHERE id = viewer),
    covered(id) AS (
        SELECT g.hub_node_id FROM hub_grant g, me
        WHERE g.grantee = me.person_id AND g.mode IN ('read', 'add')
        UNION
        SELECT m.node_id FROM membership m
        JOIN covered c ON m.hub_node_id = c.id
        JOIN node h ON h.id = m.node_id AND h.kind = 'hub'
        WHERE NOT m.withdrawn AND m.audience = 'hub'
    )
    SELECT n.id FROM node n, me
    WHERE n.tombstoned_at IS NULL AND (
        n.author = me.person_id
        OR (n.audience = 'hub' AND (
            n.id IN (SELECT id FROM covered)
            OR EXISTS (
                SELECT 1 FROM membership m
                WHERE m.node_id = n.id AND NOT m.withdrawn AND m.audience = 'hub'
                  AND m.hub_node_id IN (SELECT id FROM covered))))
    )
$$
"""


def install_final_readable(conn: Conn) -> None:
    conn.execute(FINAL_READABLE_NODES)


def use_hash_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETICULA_EMBEDDER", "hash")
    monkeypatch.delenv("RETICULA_EMBED_MODEL", raising=False)
    embedder_module._build.cache_clear()


def file_under(
    conn: Conn,
    atom: dict[str, Any],
    hub: dict[str, Any],
    *,
    by: Account | None = None,
    audience: str = "hub",
    withdrawn: bool = False,
) -> None:
    """A membership row (the real graph lane signs a `member` record; the search
    lane only reads the materialised table)."""
    author = by.person_id if by else atom["author"]
    conn.execute(
        "INSERT INTO membership (address, node_id, hub_node_id, author, audience,"
        " withdrawn, record_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            f"ret:{author}/{uuid.uuid4()}",
            atom["id"],
            hub["id"],
            author,
            audience,
            withdrawn,
            atom["head_record_id"],
        ),
    )


def grant(conn: Conn, hub: dict[str, Any], grantee: Account, mode: str = "add") -> None:
    conn.execute(
        "INSERT INTO hub_grant (hub_node_id, grantee, grantee_account_id, mode,"
        " granted_by, address, record_id) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (hub_node_id, grantee) DO UPDATE SET mode = EXCLUDED.mode",
        (
            hub["id"],
            grantee.person_id,
            grantee.id,
            mode,
            hub["author"],
            f"ret:{hub['author']}/{uuid.uuid4()}",
            hub["head_record_id"],
        ),
    )


def supersede(conn: Conn, new: dict[str, Any], old: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO supersedes (new_node_id, old_node_id) VALUES (%s, %s)",
        (new["id"], old["id"]),
    )


def link(conn: Conn, src: dict[str, Any], rel: str, dst: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO link (src_node_id, rel, dst_address, dst_node_id)"
        " VALUES (%s, %s, %s, %s)",
        (src["id"], rel, dst["address"], dst["id"]),
    )


def tombstone(conn: Conn, atom: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE node SET tombstoned_at = now(), text = '', title = NULL WHERE id = %s",
        (atom["id"],),
    )


def set_created(conn: Conn, atom: dict[str, Any], when: datetime) -> None:
    conn.execute(
        "UPDATE node SET created_at = %s, updated_at = %s WHERE id = %s",
        (when, when, atom["id"]),
    )


def hub_of(
    conn: Conn, owner: Account, title: str, text: str = "", **kw: Any
) -> dict[str, Any]:
    """A hub atom. Like the graph lane, give its creator an ``add`` grant so the
    owner reads what members file in it."""
    hub = make_atom(conn, owner, text or title, kind="hub", title=title, **kw)
    grant(conn, hub, owner, "add")
    return hub
