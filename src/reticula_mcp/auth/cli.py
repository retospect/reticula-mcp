"""``reticula-mcp connections``: list and revoke an account's connected assistants.

reticula-mcp connections list   --email me@example.com
reticula-mcp connections revoke --email me@example.com --id 3
"""

from __future__ import annotations

import argparse
from typing import Any

from reticula_mcp import accounts
from reticula_mcp.auth import grants
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.errors import NOT_FOUND, ReticulaError


def _account(conn: Any, email: str) -> accounts.Account:
    account = accounts.get_account_by_email(conn, email)
    if account is None:
        raise ReticulaError(NOT_FOUND, f"no account with e-mail {email!r}")
    return account


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def run_list(settings: Settings, args: argparse.Namespace) -> int:
    with connect(settings.require_database_url()) as conn:
        account = _account(conn, args.email)
        rows = grants.list_connections(conn, account.id)
    if not rows:
        print(f"@{account.handle} has no connected assistants")
        return 0
    print(f"@{account.handle}: {len(rows)} connection(s)")
    print("id  status   created           last used         assistant")
    for c in rows:
        status = "live" if c.live else "revoked"
        print(
            f"{c.id:<3} {status:<8} {_when(c.created_at):<17} "
            f"{_when(c.last_used_at):<17} {c.label}"
        )
    print("next: reticula-mcp connections revoke --email ... --id N")
    return 0


def run_revoke(settings: Settings, args: argparse.Namespace) -> int:
    with connect(settings.require_database_url()) as conn:
        account = _account(conn, args.email)
        connection = grants.revoke_connection(conn, account.id, args.id)
        conn.commit()
    print(f"revoked connection {connection.id} ({connection.label})")
    return 0


def register(sub: Any) -> None:
    parser = sub.add_parser(
        "connections", help="list or revoke the assistants connected to an account"
    )
    inner = parser.add_subparsers(dest="connections_command", required=True)
    listing = inner.add_parser("list", help="show an account's connections")
    listing.add_argument("--email", required=True)
    listing.set_defaults(func=run_list)
    revoke = inner.add_parser("revoke", help="revoke one connection")
    revoke.add_argument("--email", required=True)
    revoke.add_argument("--id", type=int, required=True, help="id from `list`")
    revoke.set_defaults(func=run_revoke)
