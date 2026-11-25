"""The one hybrid search: lexical + trigram + vector, fused by reciprocal rank.

Candidate rule (the only visibility rule here): a node is a candidate only if it
is in ``current_nodes(viewer)`` (or ``readable_nodes(viewer)`` with
``include_superseded``). Every leg embeds that subquery, so no leg can rank,
count or surface anything the viewer may not read.

The vector leg is optional by design. It runs only when the embedder can
answer without a long wait (``embedder.is_ready``); otherwise the search asks
it to load in the background, answers from the keyword legs and says so
(``Found.semantic``). It compares only vectors of the CURRENT model and
dimension: rows of any other model in the ``embedding`` table are never read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pgvector import Vector
from psycopg import sql

from reticula_mcp.accounts import Account
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn
from reticula_mcp.permissions import current_nodes_sql, readable_nodes_sql
from reticula_mcp.search import index
from reticula_mcp.search.embedder import (
    Embedder,
    embed_query,
    get_embedder,
    is_ready,
    load_error,
    warm,
)
from reticula_mcp.search.rank_fusion import reciprocal_rank_fusion

log = logging.getLogger("reticula_mcp.search")

RRF_K = 60
POOL_MIN = 50
TRIGRAM_MIN = 0.25
# The share of the query's distinct words a note must have to be a keyword
# match when no note has all of them (see _lexical). 0 keeps any note with one
# of the words; 1 means every word, that is no second try at all.
LOOSE_MIN_SHARE = 0.5
# Weight of each leg in the fusion (see rank_fusion.py).
LEG_WEIGHTS: dict[str, float] = {"lexical": 1.0, "title": 1.0, "semantic": 1.0}
# Up to this many candidate vectors the vector leg compares every one of them
# (exact, and fast: the candidates are only what the viewer may read). Beyond
# it the model's HNSW index is used, with pgvector's iterative scan so that
# the visibility filter cannot starve the result.
EXACT_MAX = 20_000

SEMANTIC_ON = "on"  # the vector leg ran
SEMANTIC_LOADING = "loading"  # the model is still loading: keywords only
SEMANTIC_OFF = "off"  # no model, or it failed: keywords only


def _subtree(viewer_sql: str) -> str:
    """All members of the hub subtree, as this viewer may see it: the hub's
    members, and (transitively) the members of every sub-hub.

    The walk goes only through sub-hubs the viewer can read, and counts only
    the filings the viewer may see (not withdrawn; made for the hub's readers,
    or the viewer's own private ones), the same rule as the member list of
    ``get``. So a note is never matched through a sub-hub the viewer cannot
    read or through somebody else's private filing, which would tell them where
    it is filed. UNION, not UNION ALL, so a cycle (refused on write, but never
    trusted here) cannot loop.
    """
    return f"""
        WITH RECURSIVE sub(id) AS (
            SELECT %(hub)s::bigint
            UNION
            SELECT m.node_id FROM membership m
            JOIN sub ON m.hub_node_id = sub.id
            JOIN node h ON h.id = m.node_id AND h.kind = 'hub'
            WHERE NOT m.withdrawn
              AND (m.audience = 'hub' OR m.author = %(person)s)
              AND h.id IN ({readable_nodes_sql(viewer_sql)})
        )
        SELECT m.node_id FROM membership m
        WHERE NOT m.withdrawn
          AND (m.audience = 'hub' OR m.author = %(person)s)
          AND m.hub_node_id IN (SELECT id FROM sub)
    """


@dataclass
class Hit:
    node_id: int
    score: float
    legs: dict[str, tuple[int, float]]


@dataclass
class Found:
    hits: list[Hit]
    semantic: str  # SEMANTIC_ON | SEMANTIC_LOADING | SEMANTIC_OFF


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
        where.append(f"n.id IN ({_subtree('%(viewer)s')})")
        params["hub"] = hub_node_id
        params["person"] = viewer.person_id
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
    # No note has every word: retry with the words OR-ed, and keep a note only
    # when it has at least LOOSE_MIN_SHARE of the query's distinct words (one
    # common word such as "work" or "day" is not a match). Most words first.
    loose = conn.execute(
        f"""
        WITH q AS (
            SELECT tsvector_to_array(to_tsvector('english', %(q)s)) AS words
        )
        SELECT n.id, ts_rank_cd(n.fts, oq.oq, 32) AS score
        FROM q,
             to_tsquery('english',
                 replace(plainto_tsquery('english', %(q)s)::text, ' & ', ' | ')
             ) AS oq(oq),
             node n,
             LATERAL (
                 SELECT count(*) AS n FROM unnest(tsvector_to_array(n.fts)) AS w(word)
                 WHERE w.word = ANY(q.words)
             ) AS shared
        WHERE n.fts @@ oq.oq AND {where}
          AND shared.n >= ceil(cardinality(q.words) * %(share)s::numeric)
        ORDER BY shared.n DESC, score DESC, n.id DESC LIMIT %(pool)s
        """,
        {**base, "share": LOOSE_MIN_SHARE},
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


def _candidates(
    conn: Conn, embedder: Embedder, dim: int, where: str, params: dict[str, Any]
) -> int:
    """How many vectors the exact scan would compare, counted up to
    ``EXACT_MAX + 1``. The table's row estimate answers for small instances
    without a count."""
    stat = conn.execute(
        "SELECT reltuples::bigint AS n FROM pg_class WHERE oid = 'embedding'::regclass"
    ).fetchone()
    if stat is not None and 0 <= stat["n"] <= EXACT_MAX:
        return int(stat["n"])
    row = conn.execute(
        f"""
        SELECT count(*) AS n FROM (
            SELECT 1 FROM embedding e JOIN node n ON n.id = e.node_id
            WHERE e.model = %(model)s AND e.dim = %(dim)s AND {where}
            LIMIT %(cap)s
        ) c
        """,
        {**params, "model": embedder.model, "dim": dim, "cap": EXACT_MAX + 1},
    ).fetchone()
    return int(row["n"]) if row else 0


def _iterative_scan(conn: Conn) -> bool:
    """pgvector 0.8 or newer: an index scan can go on past ``ef_search`` until
    enough rows pass the filter."""
    row = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    if row is None:
        return False
    try:
        major, minor = (int(part) for part in str(row["extversion"]).split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= (0, 8)


def semantic_sql(embedder: Embedder, dim: int, where: str, *, indexed: bool) -> str:
    """The vector leg's query. Both forms read only rows of the embedder's own
    model and dimension.

    ``indexed``: ordered by the distance over ``vec::vector(dim)``, the exact
    expression of the model's partial HNSW index, with the model as a literal
    so that the planner can match the index predicate. The cast is in the
    select list and the ORDER BY only, which are evaluated after the model
    filter, so a vector of another dimension never reaches it; the distance
    cutoff sits outside the LIMIT for the same reason.

    Exact form: the candidates are materialised first, then compared.
    """
    if indexed:
        expr = index.vector_expr(dim)
        model = sql.Literal(embedder.model).as_string().replace("%", "%%")
        return f"""
            SELECT id, d FROM (
                SELECT e.node_id AS id, {expr} <=> %(vec)s::vector({dim}) AS d
                FROM embedding e
                WHERE e.model = {model} AND e.dim = {dim}
                  AND e.node_id IN (SELECT n.id FROM node n WHERE {where})
                ORDER BY {expr} <=> %(vec)s::vector({dim})
                LIMIT %(pool)s
            ) x
            WHERE x.d <= %(max)s
            ORDER BY d, id DESC
        """
    return f"""
        WITH cand AS MATERIALIZED (
            SELECT e.node_id, e.vec FROM embedding e JOIN node n ON n.id = e.node_id
            WHERE e.model = %(model)s AND e.dim = %(dim)s AND {where}
        )
        SELECT node_id AS id, d FROM (
            SELECT c.node_id, c.vec <=> %(vec)s::vector AS d FROM cand c
        ) x
        WHERE x.d <= %(max)s
        ORDER BY d, id DESC LIMIT %(pool)s
    """


def _semantic(
    conn: Conn,
    embedder: Embedder,
    query: str,
    where: str,
    params: dict[str, Any],
    pool: int,
    keyword_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    qvec = embed_query(embedder, query)
    dim = len(qvec)
    max_distance = float(getattr(embedder, "max_distance", 1.0))
    # Instructions come back through meaning for almost any question about
    # the product itself, so they join this leg only when asked for by kind
    # or when a keyword leg matched them.
    where = (
        f"{where} AND (n.kind <> 'instruction' OR %(kind)s::text = 'instruction'"
        " OR n.id = ANY(%(keyword_ids)s))"
    )
    params = {**params, "keyword_ids": keyword_ids or []}
    indexed = (
        _candidates(conn, embedder, dim, where, params) > EXACT_MAX
        and index.has_vector_index(conn, embedder)
        and _iterative_scan(conn)
    )
    args: dict[str, Any] = {
        **params,
        "vec": Vector(qvec),
        "max": max_distance,
        "pool": pool,
    }
    if indexed:
        conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        conn.execute(f"SET LOCAL hnsw.ef_search = {max(40, min(pool * 2, 1000))}")
    else:
        args.update(model=embedder.model, dim=dim)
    rows = conn.execute(
        semantic_sql(embedder, dim, where, indexed=indexed), args
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
) -> Found:
    """Ranked hits (best first) among the nodes ``viewer`` may read, and
    whether the vector leg took part."""
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
    semantic = SEMANTIC_OFF
    try:
        embedder = get_embedder(settings or Settings.from_env())
        if not is_ready(embedder):
            # Never load a model inside a tool call: start it in the background
            # (nothing happens when it is loading, or failed a moment ago) and
            # answer from keywords now.
            failed = load_error(embedder)  # read before the retry can clear it
            warm(embedder)
            semantic = SEMANTIC_OFF if failed else SEMANTIC_LOADING
        else:
            keyword_ids = [i for _, rows in legs for i, _ in rows]
            cheap = bool(getattr(embedder, "cheap", False))
            with conn.transaction():  # savepoint: trouble here must not sink a search
                index.catch_up(
                    conn,
                    embedder,
                    viewer.id,
                    seconds=None if cheap else index.CATCH_UP_SECONDS,
                )
                hits = _semantic(
                    conn, embedder, query, where, params, pool, keyword_ids
                )
            legs.append(("semantic", hits))
            semantic = SEMANTIC_ON
    except Exception:
        # No model, a dimension surprise, a full disk: keywords still work.
        log.warning("semantic leg skipped", exc_info=True)
        semantic = SEMANTIC_OFF
    scores, provenance = reciprocal_rank_fusion(legs, k=RRF_K, weights=LEG_WEIGHTS)
    ranked = sorted(scores, key=lambda i: (-scores[i], -i))[:limit]
    return Found([Hit(i, scores[i], provenance[i]) for i in ranked], semantic)
