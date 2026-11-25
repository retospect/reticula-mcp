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


def shown_access(conn: Conn, person_id: str, hub_node_id: int) -> str | None:
    """The access to show a person and to gate the access list on.

    ``hub_access`` inherits ``add`` from a parent hub also into a personal hub,
    where only the owner adds. Such a person reads the hub and nothing more, so
    they are shown ``read`` and get the reader's view of who has access.
    """
    mode = hub_access(conn, person_id, hub_node_id)
    if mode == "add" and not can_add_person(conn, person_id, hub_node_id):
        return "read"
    return mode


def has_hub_above(conn: Conn, hub_node_id: int) -> bool:
    """True when the hub is filed under another hub, so that everyone with
    access to that hub reads this one too (``hub_ancestors``)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM hub_ancestors(%s)) AS above", (hub_node_id,)
    ).fetchone()
    assert row is not None
    return bool(row["above"])


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


def is_hub_admin(conn: Conn, person_id: str, hub_node_id: int) -> bool:
    """``person_id`` authored the hub or a hub above it."""
    row = conn.execute(
        "SELECT is_hub_admin(%s, %s) AS ok", (person_id, hub_node_id)
    ).fetchone()
    return bool(row and row["ok"])


def removed_by_admin(conn: Conn, node_id: int, hub_node_id: int) -> bool:
    """An admin of the hub removed this atom from it, and no admin has filed it
    there again since."""
    row = conn.execute(
        "SELECT 1 FROM membership WHERE node_id = %s AND hub_node_id = %s"
        " AND moderated AND withdrawn LIMIT 1",
        (node_id, hub_node_id),
    ).fetchone()
    return row is not None


def check_ancestors(conn: Conn, *, granter: str, hub: Row, grantee: str) -> None:
    """Invitations below a hub obey the hubs above it: an ancestor that restricts
    invitations to its admin, or that removed the grantee, cannot be bypassed by
    inviting through a sub-hub. The ancestor is named only if the granter can
    read it."""
    ancestors = conn.execute(
        "SELECT n.id, n.address, n.author, n.payload,"
        " EXISTS (SELECT 1 FROM account a, readable_nodes(a.id) r"
        "         WHERE a.person_id = %s AND r.node_id = n.id) AS readable,"
        " EXISTS (SELECT 1 FROM hub_grant g WHERE g.hub_node_id = n.id"
        "         AND g.grantee = %s AND g.mode = 'none'"
        "         AND g.granted_by = n.author) AS removed"
        " FROM hub_ancestors(%s) a JOIN node n ON n.id = a.hub_id ORDER BY n.id",
        (granter, grantee, hub["id"]),
    ).fetchall()
    for anc in ancestors:
        if anc["author"] == granter:
            continue
        name = f" ({anc['address']})" if anc["readable"] else ""
        if (anc["payload"] or {}).get("invite") == "admins":
            raise ReticulaError(
                FORBIDDEN,
                f"a hub above this one{name} only lets its admin invite people",
                hint="ask that hub's admin",
            )
        if anc["removed"]:
            raise ReticulaError(
                FORBIDDEN,
                f"that person was removed from a hub above this one{name} by its admin"
                if anc["readable"]
                else "that person cannot be invited to this hub",
            )


def hubs_to_detach(conn: Conn, hub_node_id: int, person_id: str) -> list[Row]:
    """Statements by which hubs owned by ``person_id`` hang under ``hub_node_id``
    or any hub below it: ``{"node_id", "hub_node_id"}`` rows."""
    return conn.execute(
        "SELECT DISTINCT m.node_id, m.hub_node_id FROM membership m"
        " JOIN node c ON c.id = m.node_id"
        " WHERE NOT m.withdrawn AND c.kind = 'hub' AND c.author = %(person)s"
        " AND m.author = %(person)s AND c.tombstoned_at IS NULL"
        " AND (m.hub_node_id = %(hub)s OR m.hub_node_id IN"
        "      (SELECT hub_id FROM hub_descendants(%(hub)s)))"
        " ORDER BY m.node_id, m.hub_node_id",
        {"person": person_id, "hub": hub_node_id},
    ).fetchall()


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
        raise ReticulaError(
            BAD_INPUT,
            "a private hub cannot be shared",
            hint=f"edit(ref={hub['address'][-8:]}, private=false), then share again",
        )
    admin = hub["author"]
    if grantee == admin:
        raise ReticulaError(
            FORBIDDEN, "the hub's admin always has full access and cannot be changed"
        )
    if mode != "none" and granter != grantee:
        check_ancestors(conn, granter=granter, hub=hub, grantee=grantee)
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


def precheck_grant(
    conn: Conn, *, granter: str, hub: Row, mode: str, targets_self: bool
) -> None:
    """The part of :func:`authorize_grant` that does not depend on WHO the
    grantee is, so a caller can refuse before looking the grantee up (a viewer
    who may not grant must not learn from the answer which handles exist)."""
    if mode not in MODES:
        raise ReticulaError(BAD_INPUT, f"mode must be one of {', '.join(MODES)}")
    if granter == hub["author"] or targets_self:
        return
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
            hint="ask the hub's admin or a member who can add",
        )
    if (hub["payload"] or {}).get("invite") == "admins":
        raise ReticulaError(
            FORBIDDEN,
            "this hub only lets its admin invite people",
            hint="ask the hub's admin",
        )
