"""Hooks other parts call: the write hook of the graph lane, and the start and
stop of the server. They keep the embedding index current and never get in the
way: none of them raises, and none of them waits for a model."""

from __future__ import annotations

import logging

from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn, Database
from reticula_mcp.search import index, worker
from reticula_mcp.search.embedder import get_embedder

log = logging.getLogger("reticula_mcp.search")


def on_node_changed(conn: Conn, node_id: int) -> None:
    """Called by the graph lane inside the writing transaction after a node row
    is created or its head changes (including tombstoning).

    Cheap and never fatal. A tombstoned node loses its vectors. With the cheap
    deterministic embedder the vector is recomputed inline. With a real model
    nothing is computed here and the model is not even loaded: the stale
    ``version_hash`` marks the node, the background worker is nudged and embeds
    it after the commit, and the next search of someone who can read it catches
    it up as well. Does not commit.
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
            else:
                worker.nudge()
    except Exception:
        log.warning("embedding upkeep failed for node %s", node_id, exc_info=True)


def on_server_start(settings: Settings, db: Database) -> None:
    """Called once when the server starts, after the pool is open. Starts the
    background thread that loads the embedding model and catches the index up
    (``worker.py``). Returns at once; does nothing for the hash embedder; never
    raises."""
    try:
        worker.start(settings, db)
    except Exception:
        log.warning("embedding worker did not start", exc_info=True)


def on_server_stop() -> None:
    """Called when the server shuts down, before the pool closes."""
    try:
        worker.stop()
    except Exception:
        log.warning("embedding worker did not stop cleanly", exc_info=True)
