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
from reticula_mcp.errors import BAD_INPUT, NOT_FOUND, ReticulaError


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
        tokens = grants.list_operator_tokens(conn, account.id)
    if not rows:
        print(f"@{account.handle} has no connected assistants")
    else:
        print(f"@{account.handle}: {len(rows)} connection(s)")
        # client: what the request log prints as client= for this connection
        print("id  status   created           last used         client      assistant")
        for c in rows:
            status = "live" if c.live else "revoked"
            print(
                f"{c.id:<3} {status:<8} {_when(c.created_at):<17} "
                f"{_when(c.last_used_at):<17} {c.client_id[:10]:<11} {c.label}"
            )
    if tokens:
        print()
        print(f"operator tokens ({len(tokens)}), minted with `reticula-mcp token`")
        print(
            "id  status   created           last used         expires           label"
        )
        for t in tokens:
            status = "revoked" if t.revoked_at else "live"
            print(
                f"{t.id:<3} {status:<8} {_when(t.created_at):<17} "
                f"{_when(t.last_used_at):<17} {_when(t.expires_at):<17} {t.label or '-'}"
            )
    print("next: reticula-mcp connections revoke --email ... --id N | --token N")
    return 0


def run_revoke(settings: Settings, args: argparse.Namespace) -> int:
    if (args.id is None) == (args.token is None):
        raise ReticulaError(
            BAD_INPUT,
            "give exactly one of --id (connection) or --token (operator token)",
        )
    with connect(settings.require_database_url()) as conn:
        account = _account(conn, args.email)
        if args.id is not None:
            connection = grants.revoke_connection(conn, account.id, args.id)
            message = f"revoked connection {connection.id} ({connection.label})"
        else:
            token = grants.revoke_operator_token(conn, account.id, args.token)
            message = f"revoked operator token {token.id} ({token.label or '-'})"
        conn.commit()
    print(message)
    return 0


def register(sub: Any) -> None:
    parser = sub.add_parser(
        "connections",
        help="list or revoke the assistants and operator tokens of an account",
    )
    inner = parser.add_subparsers(dest="connections_command", required=True)
    listing = inner.add_parser(
        "list", help="show an account's connections and operator tokens"
    )
    listing.add_argument("--email", required=True)
    listing.set_defaults(func=run_list)
    revoke = inner.add_parser("revoke", help="revoke one connection or operator token")
    revoke.add_argument("--email", required=True)
    revoke.add_argument("--id", type=int, help="connection id from `list`")
    revoke.add_argument("--token", type=int, help="operator token id from `list`")
    revoke.set_defaults(func=run_revoke)
