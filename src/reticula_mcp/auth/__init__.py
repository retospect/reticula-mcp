"""OWNED BY the sign-in lane. Foundation ships the resource-server stub.

Contract (frozen): ``build_auth(settings, db) -> AuthWiring``. ``server.py``
calls it once at start-up and never looks inside. See ``seams.AuthWiring`` for
what may be returned and AGENTS.md for the rules (the principal's
``AccessToken.subject`` is ``str(account.id)``).

Stub behaviour: bearer tokens minted by ``accounts.mint_token`` are verified
against ``oauth_token``; there are no OAuth endpoints. The sign-in lane
replaces this module with a full authorization server (dynamic client
registration, PKCE, one sign-in/sign-up page) without touching ``server.py``.
"""

from __future__ import annotations

import anyio.to_thread
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from reticula_mcp import accounts
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Database
from reticula_mcp.seams import AuthWiring

__all__ = ["AuthWiring", "build_auth"]

DEFAULT_CLIENT_ID = "reticula-token"


class DbTokenVerifier:
    """``TokenVerifier`` over ``oauth_token`` (async; DB work runs in a thread)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self._verify, token)

    def _verify(self, token: str) -> AccessToken | None:
        with self._db.connection() as conn:
            found = accounts.lookup_token(conn, token)
        if found is None:
            return None
        account, info = found
        return AccessToken(
            token="",  # never keep the raw token around
            client_id=info.client_id or DEFAULT_CLIENT_ID,
            scopes=list(info.scopes),
            expires_at=int(info.expires_at.timestamp()) if info.expires_at else None,
            resource=info.resource,
            subject=str(account.id),
            claims={"grant_id": info.grant_id, "label": info.label},
        )


def build_auth(settings: Settings, db: Database) -> AuthWiring:
    return AuthWiring(
        settings=AuthSettings(
            issuer_url=AnyHttpUrl(settings.base_url),
            resource_server_url=AnyHttpUrl(settings.resource_url),
            validate_token_resource=False,
        ),
        token_verifier=DbTokenVerifier(db),
    )
