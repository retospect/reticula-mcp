"""The local embedding index: upkeep, lazy catch-up, rebuild.

Everything here touches the ``embedding`` table only. It is derived from
``node`` (stamped with the node's ``head_version``), never shared, and can be
dropped and rebuilt at any time without changing a single shared row.

Scale note: queries scan the candidate vectors exactly (``<=>`` ordered, after
the visibility filter). When the corpus outgrows that, an HNSW index goes here:
one partial index per model, e.g.
``CREATE INDEX ... ON embedding USING hnsw ((vec::vector(384)) vector_cosine_ops)
WHERE model = '<model>'``, and the semantic leg in ``find.py`` switches to it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from pgvector import Vector

from reticula_mcp.db.pool import Conn
from reticula_mcp.permissions import readable_nodes_sql
from reticula_mcp.search.embedder import Embedder

log = logging.getLogger("reticula_mcp.search")

TEXT_LIMIT = 4000  # characters embedded per node (models truncate anyway)
BATCH = 32
CATCH_UP_CHEAP = 500  # nodes embedded per search with a cheap embedder
CATCH_UP_COSTLY = 32  # ... and with a real model


def node_text(title: str | None, text: str) -> str:
    """What gets embedded for a node."""
    return f"{title}\n{text}"[:TEXT_LIMIT] if title else text[:TEXT_LIMIT]


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


def embed_nodes(conn: Conn, embedder: Embedder, node_ids: list[int]) -> int:
    """(Re)compute the vectors of the given nodes. Tombstoned or missing nodes
    lose their vector instead. Returns how many vectors were written."""
    written = 0
    for start in range(0, len(node_ids), BATCH):
        chunk = node_ids[start : start + BATCH]
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


def catch_up(
    conn: Conn,
    embedder: Embedder,
    viewer_id: int | None = None,
    limit: int | None = None,
) -> int:
    """Embed nodes whose vector is missing or stale (older ``head_version``).

    Bounded: at most ``limit`` nodes per call (default depends on how costly
    the embedder is). With ``viewer_id`` only nodes that viewer can read are
    considered, newest first, so a search always catches up its own world
    first. Returns the number embedded. Safety net for the write hook.
    """
    if limit is None:
        limit = CATCH_UP_CHEAP if getattr(embedder, "cheap", False) else CATCH_UP_COSTLY
    params: dict[str, Any] = {"model": embedder.model, "limit": limit}
    scope = ""
    if viewer_id is not None:
        params["viewer"] = viewer_id
        scope = f" AND n.id IN ({readable_nodes_sql('%(viewer)s')})"
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT n.id FROM node n WHERE n.tombstoned_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM embedding e WHERE e.node_id = n.id"
            "   AND e.model = %(model)s AND e.version_hash = n.head_version)"
            f"{scope} ORDER BY n.id DESC LIMIT %(limit)s",
            params,
        ).fetchall()
    ]
    return embed_nodes(conn, embedder, ids) if ids else 0


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


def reindex(conn: Conn, embedder: Embedder, *, drop: bool = False) -> int:
    """Rebuild the index for ``embedder``'s model: every live node is embedded
    again. ``drop`` first empties the whole table (all models). Never touches
    a shared table. Returns the number of vectors written."""
    if drop:
        conn.execute("DELETE FROM embedding")
    else:  # vectors of nodes tombstoned since are dead weight
        conn.execute(
            "DELETE FROM embedding e USING node n"
            " WHERE n.id = e.node_id AND n.tombstoned_at IS NOT NULL"
        )
    written = 0
    for ids in _all_node_ids(conn):
        written += embed_nodes(conn, embedder, ids)
    return written
