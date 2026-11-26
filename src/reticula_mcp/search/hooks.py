"""Hooks other parts call: the write hook of the graph lane, and the start and
stop of the server. They keep the embedding index current and never get in the
way: none of them raises, and none of them waits for a model."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn, Database
from reticula_mcp.search import index, worker
from reticula_mcp.search.embedder import get_embedder, is_ready

log = logging.getLogger("reticula_mcp.search")

# The nodes a tool call wrote, while ``after_commit`` is open: they are
# embedded once the call's transaction has committed.
_WRITTEN: ContextVar[list[int] | None] = ContextVar("reticula_written", default=None)


@contextmanager
def after_commit(db: Database, *, enabled: bool = True) -> Iterator[None]:
    """Around a tool call that writes and the transaction it commits: the
    notes it wrote are embedded when the block ends without an error, that is
    after the commit and before the reply, in a short transaction of their
    own. No row lock is held while the model runs, and the note is findable
    by meaning when the tool returns. A failure leaves them in the backlog
    for the worker; the call's answer is never changed by it."""
    if not enabled:
        yield
        return
    box: list[int] = []
    token = _WRITTEN.set(box)
    try:
        yield
    finally:
        _WRITTEN.reset(token)
    if box:
        embed_written(db, box)


def embed_written(db: Database, node_ids: list[int]) -> None:
    """Embed ``node_ids`` in a transaction of their own; never raises."""
    try:
        embedder = get_embedder(Settings.from_env())
        if not is_ready(embedder):
            worker.nudge()
            return
        with db.connection() as conn:
            index.embed_nodes(conn, embedder, sorted(set(node_ids)))
    except Exception:
        log.warning(
            "embedding after the write failed for %s; left to the worker",
            node_ids,
            exc_info=True,
        )
        worker.nudge()


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
            written = _WRITTEN.get()
            if written is not None and not getattr(embedder, "cheap", False):
                # a tool call: embedded after its commit (after_commit)
                written.append(node_id)
                return
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
