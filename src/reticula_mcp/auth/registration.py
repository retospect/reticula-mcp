"""Dynamic client registration (RFC 7591) and the revocation endpoint (RFC 7009).

Both are served here instead of by the SDK handlers: the SDK's registration
accepts anything (no redirect-URI policy, no per-address cap) and its
revocation handler demands a ``client_secret`` field that public clients do not
have.

Only public clients exist (``token_endpoint_auth_method: none``, PKCE is what
protects the code); a client that asks for a secret-based method is told it is
registered as ``none`` (RFC 7591 section 3.2.1 lets the server substitute).
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread
from mcp.server.auth.routes import cors_middleware
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from reticula_mcp.auth import config, grants, throttle
from reticula_mcp.auth.netaddr import caller_bucket
from reticula_mcp.auth.provider import ReticulaProvider
from reticula_mcp.auth.web import NO_STORE, json_error, read_limited
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Database

_LOOPBACK_NAMES = frozenset({"localhost"})
_SPACE_OR_CONTROL = re.compile(r"[\x00-\x20\x7f-\x9f\\]")


def validate_redirect_uri(uri: Any, settings: Settings | None = None) -> str:
    """The URI, or ValueError saying why it is refused.

    https, or http on a loopback host (localhost, 127.0.0.0/8, ::1) for native
    clients. No userinfo, no fragment, no whitespace/control characters or
    backslashes (browsers treat ``\\`` as ``/``), bounded length.
    """
    if not isinstance(uri, str) or not uri or len(uri) > config.MAX_URI_LENGTH:
        raise ValueError("redirect URI missing or too long")
    if _SPACE_OR_CONTROL.search(uri):
        raise ValueError("redirect URI contains forbidden characters")
    parts = urlsplit(uri)
    if parts.fragment or "#" in uri:
        raise ValueError("redirect URI must not have a fragment")
    host = parts.hostname
    if not host or "@" in parts.netloc:
        raise ValueError("redirect URI needs a host and no credentials")
    try:
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as exc:
        raise ValueError("redirect URI has an invalid port") from exc
    if parts.scheme in ("https", "http") and _is_loopback(host):
        return uri
    if parts.scheme == "https":
        if settings is not None and not config.host_allowed(settings, host):
            raise ValueError("redirect URI host is not a known client of this server")
        return uri
    raise ValueError("redirect URI must be https (http only for localhost)")


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def clean_name(name: Any) -> str:
    """A client name safe to show: no control or format (bidi) characters."""
    if not isinstance(name, str):
        return "an assistant"
    kept = "".join(
        ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")
    )
    kept = " ".join(kept.split())[: config.CLIENT_NAME_LENGTH]
    return kept or "an assistant"


def build_client(
    body: dict[str, Any], settings: Settings | None = None
) -> OAuthClientInformationFull:
    """Validate a registration document; ValueError (message) when refused."""
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= config.MAX_REDIRECT_URIS:
        raise ValueError(f"redirect_uris must list 1-{config.MAX_REDIRECT_URIS} URIs")
    checked = [validate_redirect_uri(u, settings) for u in uris]
    grant_types = body.get("grant_types", ["authorization_code", "refresh_token"])
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        raise ValueError("grant_types must include authorization_code")
    response_types = body.get("response_types", ["code"])
    if not isinstance(response_types, list) or "code" not in response_types:
        raise ValueError("response_types must include code")
    method = body.get("token_endpoint_auth_method", "none")
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        raise ValueError("unsupported token_endpoint_auth_method")
    return OAuthClientInformationFull(
        client_id="c_" + secrets.token_urlsafe(18),
        client_id_issued_at=int(time.time()),
        redirect_uris=[AnyUrl(u) for u in checked],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=clean_name(body.get("client_name")),
        scope=config.SCOPE,
    )


def routes(settings: Settings, db: Database, provider: ReticulaProvider) -> list[Route]:
    limit, window = config.register_rate(settings)

    def allow_sync(bucket: str) -> bool:
        with db.connection() as conn:
            return throttle.acquire(conn, throttle.REGISTER_IP, bucket, limit, window)

    def register_sync(body: dict[str, Any]) -> Response:
        try:
            client = build_client(body, settings)
        except ValueError as exc:
            text = str(exc)
            kind = (
                "invalid_redirect_uri"
                if "redirect" in text
                else "invalid_client_metadata"
            )
            return json_error(400, kind, text)
        with db.connection() as conn:
            grants.purge_stale(conn)
            throttle.purge(conn)
            provider.store_client(conn, client)
        return JSONResponse(
            client.model_dump(mode="json", exclude_none=True),
            status_code=201,
            headers=NO_STORE,
        )

    async def register(request: Request) -> Response:
        # Every attempt counts against the address, valid or not, and is
        # counted before the body is read.
        bucket = caller_bucket(request, settings)
        if not await anyio.to_thread.run_sync(allow_sync, bucket):
            return json_error(
                429,
                "temporarily_unavailable",
                "too many registrations from this address; try again later",
                {"Retry-After": str(window)},
            )
        raw = await read_limited(request, config.MAX_REGISTER_BODY)
        if raw is None:
            return json_error(413, "invalid_client_metadata", "registration too large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return json_error(400, "invalid_client_metadata", "body must be JSON")
        if not isinstance(body, dict):
            return json_error(400, "invalid_client_metadata", "body must be an object")
        return await anyio.to_thread.run_sync(register_sync, body)

    async def revoke(request: Request) -> Response:
        raw = await read_limited(request, config.MAX_FORM_BODY)
        if raw is None:
            return json_error(413, "invalid_request", "request too large")
        from urllib.parse import parse_qs

        form = {
            k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items() if v
        }
        token, client_id = form.get("token"), form.get("client_id")
        if not token or not client_id:
            return json_error(400, "invalid_request", "token and client_id required")

        def work() -> bool:
            if provider.client_sync(client_id) is None:
                return False
            provider.revoke_by_token(client_id, token)
            return True

        if not await anyio.to_thread.run_sync(work):
            return json_error(401, "unauthorized_client", "unknown client")
        # RFC 7009: 200 whether or not the token existed.
        return Response(status_code=200, headers=NO_STORE)

    return [
        Route(
            "/register",
            endpoint=cors_middleware(register, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            "/revoke",
            endpoint=cors_middleware(revoke, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
    ]
