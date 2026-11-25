"""``/authorize``: the SDK handler, wrapped so the provider knows who is calling.

The SDK parses and validates the request (client, redirect URI, PKCE shape) and
calls ``provider.authorize``, which parks the request. The SDK does not pass the
HTTP request on, so the caller's throttle bucket travels in a context variable
(set here, read by the provider in the same task) and is stored with the parked
request for the per-address cap.
"""

from __future__ import annotations

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.transport_security import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, request_response

from reticula_mcp.auth.netaddr import caller_bucket
from reticula_mcp.auth.provider import ReticulaProvider, caller_bucket_var
from reticula_mcp.config import Settings

MAX_BODY = 64 * 1024


def routes(settings: Settings, provider: ReticulaProvider) -> list[Route]:
    handler = AuthorizationHandler(provider)

    async def authorize(request: Request) -> Response:
        token = caller_bucket_var.set(caller_bucket(request, settings))
        try:
            return await handler.handle(request)
        finally:
            caller_bucket_var.reset(token)

    # No CORS here (clients redirect the browser to it), as in the SDK's route.
    return [
        Route(
            "/authorize",
            endpoint=RequestBodyLimitMiddleware(request_response(authorize), MAX_BODY),
            methods=["GET", "POST"],
        )
    ]
