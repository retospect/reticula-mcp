"""The public page /hubs, as the other replies name it.

'public' is a grantee in hub_grant but never a person: lists and counts of the
people who read a hub leave it out, and the replies that show a hub's full
access list say "the public page" when the hub is on it. Whether it is, and
what it shows, is ``public_nodes()`` (migration 0354) alone.
"""

from __future__ import annotations

from reticula_mcp.db.pool import Conn

#: As the replies name it.
PAGE = "the public page"
#: The grantee of the grant (graph.materialize.PUBLIC).
PUBLIC_GRANTEE = "public"


def on_page(conn: Conn, hub_id: int) -> bool:
    """Whether the hub is on the public page (itself public, or an owner's
    sub-hub of a public hub)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM public_nodes() p"
        " WHERE p.node_id = %s AND NOT p.private) AS on",
        (hub_id,),
    ).fetchone()
    return bool(row and row["on"])


def opened(conn: Conn, hub_id: int) -> tuple[list[tuple[int, str | None]], int, int]:
    """What a public hub puts on the page below it: its sub-hubs (id, title),
    how many notes (counted once each), and how many private sub-hubs show
    as "private hub"."""
    rows = conn.execute(
        "SELECT p.node_id, p.parent_hub_id, p.private, n.kind, n.title"
        " FROM public_nodes() p JOIN node n ON n.id = p.node_id",
    ).fetchall()
    private: set[int] = set()
    children: dict[int, list[dict]] = {}
    for r in rows:
        if r["parent_hub_id"] is not None:
            children.setdefault(r["parent_hub_id"], []).append(r)
    subs: list[tuple[int, str | None]] = []
    notes: set[int] = set()
    seen = {hub_id}
    todo = [hub_id]
    while todo:
        at = todo.pop(0)
        for r in children.get(at, []):
            if r["private"]:
                private.add(r["node_id"])
            elif r["kind"] == "hub":
                if r["node_id"] not in seen:
                    seen.add(r["node_id"])
                    subs.append((r["node_id"], r["title"]))
                    todo.append(r["node_id"])
            else:
                notes.add(r["node_id"])
    return subs, len(notes), len(private)
