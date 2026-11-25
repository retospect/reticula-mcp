"""No-op hooks the content lane fills in. Called by foundation code."""

from __future__ import annotations

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn


def on_account_created(conn: Conn, account: Account) -> None:
    """Called inside ``accounts.create_account``'s transaction, after the
    account, its keys and its identity record exist. Not called for the system
    account. Example use: join the new account to the shipped-instructions hub."""


def on_migrated(conn: Conn) -> None:
    """Called by ``reticula-mcp migrate`` after migrations succeed (and by
    ``serve --migrate``), in a transaction the caller commits. Must be
    idempotent: runs on every migrate. Example use: seed shipped instruction
    atoms signed by the system account."""
