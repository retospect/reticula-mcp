"""Hooks the foundation calls: ``on_account_created`` and ``on_migrated``.

Both write through the graph API (``reticula_mcp.graph.api``, AGENTS.md seam
10). That module is imported lazily when a hook runs. While it does not exist
the hooks do nothing, so no wiring edit is needed when the graph lane lands.
Tests (and any other caller) can set :data:`binding` to a different
:class:`~reticula_mcp.content.bootstrap.GraphWriter`.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from reticula_mcp.accounts import Account
from reticula_mcp.content import bootstrap
from reticula_mcp.db.pool import Conn

log = logging.getLogger("reticula_mcp.content")

GRAPH_API_MODULE = "reticula_mcp.graph.api"

#: Overrides the lazy import when set.
binding: bootstrap.GraphWriter | None = None


def _import_graph_api() -> Any:
    """The graph API module, or None while the graph lane has not landed."""
    try:
        return importlib.import_module(GRAPH_API_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != GRAPH_API_MODULE:
            raise
        return None


def graph_writer() -> bootstrap.GraphWriter | None:
    """The writer the hooks use now, or None (then the hooks do nothing)."""
    if binding is not None:
        return binding
    api: bootstrap.GraphWriter | None = _import_graph_api()
    return api


def on_account_created(conn: Conn, account: Account) -> None:
    """Give a new account read access to the instructions hub.

    Called inside ``accounts.create_account``'s transaction, after the account,
    its keys and its identity record exist. Does nothing when there is no
    graph API yet, no system account, or no instructions hub (``migrate`` has
    not seeded it). A failure here is logged and never blocks the sign-up;
    the next ``migrate`` grants access to anyone who was missed.
    """
    api = graph_writer()
    if api is None:
        return
    system = bootstrap.system_account(conn)
    if system is None:
        return
    hub = bootstrap.find_shipped_node(conn, system, bootstrap.INSTRUCTIONS_HUB_SLUG)
    if hub is None:
        return
    try:
        with conn.transaction():
            api.set_grant(conn, system, hub["id"], account.person_id, "read")
    except Exception:
        log.exception(
            "could not give %s read access to the instructions hub", account.handle
        )


def on_migrated(conn: Conn) -> None:
    """Seed the shipped instructions and hubs; idempotent.

    Called by ``reticula-mcp migrate`` and ``serve --migrate`` after the
    migrations ran, inside a transaction the caller commits.
    """
    api = graph_writer()
    if api is None:
        return
    result = bootstrap.ensure_shipped_content(conn, api)
    log.info(
        "shipped content: %d created, %d updated, %d unchanged, %d granted",
        len(result.created),
        len(result.updated),
        len(result.unchanged),
        len(result.granted),
    )
