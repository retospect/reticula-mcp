"""The routes of the public pages. ``build_app`` adds them before MCP's own.

They read nothing from the request but its path: no query string, header or
cookie is used, so no input can reach a page. /hubs is the only page with data
from the database; it is rendered at most once every HUBS_SECONDS per
process and sent with no-cache, so a hub taken off the page is gone within that
time. A render that fails answers 503, and that answer is kept FAILED_SECONDS.
"""

from __future__ import annotations

import logging
import time
from importlib import resources
from threading import Lock

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Database
from reticula_mcp.web import hubs, results, slides, texts
from reticula_mcp.web.html import HEADERS, page, respond

HUBS_SECONDS = 10
FAILED_SECONDS = 60
FAILED = (
    "<h1>Public hubs</h1><p>The list of public hubs is not available right now."
    " Try again in a minute.</p>"
)
log = logging.getLogger(__name__)
ROBOTS = "User-agent: *\nAllow: /$\nAllow: /hubs\nDisallow: /\n"


class _HubCache:
    """The rendered /hubs page and when it was made; one per app."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.made = 0.0
        self.text = ""
        self.status = 200


def routes(settings: Settings, db: Database) -> list[Route]:
    cache = _HubCache()
    css = (resources.files("reticula_mcp.web") / "site.css").read_text(encoding="utf-8")

    def fixed(title: str, body: str, path: str) -> Route:
        text = page(title, body, path=path)

        async def endpoint(request: Request) -> Response:
            return respond(text)

        return Route(path, endpoint=endpoint, methods=["GET", "HEAD"])

    def render_hubs() -> tuple[str, int]:
        with cache.lock:
            keep = HUBS_SECONDS if cache.status == 200 else FAILED_SECONDS
            if time.monotonic() - cache.made < keep and cache.text:
                return cache.text, cache.status
            try:
                with db.connection() as conn:
                    body, status = hubs.body(conn), 200
            except Exception:
                log.exception("rendering /hubs failed")
                body, status = FAILED, 503
            cache.text = page("Public hubs · Reticula", body, path="/hubs")
            cache.status = status
            cache.made = time.monotonic()
            return cache.text, cache.status

    async def hub_page(request: Request) -> Response:
        text, status = await anyio.to_thread.run_sync(render_hubs)
        return respond(text, no_cache=True, status=status)

    async def connect_page(request: Request) -> Response:
        # the address is the server's own setting, read now: never stored in a file
        body = texts.connect(settings.base_url)
        return respond(page("Connect · Reticula", body, path="/connect"))

    async def stylesheet(request: Request) -> Response:
        return Response(
            css,
            media_type="text/css",
            headers={**HEADERS, "Cache-Control": "public, max-age=3600"},
        )

    async def robots(request: Request) -> Response:
        return PlainTextResponse(ROBOTS, headers=dict(HEADERS))

    return [
        fixed("Reticula", texts.FRONT, "/"),
        fixed("Slides · Reticula", slides.body(), "/slides"),
        fixed("How well it works · Reticula", results.body(), "/results"),
        fixed("Vision · Reticula", texts.VISION, "/vision"),
        fixed("Privacy · Reticula", texts.PRIVACY, "/privacy"),
        Route("/hubs", endpoint=hub_page, methods=["GET", "HEAD"]),
        Route("/connect", endpoint=connect_page, methods=["GET", "HEAD"]),
        Route("/static/site.css", endpoint=stylesheet, methods=["GET", "HEAD"]),
        Route("/robots.txt", endpoint=robots, methods=["GET", "HEAD"]),
    ]
