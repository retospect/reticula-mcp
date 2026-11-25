"""Read/add permission checks. Signatures FROZEN; bodies partly stubbed.

The rule "which notes may this account read" lives in exactly one place: the
SQL function ``readable_nodes(viewer bigint)`` (see
``db/migrations/0005_visibility.sql`` for the final semantics; the graph lane
replaces the stub body). Python code never re-implements it; it embeds
``readable_nodes_sql`` / ``current_nodes_sql`` in its own queries.
"""

from __future__ import annotations

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn


def readable_nodes_sql(viewer_param: str = "%(viewer)s") -> str:
    """``SELECT node_id FROM readable_nodes(<viewer_param>)``: embed as a
    subquery, e.g. ``WHERE n.id IN (<this>)``. ``viewer_param`` is a psycopg
    placeholder for the viewer's ACCOUNT ID (``Account.id``)."""
    return f"SELECT node_id FROM readable_nodes({viewer_param})"


def current_nodes_sql(viewer_param: str = "%(viewer)s") -> str:
    """Like :func:`readable_nodes_sql`, minus atoms superseded by an atom the
    same viewer can read (the default view; history needs ``readable``)."""
    return f"SELECT node_id FROM current_nodes({viewer_param})"


def read_through_above_sql(hub: str = "%(hub)s") -> str:
    """A boolean SQL expression: somebody reads the hub ``hub`` through a hub
    above it who is neither its owner nor named in a grant on the hub itself.

    Everyone with access to a hub reads the hubs filed under it, so for such a
    hub the grants on the hub are not the whole list of its readers. A hub
    under the owner's own unshared hub does not count: nobody else reads it.
    The expression errs on the side of saying yes (a person shut out of the
    sub-hub by a later grant still counts).
    """
    return (
        f"EXISTS (SELECT 1 FROM hub_ancestors({hub}) up_"
        " JOIN node above_ ON above_.id = up_.hub_id"
        f" JOIN node self_ ON self_.id = {hub}"
        " WHERE above_.author <> self_.author"
        " OR EXISTS (SELECT 1 FROM hub_grant ga_"
        "   WHERE ga_.hub_node_id = above_.id AND ga_.mode IN ('read', 'add')"
        "   AND ga_.grantee <> self_.author"
        "   AND NOT EXISTS (SELECT 1 FROM hub_grant gd_"
        "     WHERE gd_.hub_node_id = self_.id AND gd_.grantee = ga_.grantee"
        "     AND gd_.mode IN ('read', 'add'))))"
    )


def can_read(conn: Conn, viewer: Account, node_id: int) -> bool:
    """May ``viewer`` read node ``node_id``? Delegates to ``readable_nodes``."""
    row = conn.execute(
        f"SELECT 1 FROM ({readable_nodes_sql('%s')}) r WHERE r.node_id = %s",
        (viewer.id, node_id),
    ).fetchone()
    return row is not None


def can_add(conn: Conn, viewer: Account, hub_node_id: int) -> bool:
    """May ``viewer`` file atoms under hub ``hub_node_id``?

    True for the hub's author (its admin), for anyone holding a grant with mode
    ``add`` on the hub or on an ancestor hub, and for anyone when the hub's
    payload says ``"accept": "anyone"`` (which does not make them readers). A
    grant with mode ``none`` is no grant. Implemented by the SQL function
    ``can_add_person`` (migration 0200), which shares its ancestor walk with
    ``readable_nodes``.
    """
    row = conn.execute(
        "SELECT can_add_person(%s, %s) AS ok", (viewer.person_id, hub_node_id)
    ).fetchone()
    return bool(row and row["ok"])
