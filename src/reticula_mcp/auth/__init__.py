"""OWNED BY the sign-in lane: the OAuth 2.1 authorization server and its one page.

Contract (frozen): ``build_auth(settings, db) -> AuthWiring``. ``server.py``
calls it once at start-up and never looks inside.

Wiring (SDK-native, see AGENTS.md seam 1): ``settings`` + ``provider``. FastMCP
then verifies bearer tokens through the provider, answers unauthenticated MCP
calls with ``401`` and ``WWW-Authenticate: Bearer ... resource_metadata=...``,
and serves ``/authorize`` and ``/token`` (SDK handlers) and the protected-
resource document at the path-qualified well-known URL. Everything else is ours
and goes first in ``routes`` (so it wins over any SDK route of the same path):

    /.well-known/oauth-authorization-server    discovery (advertises auth method none)
    /.well-known/oauth-protected-resource[/mcp] discovery
    /authorize                                 SDK handler, wrapped (caller address)
    /register                                  dynamic client registration, public clients
    /revoke                                    RFC 7009 for public clients
    /signin                                    the one sign-in / create-account page

Bearer tokens minted with ``accounts.mint_token`` (tests, CLI) keep working: the
provider looks every token up with ``accounts.lookup_token``.

Modules: ``provider`` (state machine), ``grants`` (per-assistant grants and
tokens), ``registration`` (/register, /revoke), ``signin`` + ``page`` (the
page), ``metadata``, ``throttle`` + ``netaddr`` (rate limits, client address),
``verification`` (e-mail verification switch, off), ``cli`` (connections).
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from pydantic import AnyHttpUrl

from reticula_mcp.auth import authorize, config, metadata, registration, signin
from reticula_mcp.auth.provider import DEFAULT_CLIENT_ID, ReticulaProvider
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Database
from reticula_mcp.seams import AuthWiring

__all__ = ["DEFAULT_CLIENT_ID", "AuthWiring", "build_auth"]

log = logging.getLogger("reticula_mcp.auth")


def reached_through_a_proxy(settings: Settings) -> bool:
    """True when the public URL says clients do not connect to us directly."""
    parts = urlsplit(settings.base_url)
    if parts.scheme == "https":
        return True
    host = parts.hostname or ""
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def warn_if_proxy_untrusted(settings: Settings) -> bool:
    """Log one loud warning when every client would share one throttle bucket."""
    if settings.trusted_proxies or not reached_through_a_proxy(settings):
        return False
    log.warning(
        "RETICULA_PUBLIC_URL is %s but RETICULA_TRUSTED_PROXIES is empty: behind a"
        " reverse proxy or tunnel every client appears to come from the proxy, so"
        " ALL clients share ONE rate-limit bucket (sign-ups, failed sign-ins,"
        " client registrations, pending authorizations): a handful of requests from"
        " anyone locks everyone else out. Set RETICULA_TRUSTED_PROXIES to the"
        " proxy's address (e.g. 127.0.0.1 for a local tunnel).",
        settings.base_url,
    )
    return True


def build_auth(settings: Settings, db: Database) -> AuthWiring:
    warn_if_proxy_untrusted(settings)
    provider = ReticulaProvider(settings, db)
    routes = [
        *authorize.routes(settings, provider),
        *metadata.routes(settings),
        *registration.routes(settings, db, provider),
        *signin.routes(settings, db, provider),
    ]
    return AuthWiring(
        settings=AuthSettings(
            issuer_url=AnyHttpUrl(settings.base_url),
            resource_server_url=AnyHttpUrl(settings.resource_url),
            # Issued tokens carry the resource they were requested for (RFC
            # 8707); the provider reports the endpoint URL for operator tokens.
            validate_token_resource=True,
            # /register and /revoke are ours (registration.py); keep the SDK's
            # versions off so they cannot be reached by accident.
            client_registration_options=ClientRegistrationOptions(
                enabled=False,
                valid_scopes=[config.SCOPE],
                default_scopes=[config.SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=False),
        ),
        provider=provider,
        routes=routes,
    )
