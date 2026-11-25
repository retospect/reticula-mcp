"""CLI subcommands of the content lane: ``debug-notes``."""

from __future__ import annotations

import argparse
from typing import Any

from reticula_mcp.config import Settings


def register(sub: Any) -> None:
    parser = sub.add_parser(
        "debug-notes",
        help="print the newest debug notes that assistants wrote",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="how many notes to print (default 20)"
    )
    parser.set_defaults(func=run_debug_notes)


def run_debug_notes(settings: Settings, args: argparse.Namespace) -> int:
    from reticula_mcp.content.debug import format_debug_notes, list_debug_notes
    from reticula_mcp.db import connect

    with connect(settings.require_database_url()) as conn:
        notes = list_debug_notes(conn, args.limit)
    print(format_debug_notes(notes))
    return 0
