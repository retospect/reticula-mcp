"""Hooks other parts call: the write hook of the graph lane, and the start and
stop of the server. They keep the embedding index current and never get in the
way: none of them raises, and none of them waits for a model."""

from __future__ import annotations

import logging

from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn, Database
from reticula_mcp.search import index, worker
from reticula_mcp.search.embedder import get_embedder, is_ready

log = logging.getLogger("reticula_mcp.search")


def on_node_changed(conn: Conn, node_id: int) -> None:
    """Called by the graph lane inside the writing transaction after a node row
    is created or its head changes (including tombstoning).

    Never fatal, and never waits for a model to load. A tombstoned node loses
    its vectors. Otherwise, when the embedder can answer now (the hash
    embedder, or a model that is loaded), the vector is computed here and
    commits with the write: the note is findable by meaning as soon as the
    tool returns. When it cannot, or the embedding fails (rolled back alone,
    in a savepoint of its own), the node stays in ``embedding_backlog``,
    where a trigger of migration 0302 put it, and the background worker is
    nudged; the next search of someone who can read it embeds it too. Does
    not commit.
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
            if is_ready(embedder):
                try:
                    with conn.transaction():  # its own savepoint: only the vector
                        index.embed_nodes(conn, embedder, [node_id])
                    return
                except Exception:
                    log.warning(
                        "embedding at write failed for node %s; left to the worker",
                        node_id,
                        exc_info=True,
                    )
            # the row in embedding_backlog was made by the trigger of 0302
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
