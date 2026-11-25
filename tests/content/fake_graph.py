"""An in-test stand-in for ``graph/api.py``.

It has the same four operations and signatures (AGENTS.md seam 10) and writes
through the frozen ``records.sign_and_append`` plus direct SQL on the frozen
tables. It is NOT the graph lane's code; the content lane's tests use it so
they do not depend on that lane.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

from reticula_mcp import format as fmt
from reticula_mcp import records
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn

_NODE_COLUMNS = "id, address, head_record_id, head_version"


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[str] = []

    # --- the four operations -------------------------------------------------

    def create_atom(
        self,
        conn: Conn,
        author: Account,
        *,
        kind: str,
        text: str,
        title: str | None = None,
        tags: Sequence[str] | None = None,
        hub_node_id: int | None = None,
        private: bool = False,
        supersedes: Sequence[str] = (),
        ext_id: str | None = None,
        anchor: str | None = None,
        model: str | None = None,
        payload_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append("create_atom")
        payload: dict[str, Any] = {"kind": kind, "text": text}
        if title:
            payload["title"] = title
        if tags:
            payload["tags"] = list(tags)
        if model:
            payload["origin"] = {"by": "agent", "model": model}
        payload.update(payload_extra or {})
        hub = self._node(conn, hub_node_id) if hub_node_id else None
        audience = "private" if private else "hub"
        address = fmt.new_address(author.person_id)
        rec = records.sign_and_append(
            conn,
            author,
            type="atom",
            address=address,
            payload=payload,
            hub_id=fmt.hub_id_from_address(hub["address"]) if hub else None,
            audience=audience,
        )
        row = conn.execute(
            "INSERT INTO node (address, author, author_account_id, kind,"
            " head_record_id, head_version, title, text, tags, audience, payload)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
            (
                address,
                author.person_id,
                author.id,
                kind,
                rec.id,
                rec.version_hash,
                title,
                text,
                list(tags or []),
                audience,
                fmt.canonical_text(payload),
            ),
        ).fetchone()
        assert row is not None
        if hub:
            self.add_membership(conn, author, row["id"], hub["id"])
        return row

    def update_atom(
        self,
        conn: Conn,
        author: Account,
        node_id: int,
        *,
        text: str | None = None,
        title: str | None = None,
        tags: Sequence[str] | None = None,
        private: bool | None = None,
        payload_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append("update_atom")
        node = conn.execute("SELECT * FROM node WHERE id = %s", (node_id,)).fetchone()
        assert node is not None
        head = records.head(conn, node["address"])
        assert head is not None
        payload = dict(head.payload)
        if text is not None:
            payload["text"] = text
        if title is not None:
            payload["title"] = title
        if tags is not None:
            payload["tags"] = list(tags)
        payload.update(payload_extra or {})
        rec = records.sign_and_append(
            conn,
            author,
            type="atom",
            address=node["address"],
            payload=payload,
            parents=(head.version_hash,),
            hub_id=head.hub_id,
            audience=head.audience,
        )
        row = conn.execute(
            "UPDATE node SET head_record_id = %s, head_version = %s, title = %s,"
            " text = %s, tags = %s, payload = %s::jsonb, updated_at = now()"
            " WHERE id = %s RETURNING *",
            (
                rec.id,
                rec.version_hash,
                payload.get("title"),
                payload["text"],
                list(payload.get("tags", [])),
                fmt.canonical_text(payload),
                node_id,
            ),
        ).fetchone()
        assert row is not None
        return row

    def add_membership(
        self, conn: Conn, actor: Account, node_id: int, hub_node_id: int
    ) -> None:
        self.calls.append("add_membership")
        node = self._node(conn, node_id)
        hub = self._node(conn, hub_node_id)
        address = fmt.new_address(actor.person_id)
        rec = records.sign_and_append(
            conn,
            actor,
            type="member",
            address=address,
            payload={
                "atom": node["address"],
                "hub": hub["address"],
                "withdrawn": False,
            },
            hub_id=fmt.hub_id_from_address(hub["address"]),
        )
        conn.execute(
            "INSERT INTO membership (address, node_id, hub_node_id, author, record_id)"
            " VALUES (%s,%s,%s,%s,%s)",
            (address, node_id, hub_node_id, actor.person_id, rec.id),
        )

    def set_grant(
        self,
        conn: Conn,
        actor: Account,
        hub_node_id: int,
        grantee_person_id: str,
        mode: str,
    ) -> None:
        self.calls.append("set_grant")
        hub = self._node(conn, hub_node_id)
        address = fmt.new_address(actor.person_id)
        rec = records.sign_and_append(
            conn,
            actor,
            type="grant",
            address=address,
            payload={
                "hub": hub["address"],
                "to": grantee_person_id,
                "mode": mode,
                "wrapped_key": None,
            },
            hub_id=fmt.hub_id_from_address(hub["address"]),
        )
        grantee = conn.execute(
            "SELECT id FROM account WHERE person_id = %s", (grantee_person_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO hub_grant (hub_node_id, grantee, grantee_account_id, mode,"
            " granted_by, address, record_id) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (hub_node_id, grantee) DO UPDATE SET mode = excluded.mode,"
            " address = excluded.address, record_id = excluded.record_id,"
            " updated_at = now()",
            (
                hub_node_id,
                grantee_person_id,
                grantee["id"] if grantee else None,
                mode,
                actor.person_id,
                address,
                rec.id,
            ),
        )

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _node(conn: Conn, node_id: int) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT {_NODE_COLUMNS} FROM node WHERE id = %s", (node_id,)
        ).fetchone()
        assert row is not None
        return row


STAND_IN_READABLE_NODES = """
CREATE OR REPLACE FUNCTION readable_nodes(viewer bigint)
RETURNS TABLE (node_id bigint) LANGUAGE sql STABLE AS $$
    SELECT n.id FROM node n JOIN account a ON a.person_id = n.author
     WHERE a.id = viewer AND n.tombstoned_at IS NULL
    UNION
    SELECT n.id FROM node n
     WHERE n.tombstoned_at IS NULL AND n.audience = 'hub' AND (
        EXISTS (SELECT 1 FROM hub_grant g WHERE g.hub_node_id = n.id
                 AND g.grantee_account_id = viewer AND g.mode IN ('read', 'add'))
        OR EXISTS (SELECT 1 FROM membership m JOIN hub_grant g
                     ON g.hub_node_id = m.hub_node_id
                    WHERE m.node_id = n.id AND NOT m.withdrawn
                      AND g.grantee_account_id = viewer
                      AND g.mode IN ('read', 'add')))
$$
"""


def ensure_shared_visibility(conn: Conn, owner: Account, reader: Account) -> bool:
    """Make sure ``readable_nodes`` lets a reader with a direct grant see the hub.

    The foundation's stub body shows only a viewer's own notes. When the graph
    lane's real function is present, nothing is changed. Otherwise a stand-in
    with the documented semantics (without ancestors) replaces it for the rest
    of the test's transaction, which the fixture rolls back. Returns True when
    the stand-in was installed.
    """
    fake = FakeGraph()
    seen = None
    with conn.transaction():  # the probe leaves nothing behind
        hub = fake.create_atom(conn, owner, kind="hub", text="probe", title="probe")
        fake.set_grant(conn, owner, hub["id"], reader.person_id, "read")
        seen = conn.execute(
            "SELECT 1 FROM readable_nodes(%s) WHERE node_id = %s",
            (reader.id, hub["id"]),
        ).fetchone()
        raise psycopg.Rollback
    if seen:
        return False
    conn.execute(STAND_IN_READABLE_NODES)
    return True
