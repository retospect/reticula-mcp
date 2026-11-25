"""Who may do what on a hub. The rules live in SQL (``hub_access``,
``can_add_person``, ``readable_nodes``); this module adds the Python-side
authorisation of grants and the hub-hierarchy cycle check."""

from __future__ import annotations

from typing import Any

from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, CONFLICT, FORBIDDEN, ReticulaError

Row = dict[str, Any]
MODES = ("add", "read", "none")

# One global advisory lock serialises every change to the hub hierarchy, so two
# concurrent filings cannot close a cycle together.
_HIERARCHY_LOCK = 0x72657469  # "reti"


def hub_access(conn: Conn, person_id: str, hub_node_id: int) -> str | None:
    """``'add'``, ``'read'`` or None: the strongest access ``person_id`` holds."""
    row = conn.execute(
        "SELECT hub_access(%s, %s) AS mode", (person_id, hub_node_id)
    ).fetchone()
    assert row is not None
    return row["mode"]


def can_add_person(conn: Conn, person_id: str, hub_node_id: int) -> bool:
    row = conn.execute(
        "SELECT can_add_person(%s, %s) AS ok", (person_id, hub_node_id)
    ).fetchone()
    assert row is not None
    return bool(row["ok"])


def lock_hierarchy(conn: Conn) -> None:
    """Take the transaction-scoped hub-hierarchy lock."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_HIERARCHY_LOCK,))


def closes_cycle(conn: Conn, child_hub_id: int, parent_hub_id: int) -> bool:
    """Would filing hub ``child`` under hub ``parent`` close a cycle?

    True when ``parent`` is ``child`` or already sits (transitively) beneath
    it. Counts every live filing, whoever made it. Call under
    :func:`lock_hierarchy`.
    """
    if child_hub_id == parent_hub_id:
        return True
    row = conn.execute(
        "WITH RECURSIVE up(id) AS ("
        " SELECT %s::bigint"
        " UNION SELECT m.hub_node_id FROM up JOIN membership m"
        "  ON m.node_id = up.id WHERE NOT m.withdrawn)"
        " SELECT 1 AS hit FROM up WHERE id = %s",
        (parent_hub_id, child_hub_id),
    ).fetchone()
    return row is not None


def authorize_grant(
    conn: Conn,
    *,
    granter: str,
    hub: Row,
    grantee: str,
    mode: str,
) -> None:
    """Raise unless ``granter`` may set ``grantee``'s mode on ``hub`` to ``mode``.

    * the hub's author is its admin and may set anyone but themself;
    * anyone may leave (``none`` for themself);
    * otherwise the granter must hold ``add`` on the hub (directly or through
      a hub above), the hub must not restrict invites to its admin
      (``"invite": "admins"``), and the grantee must be new (no grant yet) or
      have left on their own: only the admin changes or revokes someone else.
    """
    if mode not in MODES:
        raise ReticulaError(BAD_INPUT, f"mode must be one of {', '.join(MODES)}")
    if hub["kind"] != "hub" or hub["tombstoned_at"] is not None:
        raise ReticulaError(BAD_INPUT, "that is not a live hub")
    if hub["audience"] != "hub":
        raise ReticulaError(BAD_INPUT, "a private hub cannot be shared")
    admin = hub["author"]
    if grantee == admin:
        raise ReticulaError(
            FORBIDDEN, "the hub's admin always has full access and cannot be changed"
        )
    if granter == admin:
        return
    if granter == grantee:
        if mode == "none":
            return
        raise ReticulaError(
            FORBIDDEN,
            "you cannot change your own access",
            hint="ask the hub's admin",
        )
    if mode == "none":
        raise ReticulaError(
            FORBIDDEN,
            "only the hub's admin can revoke someone else",
            hint="anyone can remove themself with mode=none",
        )
    access = hub_access(conn, granter, hub["id"])
    if access != "add":
        raise ReticulaError(
            FORBIDDEN,
            "you may share a hub only if you can add to it",
            hint="ask the hub's admin or a member who can add"
            if access
            else "you have no access to this hub",
        )
    if (hub["payload"] or {}).get("invite") == "admins":
        raise ReticulaError(
            FORBIDDEN,
            "this hub only lets its admin invite people",
            hint="ask the hub's admin",
        )
    existing = conn.execute(
        "SELECT mode, granted_by FROM hub_grant WHERE hub_node_id = %s"
        " AND grantee = %s",
        (hub["id"], grantee),
    ).fetchone()
    if existing is None:
        return
    if existing["mode"] == "none" and existing["granted_by"] == grantee:
        return  # they left on their own: any member may bring them back
    if existing["mode"] == mode:
        raise ReticulaError(CONFLICT, "that person already has this access")
    raise ReticulaError(
        FORBIDDEN,
        "only the hub's admin can change or restore someone else's access",
    )
