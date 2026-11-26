"""The local embedding index: upkeep, lazy catch-up, rebuild, the vector index.

Everything here touches the ``embedding`` table only. It is derived from
``node`` (stamped with the node's ``head_version``), never shared, and can be
dropped and rebuilt at any time without changing a single shared row.

Several models can have rows in the table at once (after ``RETICULA_EMBEDDER``
or ``RETICULA_EMBED_MODEL`` changed). A row belongs to exactly one model and one
dimension; every query here and in ``find.py`` names both, so vectors of
another model are never compared, never counted as "embedded" for the current
one, and never deleted except by ``reindex --drop``.

Each model gets its own partial HNSW index (:func:`ensure_vector_index`):
``embedding_hnsw_<hash of the model name>_<dim>`` over ``vec::vector(<dim>)``
``WHERE model = '<name>' AND dim = <dim>``. It is created before the model's
vectors are filled in, so creating it is instant and it grows with the writes.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
from pgvector import Vector
from psycopg import sql

from reticula_mcp.db.pool import Conn
from reticula_mcp.permissions import readable_nodes_sql
from reticula_mcp.search.embedder import Embedder

log = logging.getLogger("reticula_mcp.search")

TEXT_LIMIT = 4000  # characters embedded per node (models truncate anyway)
BATCH = 32  # nodes per embed call with a cheap embedder
BATCH_COSTLY = 4  # ... with a real model (one 4000-character text takes ~0.2 s)
CATCH_UP_CHEAP = 500  # nodes embedded per search with a cheap embedder
CATCH_UP_COSTLY = 16  # ... and with a real model
# A search with a real model stops catching up after this many seconds (checked
# between batches); the background worker does the rest.
CATCH_UP_SECONDS = 0.3
HNSW_MAX_DIM = 2000  # pgvector cannot index wider `vector` columns
INDEX_PREFIX = "embedding_hnsw_"


def node_text(title: str | None, text: str) -> str:
    """What gets embedded for a node."""
    return f"{title}\n{text}"[:TEXT_LIMIT] if title else text[:TEXT_LIMIT]


def _cheap(embedder: Embedder) -> bool:
    return bool(getattr(embedder, "cheap", False))


def _upsert(
    conn: Conn, embedder: Embedder, rows: list[dict[str, Any]], vectors: list[Any]
) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (node_id, model) DO UPDATE SET dim = EXCLUDED.dim,"
            " vec = EXCLUDED.vec, version_hash = EXCLUDED.version_hash",
            [
                (
                    r["id"],
                    embedder.model,
                    len(v),
                    Vector(v),
                    r["head_version"],
                )
                for r, v in zip(rows, vectors, strict=True)
            ],
        )


def embed_nodes(
    conn: Conn,
    embedder: Embedder,
    node_ids: list[int],
    *,
    deadline: float | None = None,
) -> int:
    """(Re)compute the vectors of the given nodes for ``embedder``'s model.
    Tombstoned or missing nodes lose their vectors (of every model) instead.
    ``deadline`` (a ``time.monotonic`` value) stops the work between batches.
    Returns how many vectors were written."""
    written = 0
    batch = BATCH if _cheap(embedder) else BATCH_COSTLY
    for start in range(0, len(node_ids), batch):
        if deadline is not None and start and time.monotonic() >= deadline:
            break
        chunk = node_ids[start : start + batch]
        # ascending ids: two transactions that embed the same nodes take the row
        # locks in the same order
        rows = conn.execute(
            "SELECT id, title, text, head_version FROM node"
            " WHERE id = ANY(%s) AND tombstoned_at IS NULL ORDER BY id",
            (chunk,),
        ).fetchall()
        if rows:
            vectors = embedder.embed([node_text(r["title"], r["text"]) for r in rows])
            _upsert(conn, embedder, rows, vectors)
            written += len(rows)
        live = {r["id"] for r in rows}
        gone = [i for i in chunk if i not in live]
        if gone:
            conn.execute("DELETE FROM embedding WHERE node_id = ANY(%s)", (gone,))
    return written


def forget_node(conn: Conn, node_id: int) -> None:
    """Drop every vector of a node (tombstoned atoms are never searchable)."""
    conn.execute("DELETE FROM embedding WHERE node_id = %s", (node_id,))


def missing_ids(
    conn: Conn, embedder: Embedder, viewer_id: int | None, limit: int
) -> list[int]:
    """Live nodes with no vector of ``embedder``'s model for their current
    version, newest first. A vector of another model does not count."""
    params: dict[str, Any] = {"model": embedder.model, "limit": limit}
    scope = ""
    if viewer_id is not None:
        params["viewer"] = viewer_id
        scope = f" AND n.id IN ({readable_nodes_sql('%(viewer)s')})"
    return [
        r["id"]
        for r in conn.execute(
            "SELECT n.id FROM node n WHERE n.tombstoned_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM embedding e WHERE e.node_id = n.id"
            "   AND e.model = %(model)s AND e.version_hash = n.head_version)"
            f"{scope} ORDER BY n.id DESC LIMIT %(limit)s",
            params,
        ).fetchall()
    ]


def catch_up(
    conn: Conn,
    embedder: Embedder,
    viewer_id: int | None = None,
    limit: int | None = None,
    *,
    seconds: float | None = None,
) -> int:
    """Embed nodes whose vector is missing or stale (older ``head_version``).

    Bounded: at most ``limit`` nodes per call (default depends on how costly
    the embedder is) and, when ``seconds`` is given, no new batch after that
    long. With ``viewer_id`` only nodes that viewer can read are considered,
    newest first, so a search always catches up its own world first. Returns
    the number embedded. The safety net for the write hook and the worker.
    """
    if limit is None:
        limit = CATCH_UP_CHEAP if _cheap(embedder) else CATCH_UP_COSTLY
    ids = missing_ids(conn, embedder, viewer_id, limit)
    if not ids:
        return 0
    deadline = None if seconds is None else time.monotonic() + seconds
    return embed_nodes(conn, embedder, ids, deadline=deadline)


def _all_node_ids(conn: Conn) -> Iterator[list[int]]:
    last = 0
    while True:
        rows = conn.execute(
            "SELECT id FROM node WHERE id > %s AND tombstoned_at IS NULL"
            " ORDER BY id LIMIT 500",
            (last,),
        ).fetchall()
        if not rows:
            return
        last = rows[-1]["id"]
        yield [r["id"] for r in rows]


def reindex(
    conn: Conn,
    embedder: Embedder,
    *,
    drop: bool = False,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    """Rebuild the index for ``embedder``'s model: every live node is embedded
    again. Vectors of other models stay as they are, unused. ``drop`` first
    empties the whole table (all models) and removes the vector indexes of
    other models. Never touches a shared table. Returns the number of vectors
    written.

    ``checkpoint`` is called after the preparation and after every 500 nodes;
    the command line passes ``conn.commit`` so that no lock is held for the
    whole run. Code inside a caller's transaction leaves it out.
    """
    if drop:
        conn.execute("DELETE FROM embedding")
        drop_other_vector_indexes(conn, embedder)
    else:  # vectors of nodes tombstoned since are dead weight
        conn.execute(
            "DELETE FROM embedding e USING node n"
            " WHERE n.id = e.node_id AND n.tombstoned_at IS NOT NULL"
        )
    ensure_vector_index(conn, embedder)
    if checkpoint is not None:
        checkpoint()
    written = 0
    for ids in _all_node_ids(conn):
        written += embed_nodes(conn, embedder, ids)
        if checkpoint is not None:
            checkpoint()
    return written


# ---------------------------------------------------------------- vector index


def vector_index_name(model: str, dim: int) -> str:
    """The name of the HNSW index of one model and dimension."""
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:12]
    return f"{INDEX_PREFIX}{digest}_{dim}"


def vector_expr(dim: int, alias: str = "e") -> str:
    """The indexed expression, spelled exactly as in the index definition (the
    planner matches it textually): the untyped ``vec`` cast to its dimension."""
    return f"({alias}.vec::vector({int(dim)}))"


def has_vector_index(conn: Conn, embedder: Embedder) -> bool:
    """Is there a usable (valid) vector index for ``embedder``'s model?"""
    row = conn.execute(
        "SELECT 1 AS x FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
        " WHERE c.relname = %s AND i.indisvalid",
        (vector_index_name(embedder.model, embedder.dim),),
    ).fetchone()
    return row is not None


def ensure_vector_index(conn: Conn, embedder: Embedder) -> str | None:
    """Create the HNSW index for ``embedder``'s model and dimension unless it
    exists. Returns its name, or None when the dimension cannot be indexed.

    On an autocommit connection the index is built ``CONCURRENTLY`` (writers
    are never blocked; an invalid leftover of an interrupted build is dropped
    first). Inside a transaction it is built in place, which is instant when it
    is created before the model's vectors, as every caller here does.
    """
    dim = int(embedder.dim)
    if dim > HNSW_MAX_DIM:
        return None
    name = vector_index_name(embedder.model, dim)
    state = conn.execute(
        "SELECT i.indisvalid AS valid FROM pg_index i"
        " JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = %s",
        (name,),
    ).fetchone()
    if state is not None and state["valid"]:
        return name
    idle = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    concurrently = conn.autocommit and idle
    if state is not None and concurrently:
        conn.execute(
            sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(name))
        )
    conn.execute(
        sql.SQL(
            "CREATE INDEX {concurrently} IF NOT EXISTS {name} ON embedding"
            " USING hnsw ((vec::vector({dim})) vector_cosine_ops)"
            " WHERE model = {model} AND dim = {dim}"
        ).format(
            concurrently=sql.SQL("CONCURRENTLY" if concurrently else ""),
            name=sql.Identifier(name),
            dim=sql.SQL(str(dim)),
            model=sql.Literal(embedder.model),
        )
    )
    log.info("vector index %s ready for %s", name, embedder.model)
    return name


def drop_other_vector_indexes(conn: Conn, embedder: Embedder) -> list[str]:
    """Remove the vector indexes of every model but ``embedder``'s. Returns the
    names dropped."""
    keep = vector_index_name(embedder.model, int(embedder.dim))
    names = [
        r["relname"]
        for r in conn.execute(
            "SELECT c.relname FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid"
            " JOIN pg_class t ON t.oid = i.indrelid"
            " WHERE t.relname = 'embedding' AND c.relname LIKE %s"
            " AND c.relname <> %s ORDER BY c.relname",
            (INDEX_PREFIX.replace("_", r"\_") + "%", keep),
        ).fetchall()
    ]
    for name in names:
        conn.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(name)))
    return names
