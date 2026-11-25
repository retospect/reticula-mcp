"""``reticula-mcp rebuild``: replay the record log into the graph tables."""

from __future__ import annotations

import argparse
from typing import Any

from reticula_mcp.config import Settings


def register(sub: Any) -> None:
    parser = sub.add_parser(
        "rebuild",
        help="rebuild the graph tables (nodes, memberships, grants) from the record log",
    )
    parser.set_defaults(func=run)


def run(settings: Settings, args: argparse.Namespace) -> int:
    from reticula_mcp.db import connect
    from reticula_mcp.graph.materialize import rebuild

    with connect(settings.require_database_url()) as conn:
        counts = rebuild(conn)
        conn.commit()
    print("replayed " + ", ".join(f"{n} {t}" for t, n in counts.items()))
    return 0
