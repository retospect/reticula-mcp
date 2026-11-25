"""Test support for the search lane.

Cheap state for read-side tests: atoms through the frozen foundation
(``make_atom``) and direct SQL on the frozen graph tables, read back through the
real ``readable_nodes`` (migration 0200). Tests of what a write does to
visibility use the graph handlers instead (``tests/test_search_access.py``).
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
