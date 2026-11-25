"""The one hybrid search: lexical + trigram + vector, fused by reciprocal rank.

Candidate rule (the only visibility rule here): a node is a candidate only if it
is in ``current_nodes(viewer)`` (or ``readable_nodes(viewer)`` with
``include_superseded``). Every leg embeds that subquery, so no leg can rank,
count or surface anything the viewer may not read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pgvector import Vector

from reticula_mcp.accounts import Account
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn
from reticula_mcp.permissions import current_nodes_sql, readable_nodes_sql
from reticula_mcp.search import index
from reticula_mcp.search.embedder import Embedder, embed_query, get_embedder
from reticula_mcp.search.rank_fusion import reciprocal_rank_fusion

log = logging.getLogger("reticula_mcp.search")

RRF_K = 60
POOL_MIN = 50
TRIGRAM_MIN = 0.25

# All members of the hub subtree: the hub's members, and (transitively) the
# members of every sub-hub (a hub that is itself a member). UNION, not UNION ALL,
# so a cycle (refused on write, but never trusted here) cannot loop.
_SUBTREE = """
    WITH RECURSIVE sub(id) AS (
        SELECT %(hub)s::bigint
        UNION
        SELECT m.node_id FROM membership m
        JOIN sub ON m.hub_node_id = sub.id
        JOIN node h ON h.id = m.node_id AND h.kind = 'hub'
        WHERE NOT m.withdrawn
    )
    SELECT m.node_id FROM membership m
    WHERE NOT m.withdrawn AND m.hub_node_id IN (SELECT id FROM sub)
"""


@dataclass
class Hit:
    node_id: int
    score: float
    legs: dict[str, tuple[int, float]]


def _scope(
    viewer: Account,
    *,
    hub_node_id: int | None,
    kind: str | None,
    include_superseded: bool,
) -> tuple[str, dict[str, Any]]:
    visible = readable_nodes_sql if include_superseded else current_nodes_sql
    where = [
        f"n.id IN ({visible('%(viewer)s')})",
        "n.tombstoned_at IS NULL",
        "(%(kind)s::text IS NULL OR n.kind = %(kind)s::text)",
        # debug notes only when asked for by kind
        "(n.kind <> 'debug' OR %(kind)s::text = 'debug')",
    ]
    params: dict[str, Any] = {"viewer": viewer.id, "kind": kind}
    if hub_node_id is not None:
        where.append(f"n.id IN ({_SUBTREE})")
        params["hub"] = hub_node_id
    return " AND ".join(where), params


def _lexical(
    conn: Conn, query: str, where: str, params: dict[str, Any], pool: int
) -> list[tuple[int, float]]:
    base = {**params, "q": query, "pool": pool}
    strict = conn.execute(
        f"""
        SELECT n.id, ts_rank_cd(n.fts, qq.qq, 32) AS score
        FROM node n, websearch_to_tsquery('english', %(q)s) AS qq(qq)
        WHERE n.fts @@ qq.qq AND {where}
        ORDER BY score DESC, n.id DESC LIMIT %(pool)s
        """,
        base,
    ).fetchall()
    if strict:
        return [(r["id"], float(r["score"])) for r in strict]
    # Strict (AND) query matched nothing: retry with the same terms OR-ed, the
    # way a long natural-language question needs.
    loose = conn.execute(
        f"""
        SELECT n.id, ts_rank_cd(n.fts, oq.oq, 32) AS score
        FROM node n,
             to_tsquery('english',
                 replace(plainto_tsquery('english', %(q)s)::text, ' & ', ' | ')
             ) AS oq(oq)
        WHERE n.fts @@ oq.oq AND {where}
        ORDER BY score DESC, n.id DESC LIMIT %(pool)s
        """,
        base,
    ).fetchall()
    return [(r["id"], float(r["score"])) for r in loose]


def _trigram(
    conn: Conn, query: str, where: str, params: dict[str, Any], pool: int
) -> list[tuple[int, float]]:
    # Exact scan of the (already visibility-restricted) titles. When the corpus
    # grows, use the GIN index node_title_trgm with `n.title % q` instead.
    rows = conn.execute(
        f"""
        SELECT n.id, similarity(n.title, %(q)s) AS score
        FROM node n
        WHERE n.title IS NOT NULL AND similarity(n.title, %(q)s) >= %(min)s
          AND {where}
        ORDER BY score DESC, n.id DESC LIMIT %(pool)s
        """,
        {**params, "q": query, "min": TRIGRAM_MIN, "pool": pool},
    ).fetchall()
    return [(r["id"], float(r["score"])) for r in rows]


def _semantic(
    conn: Conn,
    embedder: Embedder,
    query: str,
    where: str,
    params: dict[str, Any],
    pool: int,
) -> list[tuple[int, float]]:
    qvec = embed_query(embedder, query)
    max_distance = float(getattr(embedder, "max_distance", 1.0))
    # model-filtered candidates first (MATERIALIZED): vectors of other models
    # may have another dimension and must never meet the distance operator.
    rows = conn.execute(
        f"""
        WITH cand AS MATERIALIZED (
            SELECT e.node_id, e.vec FROM embedding e JOIN node n ON n.id = e.node_id
            WHERE e.model = %(model)s AND {where}
        )
        SELECT node_id AS id, d FROM (
            SELECT c.node_id, c.vec <=> %(vec)s::vector AS d FROM cand c
        ) x
        WHERE x.d <= %(max)s
        ORDER BY d, id DESC LIMIT %(pool)s
        """,
        {
            **params,
            "model": embedder.model,
            "vec": Vector(qvec),
            "max": max_distance,
            "pool": pool,
        },
    ).fetchall()
    return [(r["id"], 1.0 - float(r["d"])) for r in rows]


def hybrid_search(
    conn: Conn,
    viewer: Account,
    query: str,
    *,
    hub_node_id: int | None = None,
    kind: str | None = None,
    limit: int = 10,
    include_superseded: bool = False,
    settings: Settings | None = None,
) -> list[Hit]:
    """Ranked hits (best first) among the nodes ``viewer`` may read."""
    where, params = _scope(
        viewer,
        hub_node_id=hub_node_id,
        kind=kind,
        include_superseded=include_superseded,
    )
    pool = max(POOL_MIN, limit * 5)
    legs: list[tuple[str, list[tuple[int, float]]]] = [
        ("lexical", _lexical(conn, query, where, params, pool)),
        ("title", _trigram(conn, query, where, params, pool)),
    ]
    embedder: Embedder | None = None
    try:
        embedder = get_embedder(settings or Settings.from_env())
        with conn.transaction():  # savepoint: upkeep trouble must not sink a search
            index.catch_up(conn, embedder, viewer.id)
            legs.append(
                ("semantic", _semantic(conn, embedder, query, where, params, pool))
            )
    except Exception:
        # No model, a failed download, a dimension surprise: lexical still works.
        log.warning("semantic leg skipped", exc_info=True)
    scores, provenance = reciprocal_rank_fusion(legs, k=RRF_K)
    ranked = sorted(scores, key=lambda i: (-scores[i], -i))[:limit]
    return [Hit(i, scores[i], provenance[i]) for i in ranked]
