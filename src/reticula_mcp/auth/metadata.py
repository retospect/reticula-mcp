"""Discovery documents (RFC 8414 authorization server, RFC 9728 protected resource).

Every URL is built from ``Settings.base_url``, never from the request's Host
header: behind a tunnel the app sees plain HTTP on loopback.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.routes import cors_middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from reticula_mcp.config import Settings

CACHE = {"Cache-Control": "public, max-age=300"}


def authorization_server_metadata(settings: Settings) -> dict[str, Any]:
    base = settings.base_url
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
    }


def protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "resource": settings.resource_url,
        "authorization_servers": [settings.base_url],
        "bearer_methods_supported": ["header"],
        "resource_name": "Reticula",
    }


def routes(settings: Settings) -> list[Route]:
    def doc(build: Any) -> Any:
        async def handler(request: Request) -> Response:
            return JSONResponse(build(settings), headers=CACHE)

        return cors_middleware(handler, ["GET", "OPTIONS"])

    resource_path = urlsplit(settings.resource_url).path.rstrip("/")
    prm_paths = ["/.well-known/oauth-protected-resource"]
    if resource_path:
        prm_paths.append(f"/.well-known/oauth-protected-resource{resource_path}")
    found = [
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=doc(authorization_server_metadata),
            methods=["GET", "OPTIONS"],
        )
    ]
    found += [
        Route(
            path, endpoint=doc(protected_resource_metadata), methods=["GET", "OPTIONS"]
        )
        for path in prm_paths
    ]
    return found
