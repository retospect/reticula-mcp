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
