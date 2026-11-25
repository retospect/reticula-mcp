"""``reticula-mcp reindex``: rebuild the local embedding index."""

from __future__ import annotations

import argparse
from typing import Any

from reticula_mcp.config import Settings


def register(sub: Any) -> None:
    p = sub.add_parser(
        "reindex",
        help="rebuild the local embedding index (changes no shared data)",
    )
    p.add_argument(
        "--drop",
        action="store_true",
        help="empty the index first (every model), then rebuild",
    )
    p.set_defaults(func=run)


def run(settings: Settings, args: argparse.Namespace) -> int:
    from reticula_mcp.db import connect
    from reticula_mcp.search import index
    from reticula_mcp.search.embedder import get_embedder

    embedder = get_embedder(settings)
    with connect(settings.require_database_url()) as conn:
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
