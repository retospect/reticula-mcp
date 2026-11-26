"""One line per request, for the operator: who called, with what, and why a
request was refused.

uvicorn's access line names the TCP peer, which behind a proxy is the proxy,
and gives no reason. When an assistant "does not connect", the questions are:
did it reach the server at all, which program was it, how far did it get, and
what did the server answer. The line answers them::

    POST /mcp 400 2ms from=203.0.113.7 agent="ExampleClient/1.2" mcp=2099-01-01 reason="Bad Request: Unsupported protocol version: 2099-01-01"
    POST /mcp 200 31ms from=203.0.113.7 agent="ExampleClient/1.2" mcp=2025-06-18 client=c_4f2a9e1b

What a line holds, and what it never holds:

* method, path WITHOUT the query string (``/signin?req=...`` and
  ``/authorize?...&state=...`` carry values that must not be kept), status,
  time until the answer started;
* ``from``: the caller's address as ``auth.netaddr.client_address`` works it
  out (``X-Forwarded-For`` is believed only from ``RETICULA_TRUSTED_PROXIES``);
* ``agent``: the ``User-Agent`` header, printable ASCII, cut;
* ``mcp``: the ``MCP-Protocol-Version`` header, when sent;
* ``client``: the start of the connection's client id, when the request carried
  a valid token. All requests of one connection carry the same value, and
  ``reticula-mcp connections list`` shows it in its ``client`` column;
* ``reason``, for a status of 400 and above: the server's OWN error text, taken
  from the answer (``error`` and ``error_description`` of a sign-in error, the
  message of a protocol error, the ``error`` of a ``WWW-Authenticate`` header,
  one of the fixed texts of the MCP package for a bad ``Host`` or ``Origin``).
  An answer that is a page says its reason through ``refusal.note``: a refused
  sign-in reads ``reason="signin: bad_credentials"``.

Never: a request body, a token, a note, a tool's arguments or reply. A tool that
fails answers with status 200 and its error as text, so it has no reason here.
``GET /healthz`` with status 200 is not logged: the container asks every few
seconds.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reticula_mcp import refusal
from reticula_mcp.auth.netaddr import client_address
from reticula_mcp.config import Settings

log = logging.getLogger("reticula_mcp.request")

QUIET_PATHS = frozenset({"/healthz"})
AGENT_MAX = 80
REASON_MAX = 160
BODY_MAX = 4096  # bytes of an error answer read for its reason
_VERSION_RE = re.compile(r"^[0-9A-Za-z.-]{1,20}$")
_CHALLENGE_ERROR_RE = re.compile(r'error="([^"]{1,60})"')
# The MCP package refuses a bad Host, Origin or Content-Type with one of these
# texts as its whole answer. No other text answer is taken for a reason: it
# could repeat what the caller sent.
FIXED_TEXTS = frozenset(
    {
        "Invalid Host header",
        "Invalid Origin header",
        "Invalid Content-Type header",
        "Request body too large",
    }
)
# A validation message quotes what it was given; the reason stops before that.
_ECHO_MARKS = ("input_value", "[type=", "input=")


def one_line(text: str, limit: int) -> str:
    """Printable ASCII, white space as single spaces, cut at ``limit``."""
    flat = " ".join("".join(c if " " <= c <= "~" else " " for c in text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def path_field(path: str) -> str:
    """The request path as one field of the line. A path is percent-decoded,
    so it can hold spaces and quotes; such a path is written JSON-quoted, like
    ``agent=``, and every other path as it is."""
    flat = one_line(path, 120) or "-"
    if any(c in flat for c in ' "\\'):
        return json.dumps(flat)
    return flat


def reason_of(status: int, headers: Headers, body: bytes) -> str:
    """Why the server refused, from its own answer; ``""`` when it does not say."""
    said = ""
    if body and "json" in headers.get("content-type", ""):
        try:
            data: Any = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):  # a protocol error
                said = str(error.get("message") or "")
            elif isinstance(error, str):  # a sign-in error
                detail = data.get("error_description")
                said = f"{error}: {detail}" if isinstance(detail, str) else error
    if not said and body:
        text = body[:80].decode("ascii", "replace").strip()
        if text in FIXED_TEXTS:
            said = text
    if not said:
        challenge = headers.get("www-authenticate", "")
        found = _CHALLENGE_ERROR_RE.search(challenge)
        if found:
            said = found.group(1)
        elif status == 401 and challenge:
            # a bare challenge. Not every 401: the sign-in page answers a wrong
            # password with one, and that is not a missing token.
            said = "no token"
    for mark in _ECHO_MARKS:
        said = said.partition(mark)[0]
    return one_line(said, REASON_MAX)


class RequestLog:
    """ASGI middleware that writes the line. Outermost, so that it also sees
    what the sign-in layer refuses."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0
        answer = Headers()
        body = bytearray()
        written = False
        noted, token = refusal.begin()

        def write() -> None:
            nonlocal written
            if not written:
                written = True
                self._write(
                    scope,
                    status,
                    answer,
                    bytes(body),
                    started,
                    noted.get("reason", ""),
                )

        async def watched(message: Message) -> None:
            nonlocal status, answer
            if message["type"] == "http.response.start":
                status = int(message["status"])
                answer = Headers(raw=message.get("headers", []))
                if status < 400:
                    write()  # now: an event stream may stay open for long
            elif message["type"] == "http.response.body" and status >= 400:
                room = BODY_MAX - len(body)
                if room > 0:
                    body.extend(message.get("body", b"")[:room])
                if not message.get("more_body"):
                    write()
            await send(message)

        try:
            await self.app(scope, receive, watched)
        finally:
            status = status or 500  # ended without an answer
            write()
            refusal.end(token)

    def _write(
        self,
        scope: Scope,
        status: int,
        answer: Headers,
        body: bytes,
        started: float,
        noted: str,
    ) -> None:
        path = scope.get("path", "")
        if status == 200 and path in QUIET_PATHS:
            return
        request = Request(scope)
        parts = [
            scope.get("method", "-"),
            path_field(path),
            str(status),
            f"{(time.perf_counter() - started) * 1000:.0f}ms",
            f"from={client_address(request, self.settings)}",
        ]
        agent = one_line(request.headers.get("user-agent", ""), AGENT_MAX)
        if agent:
            parts.append(f"agent={json.dumps(agent)}")
        version = request.headers.get("mcp-protocol-version", "")
        if version:
            parts.append(f"mcp={version if _VERSION_RE.match(version) else '?'}")
        token = getattr(scope.get("user"), "access_token", None)
        client_id = getattr(token, "client_id", None)
        if isinstance(client_id, str) and client_id:
            parts.append(f"client={one_line(client_id, 200)[:10]}")
        if status >= 400:
            # what the refusing code said itself comes before what the answer shows
            why = one_line(noted, REASON_MAX) or reason_of(status, answer, body)
            if why:
                parts.append(f"reason={json.dumps(why)}")
        log.info(" ".join(parts))
