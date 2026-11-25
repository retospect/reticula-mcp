"""Write hook the graph lane calls; keeps the embedding index current."""

from __future__ import annotations

import logging

from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn
from reticula_mcp.search import index
from reticula_mcp.search.embedder import get_embedder

log = logging.getLogger("reticula_mcp.search")


def on_node_changed(conn: Conn, node_id: int) -> None:
    """Called by the graph lane inside the writing transaction after a node row
    is created or its head changes (including tombstoning).

    Cheap and never fatal: a tombstoned node loses its vectors; with the cheap
    deterministic embedder the vector is recomputed inline; with a real model
    nothing is done here, the stale ``version_hash`` makes the next search's
    catch-up pass (or ``reindex``) pick the node up. Does not commit.
    """
    try:
        with conn.transaction():  # savepoint: a failure here must not poison the write
            row = conn.execute(
                "SELECT tombstoned_at FROM node WHERE id = %s", (node_id,)
            ).fetchone()
            if row is None:
                return
            if row["tombstoned_at"] is not None:
                index.forget_node(conn, node_id)
                return
            embedder = get_embedder(Settings.from_env())
            if getattr(embedder, "cheap", False):
                index.embed_nodes(conn, embedder, [node_id])
    except Exception:
        log.warning("embedding upkeep failed for node %s", node_id, exc_info=True)
