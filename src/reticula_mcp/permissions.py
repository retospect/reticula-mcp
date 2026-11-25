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

    STUB (foundation): only the hub's author. The graph lane implements the
    final rule: a grant with mode ``add`` on the hub or an ancestor hub, hub
    ``accept: anyone``, and revocations (mode ``none``).
    """
    row = conn.execute(
        "SELECT 1 FROM node WHERE id = %s AND author = %s AND tombstoned_at IS NULL",
        (hub_node_id, viewer.person_id),
    ).fetchone()
    return row is not None
