"""``reticula-mcp`` command line: serve, migrate, token, account create."""

from __future__ import annotations

import argparse
import getpass
import importlib
import logging
import os
import sys
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import psycopg

from reticula_mcp import __version__
from reticula_mcp.config import Settings
from reticula_mcp.errors import BAD_INPUT, UNAVAILABLE, ReticulaError


def _check_schema_age(settings: Settings, conn: Any) -> None:
    """Refuse a database that a newer version has used, or say that the
    operator chose to run on it."""
    from reticula_mcp.db.migrate import newer_schema_message, unknown

    newer = unknown(conn)
    if not newer:
        return
    if not settings.allow_newer_schema:
        raise ReticulaError(
            BAD_INPUT,
            newer_schema_message(newer),
            hint="start that newer version again, or restore a dump taken before"
            " the upgrade. To run this version on the newer schema anyway, set"
            " RETICULA_ALLOW_NEWER_SCHEMA=1 (docs/operating.md, Upgrade)",
        )
    print(
        f"warning: {newer_schema_message(newer)}; running on the newer schema"
        " because RETICULA_ALLOW_NEWER_SCHEMA is set",
        file=sys.stderr,
    )


def _refuse_newer_schema(settings: Settings) -> None:
    """The check of ``_check_schema_age`` for a command that does not migrate
    and does not serve: such a command reads and writes the same tables."""
    from reticula_mcp.db import connect

    with connect(settings.require_database_url()) as conn:
        _check_schema_age(settings, conn)


def _migrate(settings: Settings) -> int:
    from reticula_mcp import accounts
    from reticula_mcp.content import hooks
    from reticula_mcp.db import connect
    from reticula_mcp.db.migrate import migrate
    from reticula_mcp.db.pool import configure_connection

    with connect(settings.require_database_url()) as conn:
        _check_schema_age(settings, conn)
        ran = migrate(conn, allow_newer=settings.allow_newer_schema)
        # On a new database the vector type did not exist when this connection
        # was opened, so its adapter is registered only now.
        conn.commit()
        configure_connection(conn)
        for m in ran:
            print(f"applied {m.path.name}")
        if not ran:
            print("schema is up to date")
        accounts.ensure_system_account(conn)
        hooks.on_migrated(conn)
        conn.commit()
    return 0


class _NoQueryString(logging.Filter):
    """Access log lines without the query string.

    ``/signin?req=...`` and ``/authorize?...&state=...`` carry values that must
    not be kept in a log. uvicorn passes (peer, method, path with query, HTTP
    version, status) as the arguments of an access record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].partition("?")[0], *args[3:])
        return True


def _serve(settings: Settings, auto_migrate: bool) -> int:
    import uvicorn

    from reticula_mcp.db import connect
    from reticula_mcp.db.migrate import pending
    from reticula_mcp.server import build_app

    if auto_migrate:
        _migrate(settings)
    with connect(settings.require_database_url()) as conn:
        if not auto_migrate:
            _check_schema_age(settings, conn)
        todo = pending(conn)
    if todo:
        raise ReticulaError(
            "BadInput",
            f"{len(todo)} migration(s) pending (e.g. {todo[0].path.name})",
            hint="run `reticula-mcp migrate` or start with `serve --migrate`",
        )
    # Added before uvicorn configures logging: that replaces the logger's
    # handlers and keeps its filters.
    logging.getLogger("uvicorn.access").addFilter(_NoQueryString())
    # The SDK logs every request at INFO and httpx every model download request.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # proxy_headers off: uvicorn would otherwise believe X-Forwarded-For from any
    # loopback peer. Whose header is believed is RETICULA_TRUSTED_PROXIES' job.
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
    )
    return 0


def _token(settings: Settings, email: str, label: str, ttl_days: int | None) -> int:
    from reticula_mcp import accounts
    from reticula_mcp.db import connect

    if ttl_days is not None and ttl_days <= 0:
        raise ReticulaError(
            BAD_INPUT,
            "--ttl-days must be at least 1",
            hint="leave it out for a token that does not expire",
        )
    with connect(settings.require_database_url()) as conn:
        account = accounts.get_account_by_email(conn, email)
        if account is None:
            raise ReticulaError("NotFound", f"no account with e-mail {email!r}")
        ttl = timedelta(days=ttl_days) if ttl_days is not None else None
        token = accounts.mint_token(conn, account, label, ttl)
        conn.commit()
    print(token)
    return 0


def _account_create(
    settings: Settings, email: str, password: str | None, handle: str | None
) -> int:
    from reticula_mcp import accounts
    from reticula_mcp.db import connect

    if password is None:
        if sys.stdin.isatty():
            password = getpass.getpass("Password: ")
            if getpass.getpass("Again: ") != password:
                raise ReticulaError("BadInput", "passwords differ")
        else:
            password = sys.stdin.readline().rstrip("\n")
    with connect(settings.require_database_url()) as conn:
        account = accounts.create_account(conn, email, password, handle)
        conn.commit()
    print(f"created @{account.handle} ({account.person_id})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reticula-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server (streamable HTTP)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--migrate", action="store_true", help="apply migrations first")

    sub.add_parser("migrate", help="apply pending database migrations")

    token = sub.add_parser("token", help="mint a bearer token for an account")
    token.add_argument("--email", required=True)
    token.add_argument("--label", default="cli")
    token.add_argument("--ttl-days", type=int)

    account = sub.add_parser("account", help="account administration")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    create = account_sub.add_parser("create", help="create an account")
    create.add_argument("--email", required=True)
    create.add_argument("--handle")
    create.add_argument(
        "--password", help="omit to be prompted (or to read one line from stdin)"
    )
    _register_lane_commands(sub)
    return parser


_LANES = ("auth", "graph", "search", "content")


def _register_lane_commands(sub: Any) -> None:
    """Let each lane add subcommands from its own ``<lane>/cli.py``.

    A lane module defines ``register(sub)``: it adds parsers to ``sub`` and
    gives each one ``set_defaults(func=run)`` where ``run(settings, args) -> int``.
    """
    for lane in _LANES:
        name = f"reticula_mcp.{lane}.cli"
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            continue
        module.register(sub)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        env = dict(os.environ)
        if args.command == "serve":
            # Before validation, so an error names the address really used.
            if args.host:
                env["RETICULA_HOST"] = args.host
            if args.port:
                env["RETICULA_PORT"] = str(args.port)
        settings = Settings.from_env(env)
        if args.command == "serve":
            return _serve(settings, args.migrate)
        if args.command == "migrate":
            return _migrate(settings)
        # Every other command works on the tables as this version knows them.
        _refuse_newer_schema(settings)
        func = getattr(args, "func", None)
        if func is not None:
            return int(func(settings, args))
        if args.command == "token":
            return _token(settings, args.email, args.label, args.ttl_days)
        if args.command == "account":
            return _account_create(settings, args.email, args.password, args.handle)
    except ReticulaError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else "no answer"
        print(
            f"[error:{UNAVAILABLE}] cannot reach the database: {reason}\n"
            "hint: check RETICULA_DATABASE_URL and that PostgreSQL is running",
            file=sys.stderr,
        )
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
