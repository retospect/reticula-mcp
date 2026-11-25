"""Who is calling? Maps an MCP request context to an account. FROZEN."""

from __future__ import annotations

from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from reticula_mcp.accounts import Account, get_account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import FORBIDDEN, ReticulaError


def access_token_from_context(ctx: Any) -> AccessToken | None:
    """The authenticated principal of the HTTP request behind ``ctx``.

    Reads the Starlette scope of the request that carried this tool call (the
    SDK puts the request on ``ctx.request_context``); falls back to the SDK's
    auth contextvar. Must be called on the event-loop side, before handing
    off to a worker thread.
    """
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        request = None
    user = request.scope.get("user") if request is not None else None
    if isinstance(user, AuthenticatedUser):
        return user.access_token
    return get_access_token()


def viewer_for_token(conn: Conn, token: AccessToken | None) -> Account:
    """The :class:`Account` an access token's ``subject`` names."""
    if token is None or not (token.subject or "").isdigit():
        raise ReticulaError(FORBIDDEN, "not authenticated")
    account = get_account(conn, int(token.subject or 0))
    if account is None or account.status == "disabled":
        raise ReticulaError(FORBIDDEN, "account is not available")
    return account
