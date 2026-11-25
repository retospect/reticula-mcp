"""The one hybrid search: lexical + trigram + vector, fused by reciprocal rank.

Two kinds of match are certain and go first, ahead of the fused ranking: the
paper stored under the identifier the query is (``_identifier``), and notes
whose title or tag is the query (``_exact``).

Candidate rule (the only visibility rule here): a node is a candidate only if it
is in ``current_nodes(viewer)`` (or ``readable_nodes(viewer)`` with
``include_superseded``). Every leg embeds that subquery, so no leg can rank,
count or surface anything the viewer may not read. A search inside given ids
(``only_ids``) asks the same function once about those ids, and every leg then
embeds the ids that passed (see ``scope``).

The vector leg is optional by design. It runs only when the embedder can
answer without a long wait (``embedder.is_ready``); otherwise the search asks
it to load in the background, answers from the keyword legs and says so
(``Found.semantic``). It compares only vectors of the CURRENT model and
dimension: rows of any other model in the ``embedding`` table are never read.
"""

from __future__ import annotations

import logging
import re
import string
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pgvector import Vector
from psycopg import sql

from reticula_mcp.accounts import Account
from reticula_mcp.config import Settings
from reticula_mcp.content.bootstrap import SHIPPED_TAG
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph.paper import normalize_ext_id
from reticula_mcp.permissions import (
    current_nodes_sql,
    readable_hub_ids,
    readable_hub_sql,
    readable_nodes_sql,
)
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
from reticula_mcp.search.rows import visible_subset

log = logging.getLogger("reticula_mcp.search")

RRF_K = 60
POOL_MIN = 50
TRIGRAM_MIN = 0.25
# How much of a title must be found inside the query, word for word, for the
# title to match although the query is much longer (see _trigram). 1 is the
# title exactly as written. 0.8 forgives a plural in a title of several words;
# a one-word title has to be there letter for letter.
TITLE_INSIDE_MIN = 0.8
# A query with more counted words than this is a pasted text, not a question:
# titles are not looked for inside it. Many short titles stand somewhere in a
# long text, and the comparison costs more with every character of the query.
TITLE_INSIDE_WORDS = 16
# Up to this many characters of query the comparison of title and query as
# wholes goes through the trigram index. The index looks up every trigram of
# the query, so it costs with the query's length; a longer query is compared
# only with the titles long enough to resemble it (see _trigram).
TITLE_BY_INDEX = 100
# The share of the query's distinct words a note must have to be a keyword
# match when no note has all of them (see _lexical). 0 keeps any note with one
# of the words; 1 means every word, that is no second try at all.
LOOSE_MIN_SHARE = 0.5
# A search that shows fewer rows than this is filled up with notes that have
# only some of the query's words (see _partial). They are returned apart from
# the hits and shown under a line that says what they are.
PARTIAL_FILL = 3
# How many of the query's words are looked for in such a note: the first ones
# of a query that is a whole pasted sentence.
PARTIAL_WORDS = 16
# Of the rows filled in, at most this many are from outside the viewer's own
# notes and hubs (see _partial), so that a hub somebody shared unasked cannot
# take them.
PARTIAL_OTHERS = 1
# Of the notes the keyword leg's second try found (half of the words or more),
# at most this many from outside the viewer's own notes and hubs are shown.
# Three keep a library that somebody shares findable; a hub filled with notes
# to take the rows gets three of them and not ten.
PARTIAL_KEPT_OTHERS = 3
# A word of the query that more notes than this have (of those the viewer can
# read, in the scope of the search) is a common word there: a name, "call",
# "new". A note is filled in only when it has a word that is not common. In a
# big account every such word is in hundreds of notes, and three of them say
# nothing about the subject. A distance limit does not do this: among hundreds
# of notes some are always near.
PARTIAL_COMMON = 50
# Words that name what is kept here rather than what a note says. "kiln notes"
# asks for the notes about kilns, and those need not contain "notes": such a
# word is never required of a note (see content_words). A note that has it
# still comes before one that has not.
REQUEST_WORDS = frozenset(
    [
        "note",
        "notes",
        "hub",
        "hubs",
        "paper",
        "papers",
        "instruction",
        "instructions",
        "entry",
        "entries",
        "memo",
        "memos",
        "stuff",
        "info",
        "information",
        "everything",
        "anything",
    ]
)
_QUERY_PART = re.compile(r'-?"[^"]*"?|\S+')
# A word that says what a note is about, as a condition on a stem ``w`` of
# the text search: three letters or more, not a bare number or date, and not
# the stem of one of the REQUEST_WORDS (passed as the parameter ``request``).
_REQUEST_TEXT = " ".join(sorted(REQUEST_WORDS))
_COUNTS = (
    "length(w) >= 3 AND w !~ '^[-0-9.,:/]+$'"
    " AND w <> ALL (tsvector_to_array(to_tsvector('english', %(request)s)))"
)
# Weight of each leg in the fusion (see rank_fusion.py).
LEG_WEIGHTS: dict[str, float] = {
    "identifier": 1.0,
    "exact": 1.0,
    "lexical": 1.0,
    "title": 1.0,
    "semantic": 1.0,
}
# Up to this many candidate vectors the vector leg compares every one of them
# (exact, and fast: the candidates are only what the viewer may read). Beyond
# it the model's HNSW index is used, with pgvector's iterative scan so that
# the visibility filter cannot starve the result.
EXACT_MAX = 20_000

SEMANTIC_ON = "on"  # the vector leg ran
SEMANTIC_LOADING = "loading"  # the model is still loading: keywords only
SEMANTIC_OFF = "off"  # no model, or it failed: keywords only

# A search inside given ids: the ids that passed the check of ``scope``.
ONLY_IDS_SQL = "n.id = ANY(%(only_ids)s::bigint[])"


def subtree_sql() -> str:
    """All members of the hub subtree, as this viewer may see it: the hub's
    members, and (transitively) the members of every sub-hub. A query of one
    column, ``node_id``, to embed as ``n.id IN (<this>)``.

    It reads three parameters, all of which :func:`scope` puts into its
    params when a hub is given: ``hub`` (the hub's node id), ``person`` (the
    viewer's person id) and ``hubs`` (``permissions.readable_hub_ids``).

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
              AND {readable_hub_sql("h.id")}
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
class Partial:
    """A note that has only some of the query's words."""

    node_id: int
    words: list[str]  # the words of the query it has, as they were typed
    near: bool  # the viewer wrote it, or it is near the viewer (see _partial)


@dataclass
class Found:
    hits: list[Hit]
    semantic: str  # SEMANTIC_ON | SEMANTIC_LOADING | SEMANTIC_OFF
    partial: list[Partial] = field(default_factory=list)
    more: int = 0  # notes near the viewer like those in ``partial``, not in it


def scope(
    conn: Conn,
    viewer: Account,
    *,
    hub_node_id: int | None,
    kind: str | None,
    include_superseded: bool,
    only_ids: Sequence[int] | None = None,
) -> tuple[str, dict[str, Any]]:
    """What a search or a listing may return at all, as a condition on ``node
    n`` and its parameters: what the viewer may read (without the notes a
    readable newer note replaces, unless ``include_superseded``), of ``kind``
    if given, no debug note unless that kind is asked for, and inside the hub
    ``hub_node_id`` if given (:func:`subtree_sql`).

    ``only_ids``: nothing outside these node ids. The caller need not have
    checked them: which of them the viewer may read (and which are current) is
    asked here, once, and the condition then names the ids that passed, so the
    statements built on it do not ask again. An empty list finds nothing.

    The params always hold ``viewer`` (account id), ``person`` (person id),
    ``kind`` and ``hubs`` (``permissions.readable_hub_ids``, for statements
    that test a hub with ``permissions.readable_hub_sql``), and ``hub`` when a
    hub is given. A caller adds its own conditions and parameters to both.
    """
    visible = readable_nodes_sql if include_superseded else current_nodes_sql
    where = [
        f"n.id IN ({visible('%(viewer)s')})" if only_ids is None else ONLY_IDS_SQL,
        "n.tombstoned_at IS NULL",
        "(%(kind)s::text IS NULL OR n.kind = %(kind)s::text)",
        # debug notes only when asked for by kind
        "(n.kind <> 'debug' OR %(kind)s::text = 'debug')",
    ]
    params: dict[str, Any] = {
        "viewer": viewer.id,
        "person": viewer.person_id,
        "kind": kind,
        "hubs": readable_hub_ids(conn, viewer),
    }
    if only_ids is not None:
        params["only_ids"] = sorted(
            visible_subset(conn, viewer, list(only_ids), current=not include_superseded)
        )
    if hub_node_id is not None:
        where.append(f"n.id IN ({subtree_sql()})")
        params["hub"] = hub_node_id
    return " AND ".join(where), params


def _identifier(
    conn: Conn, query: str, where: str, params: dict[str, Any], pool: int
) -> list[tuple[int, float]]:
    """Papers stored under the identifier that the query is: a DOI in any
    spelling, an arXiv id, or any other id exactly as it was given. The key is
    the one ``put`` stores (``graph/paper.py``), so a search before writing
    finds the paper that a write would have run into. Oldest first: that is
    the one ``put`` returns."""
    try:
        key, _ = normalize_ext_id(query)
    except ReticulaError:
        return []
    rows = conn.execute(
        f"""
        SELECT n.id FROM node n
        WHERE n.kind = 'paper' AND n.ext_key = %(key)s AND {where}
        ORDER BY n.id LIMIT %(pool)s
        """,
        {**params, "key": key, "pool": pool},
    ).fetchall()
    # then the notes written about it (they carry the identifier), newest first
    about = conn.execute(
        f"""
        SELECT n.id FROM node n
        WHERE n.kind <> 'paper' AND n.payload ? 'ext'
          AND n.payload->'ext'->>'key' = %(key)s AND {where}
        ORDER BY n.created_at DESC, n.id DESC LIMIT %(pool)s
        """,
        {**params, "key": key, "pool": pool},
    ).fetchall()
    return [(r["id"], 1.0) for r in (*rows, *about)]


def _exact(
    conn: Conn, query: str, where: str, params: dict[str, Any], pool: int
) -> list[tuple[int, float]]:
    """Notes whose title is the query (case and surrounding space ignored),
    then notes that carry the query as a tag (case and a leading ``#``
    ignored), newest first."""
    key = query.strip().lower()
    tag = key.lstrip("#").strip()
    ws = " \t\r\n"
    rows = conn.execute(
        f"""
        SELECT n.id,
               CASE WHEN lower(btrim(n.title, %(ws)s)) = %(key)s THEN 2.0
                    ELSE 1.0 END AS score
        FROM node n
        WHERE {where}
          AND (lower(btrim(n.title, %(ws)s)) = %(key)s
               OR (%(tag)s <> '' AND EXISTS (
                    SELECT 1 FROM unnest(n.tags) t
                    WHERE lower(ltrim(btrim(t, %(ws)s), '#')) = %(tag)s)))
        ORDER BY score DESC, n.created_at DESC, n.id DESC LIMIT %(pool)s
        """,
        {**params, "key": key, "tag": tag, "ws": ws, "pool": pool},
    ).fetchall()
    return [(r["id"], float(r["score"])) for r in rows]


def content_words(query: str) -> str:
    """The query without its REQUEST_WORDS: "kiln notes" is "kiln". A quoted
    phrase and an excluded word (``-notes``) stay as written. The query comes
    back unchanged when nothing else would be left, and when it has an ``or``
    (dropping one side of an ``or`` would narrow the search, not widen it)."""
    kept: list[str] = []
    content = dropped = False
    for part in _QUERY_PART.findall(query):
        if part.lower() == "or":
            return query
        if part.startswith(("-", '"')):
            kept.append(part)
            content = content or part.startswith('"')
        elif part.strip(string.punctuation).lower() in REQUEST_WORDS:
            dropped = True
        else:
            kept.append(part)
            content = True
    return " ".join(kept) if dropped and content else query


def counted_words(conn: Conn, query: str) -> tuple[list[str], str]:
    """The words of the query that say what is looked for, as they were typed
    and in that order, each stem once; and the part of the query that excludes
    (``-word``). A word counts when a stem of it passes ``_COUNTS``: "SD", "3",
    "notes" and stop words do not. The words of a quoted phrase count one by
    one."""
    words: list[str] = []
    minus: list[str] = []
    for part in _QUERY_PART.findall(content_words(query)):
        if part.startswith("-"):
            minus.append(part)
            continue
        for token in part.strip('"').split():
            word = token.strip(string.punctuation)
            if word:
                words.append(word)
    if not words:
        return [], " ".join(minus)
    rows = conn.execute(
        f"""
        SELECT t.word, to_tsvector('english', t.word)::text AS stems
        FROM unnest(%(words)s::text[]) WITH ORDINALITY AS t(word, ord)
        WHERE EXISTS (
            SELECT 1
            FROM unnest(tsvector_to_array(to_tsvector('english', t.word))) w
            WHERE {_COUNTS})
        ORDER BY t.ord
        """,
        {"words": words, "request": _REQUEST_TEXT},
    ).fetchall()
    seen: set[str] = set()
    counted: list[str] = []
    for row in rows:
        if row["stems"] not in seen:
            seen.add(row["stems"])
            counted.append(row["word"])
    return counted, " ".join(minus)


def _word_terms(words: list[str], args: dict[str, Any]) -> list[str]:
    """One text-search query per word, as SQL over parameters added to
    ``args``. Each is a constant to the planner, so ``n.fts @@ (a || b)`` can
    use the index on ``fts``."""
    terms = []
    for i, word in enumerate(words):
        args[f"word{i}"] = word
        terms.append(f"plainto_tsquery('english', %(word{i})s)")
    return terms


def _lexical(
    conn: Conn, query: str, where: str, params: dict[str, Any], pool: int
) -> list[tuple[int, float]]:
    """The keyword leg: the notes that have every word, or, when there is
    none, the second try (see ``_keyword``)."""
    return _keyword(conn, query, where, params, pool)[0]


def _keyword(
    conn: Conn, query: str, where: str, params: dict[str, Any], pool: int
) -> tuple[list[tuple[int, float]], bool]:
    """The keyword matches, and whether they are from the second try: notes
    that do not have every word."""
    # ``q`` is what a note must have, ``every`` is all that was typed
    base = {**params, "q": content_words(query), "every": query, "pool": pool}
    # A query with no positive term (only negations such as "-zebra", or only
    # stop words) has nothing to require: "!zebra" would match every note
    # without the word.
    trees = conn.execute(
        "SELECT querytree(websearch_to_tsquery('english', %(q)s)) AS q,"
        " querytree(websearch_to_tsquery('english', %(every)s)) AS every",
        base,
    ).fetchone()
    nothing = ("", "T")
    if trees is None or trees["every"] in nothing:
        return [], False
    if trees["q"] in nothing:  # "the notes": the request word is all there is
        base["q"] = query
    # Both statements are planned for the words at hand each time
    # (prepare=False). A connection that has run one of them a few times
    # otherwise keeps one plan for any words, made without knowing how many
    # notes have them: measured at 6,000 notes, the second try then took 117 ms
    # instead of 22 for five words, and 12 s for a 1,000-character query.
    # Notes with every word typed first, then those with every content word.
    strict = conn.execute(
        f"""
        SELECT n.id, (n.fts @@ eq.eq) AS every,
               ts_rank_cd(
                   n.fts, CASE WHEN n.fts @@ eq.eq THEN eq.eq ELSE qq.qq END, 32
               ) AS score
        FROM node n, websearch_to_tsquery('english', %(q)s) AS qq(qq),
             websearch_to_tsquery('english', %(every)s) AS eq(eq)
        WHERE n.fts @@ qq.qq AND {where}
        ORDER BY every DESC, score DESC, n.id DESC LIMIT %(pool)s
        """,
        base,
        prepare=False,
    ).fetchall()
    if strict:
        return [(r["id"], float(r["score"])) for r in strict], False
    # No note has every content word: retry with those words OR-ed, and keep a
    # note only when it has at least LOOSE_MIN_SHARE of the distinct ones (one
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
        prepare=False,
    ).fetchall()
    return [(r["id"], float(r["score"])) for r in loose], True


def _trigram(
    conn: Conn,
    query: str,
    where: str,
    params: dict[str, Any],
    pool: int,
    words: list[str] | None = None,
) -> list[tuple[int, float]]:
    # A title matches when it resembles the whole query (TRIGRAM_MIN), or when
    # it stands inside the query as written (TITLE_INSIDE_MIN). The second is
    # for a long query and a short title: every further word of the query
    # lowers the similarity of the two as wholes, so "Gateway" was found for
    # four words and lost for five. It needs a title that has one of the
    # query's counted words (``words``, see counted_words): "Notes", "To do"
    # or "3" inside a query is no match.
    # Of two titles that match equally the viewer's own comes first, so that
    # titles other people chose cannot take the leg from the viewer's notes.
    #
    # Neither comparison is computed for every title: each costs time with
    # every character of the query (at 6,000 titles and a 1,000-character
    # query 0.4 s the first and 0.9 s the second, row by row).
    #
    # The first goes through the trigram index on the title (`%` with the
    # threshold set to TRIGRAM_MIN for this transaction) for a query of up to
    # TITLE_BY_INDEX characters. A longer one is compared only with the titles
    # long enough: two texts share at most the trigrams of the shorter, so a
    # title with fewer than TRIGRAM_MIN times the query's trigrams cannot
    # resemble it, and a title has at most one trigram more than it has
    # characters. The CASE makes that test come first whatever the plan.
    # The second runs over the titles that have one of the counted words (the
    # index of migration 0301), and not at all for a query of more than
    # TITLE_INSIDE_WORDS counted words.
    #
    # The titles are found first and who may read them is decided after
    # (MATERIALIZED keeps the planner from turning that around). Left to
    # choose, it walks every readable note and compares title by title: it
    # takes the readable notes for 200 rows however many they are.
    if words is None:
        words, _ = counted_words(conn, query)
    args: dict[str, Any] = {
        **params,
        "q": query,
        "min": TRIGRAM_MIN,
        "inside": TITLE_INSIDE_MIN,
        "pool": pool,
    }
    if len(query) <= TITLE_BY_INDEX:
        whole = "t.title %% %(q)s AND similarity(t.title, %(q)s) >= %(min)s"
    else:
        whole = """
            CASE WHEN char_length(lower(t.title)) + 1
                      >= %(min)s * cardinality(show_trgm(%(q)s))
                 THEN similarity(t.title, %(q)s) >= %(min)s
                 ELSE false END
        """
    inside = ""
    if words and len(words) <= TITLE_INSIDE_WORDS:
        any_word = " || ".join(_word_terms(words, args))
        inside = f"""
          UNION ALL
            SELECT t.id, strict_word_similarity(t.title, %(q)s) AS score
            FROM node t
            WHERE to_tsvector('english', coalesce(t.title, '')) @@ ({any_word})
              AND strict_word_similarity(t.title, %(q)s) >= %(inside)s
        """
    with conn.transaction():
        # `%` compares with a setting of the session. It is set for this
        # statement and put back after, since a setting made in a transaction
        # outlives this block. On a connection that has not used the trigram
        # extension yet the setting does not exist: that reads as NULL, and
        # NULL puts back the default.
        previous = conn.execute(
            "SELECT current_setting('pg_trgm.similarity_threshold', true) AS was"
        ).fetchone()
        conn.execute(
            "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
            (str(TRIGRAM_MIN),),
        )
        rows = conn.execute(
            f"""
            WITH found AS MATERIALIZED (
                SELECT t.id, similarity(t.title, %(q)s) AS score
                FROM node t
                WHERE {whole}
                {inside}
            )
            SELECT n.id, max(f.score) AS score
            FROM found f JOIN node n ON n.id = f.id
            WHERE {where}
            GROUP BY n.id
            ORDER BY score DESC, (n.author = %(person)s) DESC, n.id DESC
            LIMIT %(pool)s
            """,
            args,
            prepare=False,
        ).fetchall()
        conn.execute(
            "SELECT set_config('pg_trgm.similarity_threshold', %s::text, true)",
            (previous["was"] if previous else None,),
        )
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
    qvec: list[float] | None = None,
) -> list[tuple[int, float]]:
    if qvec is None:
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


def _partial(
    conn: Conn,
    where: str,
    params: dict[str, Any],
    words: list[str],
    minus: str,
    *,
    taken: list[int],
    keep: list[int],
    fill: int,
    room: int,
    model: tuple[str, list[float]] | None,
) -> tuple[list[Partial], int]:
    """Notes that have some of ``words`` and are not among the hits
    (``taken``), and how many more such notes near the viewer there are than
    are returned. Notes from outside are not counted: the count sends the
    reader on to another search, and nobody else is to raise it.

    ``keep`` are the notes the keyword leg's second try found and no other leg
    did: they stay, as before, when they have a counted word, those from
    outside (not near, see below) up to PARTIAL_KEPT_OTHERS. Beyond them the
    result is filled until it has ``fill`` rows.

    The rows filled in go first to the notes near the viewer. A note is near
    when the viewer wrote it, when its author is somebody the viewer has given
    access to a hub, or when it stands in a hub the viewer has filed something
    into (not a hub anyone may file into): each is the viewer's own doing, so
    nobody can make their notes near to someone else. Of all other notes at most
    PARTIAL_OTHERS are filled in, and only into rows the near ones leave
    empty: somebody who shares a hub unasked adds one row at most.

    Order, for the choice and for the result: the near ones first, then the
    notes with most of the words, then the nearest in meaning (``model``: the
    model's name and the query's vector, None when the vector leg did not
    run), then the newest. What is shipped with the product is never one of
    them, and an instruction only when the search asks for that kind or the
    second try found it. A note is filled in only when it has a word that is
    not common among these notes (PARTIAL_COMMON). Never more than ``room``
    rows.
    """
    words = words[:PARTIAL_WORDS]
    if not words or room <= 0 or (fill <= 0 and not keep):
        return [], 0
    args: dict[str, Any] = {
        **params,
        "taken": taken,
        "keep": keep,
        "shipped": SHIPPED_TAG,
        "fill": max(fill, 0),
        "others": PARTIAL_OTHERS,
        "kept_others": PARTIAL_KEPT_OTHERS,
        "common": PARTIAL_COMMON,
    }
    terms = _word_terms(words, args)
    has = ", ".join(f"n.fts @@ {term}" for term in terms)
    any_word = " || ".join(terms)
    places = range(1, len(terms) + 1)
    few = ", ".join(f"count(*) FILTER (WHERE c.has[{i}]) <= %(common)s" for i in places)
    telling = " OR ".join(f"(c.has[{i}] AND few.few[{i}])" for i in places)
    excluded = ""
    if minus:
        # "-the" and a bare "-" exclude nothing: the query they make is empty,
        # and an empty query matches no note
        args["minus"] = minus
        excluded = """
            AND (numnode(websearch_to_tsquery('english', %(minus)s)) = 0
                 OR n.fts @@ websearch_to_tsquery('english', %(minus)s))
        """
    distance, vectors = "NULL::float8", ""
    if model is not None:
        name, qvec = model
        args.update(model=name, dim=len(qvec), vec=Vector(qvec))
        distance = "e.vec <=> %(vec)s::vector"
        vectors = (
            "LEFT JOIN embedding e ON e.node_id = n.id"
            " AND e.model = %(model)s AND e.dim = %(dim)s"
        )
    rows = conn.execute(
        f"""
        WITH c AS MATERIALIZED (
            SELECT n.id, n.kind, n.author, n.author = %(person)s AS own,
                   n.id = ANY(%(keep)s) AS kept,
                   ARRAY[{has}] AS has, {distance} AS d
            FROM node n {vectors}
            WHERE n.fts @@ ({any_word}) AND {where}
              AND n.id <> ALL(%(taken)s)
              AND NOT (%(shipped)s = ANY(n.tags))
              AND (n.id = ANY(%(keep)s) OR n.kind <> 'instruction'
                   OR %(kind)s::text = 'instruction')
              {excluded}
        ), known AS MATERIALIZED (
            -- the people the viewer has given access to a hub
            SELECT DISTINCT g.grantee AS person
            FROM hub_grant g
            WHERE g.granted_by = %(person)s AND g.grantee <> %(person)s
              AND g.mode IN ('read', 'add')
        ), filed AS (
            -- where the candidates stand, as the viewer may see it
            SELECT c.id, m.hub_node_id AS hub
            FROM c JOIN membership m ON m.node_id = c.id
            WHERE NOT m.withdrawn
              AND (m.audience = 'hub' OR m.author = %(person)s)
        ), mine AS MATERIALIZED (
            -- of those hubs, and of the candidates that are hubs: the ones
            -- the viewer has filed something into, unless anyone may file
            -- there
            SELECT a.id, a.author = %(person)s AS own
            FROM (SELECT hub AS id FROM filed
                  UNION SELECT c.id FROM c WHERE c.kind = 'hub') h
            JOIN node a ON a.id = h.id
            WHERE a.payload->>'accept' IS DISTINCT FROM 'anyone'
              AND EXISTS (
                SELECT 1 FROM membership w
                WHERE w.hub_node_id = a.id AND NOT w.withdrawn
                  AND w.author = %(person)s)
        ), used AS (
            -- and can still read: that a note stands in a hub the viewer has
            -- lost is not theirs to see
            SELECT m.id FROM mine m
            WHERE m.own OR {readable_hub_sql("m.id")}
        ), few AS (
            -- per word: no more than PARTIAL_COMMON of these notes have it
            SELECT ARRAY[{few}] AS few FROM c
        ), r AS (
            SELECT c.id, c.own, c.kept, c.has, c.d,
                   cardinality(array_positions(c.has, true)) AS n,
                   ({telling}) AS telling,
                   (c.own OR c.author IN (SELECT person FROM known)
                    OR c.id IN (SELECT id FROM used)
                    OR EXISTS (SELECT 1 FROM filed f
                               WHERE f.id = c.id
                                 AND f.hub IN (SELECT id FROM used))) AS near
            FROM c, few
        ), ranked AS (
            SELECT r.*, count(*) FILTER (WHERE r.near) OVER () AS total,
                   row_number() OVER (
                       PARTITION BY r.kept, r.near
                       ORDER BY r.n DESC, r.d ASC NULLS LAST, r.id DESC
                   ) AS place
            FROM r
            WHERE r.kept OR r.telling
        )
        SELECT id, own, kept, has, n, near, d, total FROM ranked
        WHERE CASE WHEN kept THEN near OR place <= %(kept_others)s
                   WHEN near THEN place <= %(fill)s
                   ELSE place <= %(others)s END
        ORDER BY near DESC, n DESC, d ASC NULLS LAST, id DESC
        """,
        args,
        prepare=False,  # planned for these words, see _keyword
    ).fetchall()
    # What the second try found stays: it was shown before. Of what it found
    # outside, the first PARTIAL_KEPT_OTHERS. The rows still to fill go to the
    # notes near the viewer (what the second try found outside takes none of
    # them), and what is then left to at most PARTIAL_OTHERS notes from
    # outside.
    kept = [r for r in rows if r["kept"]]
    spare = max(fill, 0) - sum(1 for r in kept if r["near"])
    near = [r for r in rows if not r["kept"] and r["near"]][: max(spare, 0)]
    spare -= len(near) + sum(1 for r in kept if not r["near"])
    outside = [r for r in rows if not r["kept"] and not r["near"]]
    outside = outside[: max(min(spare, PARTIAL_OTHERS), 0)]
    chosen = {r["id"] for r in (*kept, *near, *outside)}
    shown = [
        Partial(
            r["id"],
            [word for word, there in zip(words, r["has"], strict=True) if there],
            bool(r["near"]),
        )
        for r in rows
        if r["id"] in chosen
    ][:room]
    # ``total``: every note near the viewer that could have been one of them
    if not shown:
        return [], 0
    return shown, rows[0]["total"] - sum(1 for p in shown if p.near)


def list_scope(
    conn: Conn,
    viewer: Account,
    *,
    hub_node_id: int | None = None,
    kind: str | None = None,
    limit: int = 10,
    include_superseded: bool = False,
) -> tuple[list[int], int]:
    """What a search without words lists: everything in scope that the viewer
    may read, newest first; when hubs are asked for, the viewer's own before the
    others. Returns the first ``limit`` ids and how many there are in all."""
    where, params = scope(
        conn,
        viewer,
        hub_node_id=hub_node_id,
        kind=kind,
        include_superseded=include_superseded,
    )
    params = {**params, "me": viewer.person_id, "limit": limit}
    own_first = "(n.author = %(me)s) DESC, " if kind == "hub" else ""
    total = conn.execute(
        f"SELECT count(*) AS n FROM node n WHERE {where}", params
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT n.id FROM node n WHERE {where}
        ORDER BY {own_first}n.created_at DESC, n.id DESC LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return [r["id"] for r in rows], int(total["n"]) if total else 0


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
    only_ids: Sequence[int] | None = None,
) -> Found:
    """Ranked hits (best first) among the nodes ``viewer`` may read, whether
    the vector leg took part, and, when the hits are few, the notes that have
    only some of the query's words (``Found.partial``, see ``_partial``) and
    how many more of those there are (``Found.more``). Hits and partial
    matches together are at most ``limit``.

    ``only_ids``: the search runs inside these node ids only (see
    :func:`scope`); hits, partial matches and ``Found.more`` are all among
    them. For a caller that has its own set, such as the notes changed since
    a day: asking for many matches and dropping those outside the set misses
    what ranks below the cut. How common a word is (``_partial``) is counted
    among the ids, as it is counted inside a hub with ``hub_node_id``."""
    where, params = scope(
        conn,
        viewer,
        hub_node_id=hub_node_id,
        kind=kind,
        include_superseded=include_superseded,
        only_ids=only_ids,
    )
    pool = max(POOL_MIN, limit * 5)
    words, minus = counted_words(conn, query)
    certain = [
        ("identifier", _identifier(conn, query, where, params, pool)),
        ("exact", _exact(conn, query, where, params, pool)),
    ]
    keyword, second_try = _keyword(conn, query, where, params, pool)
    legs: list[tuple[str, list[tuple[int, float]]]] = [
        *certain,
        ("lexical", keyword),
        ("title", _trigram(conn, query, where, params, pool, words)),
    ]
    semantic = SEMANTIC_OFF
    model: tuple[str, list[float]] | None = None
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
            qvec = embed_query(embedder, query)
            with conn.transaction():  # savepoint: trouble here must not sink a search
                index.catch_up(
                    conn,
                    embedder,
                    viewer.id,
                    seconds=None if cheap else index.CATCH_UP_SECONDS,
                )
                hits = _semantic(
                    conn, embedder, query, where, params, pool, keyword_ids, qvec
                )
            legs.append(("semantic", hits))
            semantic = SEMANTIC_ON
            model = (embedder.model, qvec)
    except Exception:
        # No model, a dimension surprise, a full disk: keywords still work.
        log.warning("semantic leg skipped", exc_info=True)
        semantic = SEMANTIC_OFF
    scores, provenance = reciprocal_rank_fusion(legs, k=RRF_K, weights=LEG_WEIGHTS)
    # certain matches first, in their own order; the rest by fused score
    first = list(dict.fromkeys(i for _, rows in certain for i, _ in rows))
    taken = set(first)
    rest = sorted((i for i in scores if i not in taken), key=lambda i: (-scores[i], -i))
    # What only the keyword leg's second try found does not have all the
    # words: it is a partial match, not a hit.
    aside = (
        {i for i in rest if set(provenance[i]) == {"lexical"}} if second_try else set()
    )
    ranked = [i for i in first + rest if i not in aside][:limit]

    def partial_matches(
        model: tuple[str, list[float]] | None,
    ) -> tuple[list[Partial], int]:
        return _partial(
            conn,
            where,
            params,
            words,
            minus,
            taken=ranked,
            keep=[i for i in rest if i in aside],
            fill=min(PARTIAL_FILL, limit) - len(ranked),
            room=limit - len(ranked),
            model=model,
        )

    if model is None:
        partial, more = partial_matches(None)
    else:
        try:
            with conn.transaction():  # savepoint, as for the vector leg
                partial, more = partial_matches(model)
        except Exception:
            # the vectors gave trouble: order by the words alone
            log.warning("partial matches without distances", exc_info=True)
            partial, more = partial_matches(None)
    return Found(
        [Hit(i, scores[i], provenance[i]) for i in ranked], semantic, partial, more
    )
