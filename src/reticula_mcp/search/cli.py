"""``reticula-mcp reindex``: rebuild the local embedding index, or remove the
vectors of models no longer in use."""

from __future__ import annotations

import argparse
from typing import Any

from reticula_mcp.config import Settings


def register(sub: Any) -> None:
    p = sub.add_parser(
        "reindex",
        help="rebuild the local embedding index (changes no shared data)",
    )
    what = p.add_mutually_exclusive_group()
    what.add_argument(
        "--drop",
        action="store_true",
        help="empty the index first (every model), then rebuild",
    )
    what.add_argument(
        "--drop-other-models",
        action="store_true",
        help=(
            "remove the vectors and vector indexes of every model but the"
            " configured one, once it has a vector for every note; embeds nothing"
        ),
    )
    p.set_defaults(func=run)


def run(settings: Settings, args: argparse.Namespace) -> int:
    from reticula_mcp.db import connect
    from reticula_mcp.search import index
    from reticula_mcp.search.embedder import get_embedder

    embedder = get_embedder(settings)
    with connect(settings.require_database_url()) as conn:
        if getattr(args, "drop_other_models", False):
            return drop_other_models(conn, embedder)
        # The vector index first, outside a transaction, so that building it
        # never blocks a running server's writes; then the vectors, committed
        # in steps.
        conn.autocommit = True
        index.ensure_vector_index(conn, embedder)
        conn.autocommit = False
        n = index.reindex(conn, embedder, drop=args.drop, checkpoint=conn.commit)
        conn.commit()
    print(f"embedded {n} nodes with {embedder.model}")
    return 0


def drop_other_models(conn: Any, embedder: Any) -> int:
    """Remove the vectors and indexes of the other models, but only when the
    configured model has caught up: the worker has recorded it as complete
    and no note is left in the backlog. Otherwise nothing is removed and the
    command says why; switching back would still find the old vectors."""
    from reticula_mcp.search import index

    missing = len(index.missing_ids(conn, embedder, None, 1))
    waiting = conn.execute("SELECT count(*) AS n FROM embedding_backlog").fetchone()[
        "n"
    ]
    if not index.is_complete(conn, embedder) or missing or waiting:
        print(
            f"not removed: {embedder.model} does not have a vector for every note"
            f" yet ({waiting} waiting). The server embeds them in the background;"
            " run this again later, or run `reticula-mcp reindex` first."
        )
        return 1
    gone = conn.execute(
        "DELETE FROM embedding WHERE model <> %s", (embedder.model,)
    ).rowcount
    names = index.drop_other_vector_indexes(conn, embedder)
    conn.commit()
    print(
        f"removed {gone} vectors and {len(names)} vector indexes of other models;"
        f" {embedder.model} is kept"
    )
    return 0
