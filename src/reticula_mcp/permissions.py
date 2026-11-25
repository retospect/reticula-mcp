"""Read/add permission checks. Signatures FROZEN; bodies partly stubbed.

The rule "which notes may this account read" lives in exactly one place: the
SQL function ``readable_nodes(viewer bigint)`` (see
``db/migrations/0005_visibility.sql`` for the final semantics; the graph lane
replaces the stub body). Python code never re-implements it; it embeds
``readable_nodes_sql`` / ``current_nodes_sql`` in its own queries.

The hubs the rule reaches the notes through are a function of their own,
``readable_hubs(viewer)`` (migration 0204). A statement that only asks "may
the viewer read this hub" takes them as a list (``readable_hub_ids``,
``readable_hub_sql``), which the server asks for once per tool call that only
reads (``read_call``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn

# The readable hubs of each viewer asked about during the tool call at hand,
# by account id. None: no call is open and nothing is kept.
_KEPT: ContextVar[dict[int, list[int]] | None] = ContextVar(
    "reticula_readable_hubs", default=None
)


@contextmanager
def read_call() -> Iterator[None]:
    """One tool call that only reads. Inside it :func:`readable_hub_ids` asks
    the database once for each viewer and keeps the answer; the answer is
    dropped when the block ends.

    The server opens this for the tools that read and for nothing else. A tool
    that writes must never run inside it: a write can change who reads what,
    and the list kept from before the write would be wrong after it.
    """
    token = _KEPT.set({})
    try:
        yield
    finally:
        _KEPT.reset(token)


def readable_hub_ids(conn: Conn, viewer: Account) -> list[int]:
    """The ids of the hubs ``viewer`` may read: what the SQL function
    ``readable_hubs(viewer)`` returns (migration 0204), the same hubs that
    ``readable_nodes`` reaches the notes through.

    For a statement that only has to know "is this a hub the viewer may read":
    pass the list as the parameter ``hubs`` and test the hub with
    :func:`readable_hub_sql`. Which notes are readable is still decided by
    ``readable_nodes`` alone.

    Inside :func:`read_call` the database is asked once for a viewer and the
    list is kept until the call ends. Within one read call the hubs are
    therefore those of its first ask: a share that somebody else takes away
    while the call runs takes effect at the next call. With no call open (a
    tool that writes, a test that calls a handler, the indexer) every ask goes
    to the database.
    """
    kept = _KEPT.get()
    if kept is not None and viewer.id in kept:
        return list(kept[viewer.id])
    rows = conn.execute("SELECT hub_id FROM readable_hubs(%s)", (viewer.id,)).fetchall()
    ids = sorted(r["hub_id"] for r in rows)
    if kept is not None:
        kept[viewer.id] = ids
    return list(ids)


def readable_hub_sql(hub: str) -> str:
    """A boolean SQL expression: ``hub`` (a column holding a node id) is a hub
    the viewer may read. The statement's parameters must hold
    ``"hubs": readable_hub_ids(conn, viewer)``; ``hubs`` is the one name for
    that parameter everywhere."""
    return f"{hub} = ANY(%(hubs)s::bigint[])"


def readable_nodes_sql(viewer_param: str = "%(viewer)s") -> str:
    """``SELECT node_id FROM readable_nodes(<viewer_param>)``: embed as a
    subquery, e.g. ``WHERE n.id IN (<this>)``. ``viewer_param`` is a psycopg
    placeholder for the viewer's ACCOUNT ID (``Account.id``)."""
    return f"SELECT node_id FROM readable_nodes({viewer_param})"


def readable_among_sql(ids_sql: str, viewer_param: str = "%(viewer)s") -> str:
    """The ids that ``ids_sql`` (a query of one column of node ids) gives and
    that ``readable_nodes(<viewer_param>)`` holds: embed as ``WHERE n.id IN
    (<this>)`` where only a few ids are in question.

    Who may read is still ``readable_nodes`` alone. Written as ``= ANY
    (ARRAY(...))``, the planner takes the condition into each branch of the
    inlined function, which then reads the rows of those ids only, in place of
    every note the viewer may read. ``tests/test_resolve_ref_cost.py`` fails
    when it stops doing so."""
    return (
        f"SELECT v.node_id FROM ({readable_nodes_sql(viewer_param)}) v"
        f" WHERE v.node_id = ANY (ARRAY({ids_sql}))"
    )


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
