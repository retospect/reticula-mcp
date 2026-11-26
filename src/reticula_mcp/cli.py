"""``reticula-mcp`` command line: serve, migrate, token, account create."""

from __future__ import annotations

import argparse
import getpass
import importlib
import logging
import os
import sys
import time
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


def _timed() -> logging.Formatter:
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime
    return formatter


def _request_log_to_stdout() -> None:
    """The request lines of ``requestlog`` go to standard output, one per line
    with the time in UTC, whatever else configures logging."""
    requests = logging.getLogger("reticula_mcp.request")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_timed())
    requests.addHandler(handler)
    requests.setLevel(logging.INFO)
    requests.propagate = False


def _time_on_other_lines() -> None:
    """The lines that reach the root logger get the same time in front. The MCP
    package sets that logger up with the bare message, so its line about a
    refused ``Host`` header could not be put beside the request it belongs to.
    Only plain stream handlers: one that formats by itself is left alone."""
    for handler in logging.getLogger().handlers:
        if type(handler) is logging.StreamHandler:
            handler.setFormatter(_timed())


def _serve(settings: Settings, auto_migrate: bool) -> int:
    import uvicorn

    from reticula_mcp.auth import refuse_unset_proxy_trust
    from reticula_mcp.db import connect
    from reticula_mcp.db.migrate import pending
    from reticula_mcp.server import build_app

    refuse_unset_proxy_trust(settings)  # before anything is migrated or listened on
    if auto_migrate:
        _migrate(settings)
    with connect(settings.require_database_url()) as conn:
        if not auto_migrate:
            _check_schema_age(settings, conn)
        todo = pending(conn)
        if not todo:
            from reticula_mcp import keystore

            keystore.check_database(conn)
    if todo:
        raise ReticulaError(
            "BadInput",
            f"{len(todo)} migration(s) pending (e.g. {todo[0].path.name})",
            hint="run `reticula-mcp migrate` or start with `serve --migrate`",
        )
    # One line per request comes from requestlog: the caller behind the proxy,
    # the client program, the reason for a refusal, and no query string.
    # uvicorn's own access line (switched off below) has the proxy as the caller.
    _request_log_to_stdout()
    # The SDK logs every request at INFO and httpx every model download request.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # proxy_headers off: uvicorn would otherwise believe X-Forwarded-For from any
    # loopback peer. Whose header is believed is RETICULA_TRUSTED_PROXIES' job.
    app = build_app(settings)
    _time_on_other_lines()  # after build_app: the MCP package configures logging there
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
        access_log=False,
    )
    return 0


#: Days an operator token lasts unless --ttl-days or --no-expiry says otherwise.
TOKEN_DAYS = 90


def _token(
    settings: Settings,
    email: str,
    label: str,
    ttl_days: int | None,
    no_expiry: bool = False,
) -> int:
    from reticula_mcp import accounts
    from reticula_mcp.db import connect

    if no_expiry and ttl_days is not None:
        raise ReticulaError(BAD_INPUT, "give --ttl-days or --no-expiry, not both")
    if ttl_days is not None and ttl_days <= 0:
        raise ReticulaError(
            BAD_INPUT,
            "--ttl-days must be at least 1",
            hint="--no-expiry for a token that does not expire",
        )
    if ttl_days is None and not no_expiry:
        ttl_days = TOKEN_DAYS
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


def _account_disable(settings: Settings, email: str) -> int:
    from reticula_mcp import accounts
    from reticula_mcp.auth import grants
    from reticula_mcp.db import connect

    with connect(settings.require_database_url()) as conn:
        account = accounts.get_account_by_email(conn, email)
        if account is None:
            raise ReticulaError("NotFound", f"no account with e-mail {email!r}")
        if account.status == "system":
            raise ReticulaError(BAD_INPUT, "the instance's own account stays enabled")
        accounts.set_status(conn, account, "disabled")
        n_grants, n_tokens = grants.revoke_everything(conn, account.id)
        conn.commit()
    print(
        f"disabled @{account.handle}; revoked {n_grants} connections and"
        f" {n_tokens} tokens"
    )
    return 0


def _read_new_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != password:
            raise ReticulaError("BadInput", "passwords differ")
        return password
    return sys.stdin.readline().rstrip("\n")


def _account_passwd(settings: Settings, email: str) -> int:
    from reticula_mcp import accounts
    from reticula_mcp.auth import grants
    from reticula_mcp.db import connect

    password = _read_new_password()
    with connect(settings.require_database_url()) as conn:
        account = accounts.get_account_by_email(conn, email)
        if account is None:
            raise ReticulaError("NotFound", f"no account with e-mail {email!r}")
        accounts.set_password(conn, account, password)
        n_grants, n_tokens = grants.revoke_everything(conn, account.id)
        conn.commit()
    print(
        f"new password for @{account.handle}; revoked {n_grants} connections and"
        f" {n_tokens} tokens: sign in again"
    )
    return 0


def _key_init(path: str) -> int:
    from reticula_mcp import keystore

    try:
        keystore.init_file(path)
    except FileExistsError as exc:
        raise ReticulaError(
            BAD_INPUT, f"{path} exists: a secret is never overwritten"
        ) from exc
    print(f"wrote a new secret to {path} (mode 600); set RETICULA_KEY_FILE={path}")
    return 0


def _key_command(settings: Settings, args: Any) -> int:
    from reticula_mcp import keystore
    from reticula_mcp.db import connect

    with connect(settings.require_database_url()) as conn:
        if args.key_command == "encrypt":
            counts = keystore.encrypt_all(conn)
            keystore.check_database(conn)  # nothing plaintext is left
            verb = "encrypted"
        else:
            counts = keystore.rotate_all(conn, keystore.read_secret(args.new_file))
            verb = "re-encrypted"
    print(
        f"{verb} {counts.encrypted} keys of {counts.accounts} accounts;"
        f" {counts.already} were done already"
    )
    if args.key_command == "rotate":
        print(f"now set RETICULA_KEY_FILE={args.new_file} and start the server")
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
    token.add_argument(
        "--ttl-days", type=int, help=f"days until it expires (default {TOKEN_DAYS})"
    )
    token.add_argument(
        "--no-expiry", action="store_true", help="a token that never expires"
    )

    account = sub.add_parser("account", help="account administration")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    create = account_sub.add_parser("create", help="create an account")
    create.add_argument("--email", required=True)
    create.add_argument("--handle")
    create.add_argument(
        "--password", help="omit to be prompted (or to read one line from stdin)"
    )
    disable = account_sub.add_parser(
        "disable", help="disable an account and revoke all its connections and tokens"
    )
    disable.add_argument("--email", required=True)
    passwd = account_sub.add_parser(
        "passwd",
        help="set a new password (prompted) and revoke all connections and tokens",
    )
    passwd.add_argument("--email", required=True)
    key = sub.add_parser("key", help="the secret that encrypts private keys")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    init = key_sub.add_parser(
        "init", help="write a new secret to a file (never printed)"
    )
    init.add_argument("--file", required=True)
    key_sub.add_parser(
        "encrypt", help="encrypt every stored private key (server stopped; safe twice)"
    )
    rotate = key_sub.add_parser(
        "rotate", help="re-encrypt every key with a new secret (server stopped)"
    )
    rotate.add_argument("--new-file", required=True)
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
            from reticula_mcp import keystore

            keystore.configure(settings.key_file)
            return _serve(settings, args.migrate)
        if args.command == "migrate":
            from reticula_mcp import keystore

            keystore.configure(settings.key_file)
            return _migrate(settings)
        if args.command == "key" and args.key_command == "init":
            return _key_init(args.file)
        # Every other command may write a key, or sign: the secret first.
        from reticula_mcp import keystore

        keystore.configure(settings.key_file)
        # Every other command works on the tables as this version knows them.
        _refuse_newer_schema(settings)
        func = getattr(args, "func", None)
        if func is not None:
            return int(func(settings, args))
        if args.command == "key":
            return _key_command(settings, args)
        if args.command == "token":
            return _token(
                settings, args.email, args.label, args.ttl_days, args.no_expiry
            )
        if args.command == "account" and args.account_command == "disable":
            return _account_disable(settings, args.email)
        if args.command == "account" and args.account_command == "passwd":
            return _account_passwd(settings, args.email)
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
