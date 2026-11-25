"""No-op hook the graph lane calls and the search lane fills in."""

from __future__ import annotations

from reticula_mcp.db.pool import Conn


def on_node_changed(conn: Conn, node_id: int) -> None:
    """Called by the graph lane inside the writing transaction after a node
    row is created or its head changes (including tombstoning). The search
    lane may (re)compute or queue the embedding here. Must not raise for
    ordinary failures of optional work, and must not commit."""
