"""Seam types shared between foundation code and lane code. FROZEN."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from starlette.middleware import Middleware
from starlette.routing import BaseRoute


@dataclass(frozen=True)
class AuthWiring:
    """Everything ``server.py`` needs from the auth lane.

    ``reticula_mcp.auth.build_auth(settings, db)`` returns one of these;
    ``server.py`` hands the fields to ``FastMCP(auth=..., token_verifier=...,
    auth_server_provider=...)`` and to the Starlette app. Exactly the SDK's two
    mechanisms are supported, plus an escape hatch:

    * resource server only: set ``settings`` + ``token_verifier``;
    * SDK-native authorization server: set ``settings`` + ``provider`` (the
      SDK then serves /authorize, /token, /register, /revoke and the
      ``.well-known`` documents, and verifies bearer tokens through the
      provider);
    * own bearer middleware: leave both unset and supply ``middleware`` that
      sets ``scope["user"]`` to ``mcp.server.auth.middleware.bearer_auth.
      AuthenticatedUser`` (with ``AccessToken.subject`` = the account id) and
      rejects unauthenticated MCP requests itself.

    ``routes`` (sign-in page, extra OAuth routes) are put ahead of the MCP
    route. ``middleware`` is added to the app. Whatever the mechanism, the
    authenticated principal MUST reach tools as an ``AccessToken`` whose
    ``subject`` is ``str(account.id)`` (decimal account row id); that is how
    ``server.py`` finds the viewer.
    """

    settings: AuthSettings | None = None
    token_verifier: TokenVerifier | None = None
    provider: OAuthAuthorizationServerProvider | None = None
    routes: Sequence[BaseRoute] = ()
    middleware: Sequence[Middleware] = ()

    def protects_mcp(self) -> bool:
        return bool(self.token_verifier or self.provider or self.middleware)
