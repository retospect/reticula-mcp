"""Helpers for the sign-in lane tests: a live server and a scripted OAuth dance.

The dance is what claude.ai does: discover, register a public client, send the
browser to /authorize, sign in on the one page, take the code from the redirect,
exchange it with the PKCE verifier, call MCP with the token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.server import build_app

PASSWORD = "correct horse battery staple"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_REDIRECT = "http://127.0.0.1:53682/callback"


@dataclass
class Live:
    url: str
    db_url: str
    settings: Settings

    @property
    def resource(self) -> str:
        return f"{self.url}/mcp"

    def http(self, **kw: Any) -> httpx.Client:
        return httpx.Client(base_url=self.url, follow_redirects=False, timeout=30, **kw)

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with connect(self.db_url) as conn:
            cur = conn.execute(query, params or None)
            rows = cur.fetchall() if cur.description else []
            conn.commit()
        return rows


@contextmanager
def start_live(db_url: str, **settings_kw: Any) -> Iterator[Live]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = Settings(database_url=db_url, host="127.0.0.1", port=port, **settings_kw)
    config = uvicorn.Config(
        build_app(settings),
        log_level="warning",
        # Our own X-Forwarded-For logic must be the only authority.
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "server did not start"
    try:
        yield Live(f"http://127.0.0.1:{port}", db_url, settings)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def query_of(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def form_field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"no field {name} in page"
    return match.group(1)


@dataclass
class Assistant:
    """One OAuth client (an assistant) driving the flow through a browser."""

    live: Live
    name: str = "Test Assistant"
    redirect_uri: str = CLAUDE_REDIRECT
    client_id: str = ""
    verifier: str = ""
    challenge: str = ""
    state: str = ""
    browser: httpx.Client = field(init=False)
    api: httpx.Client = field(init=False)
    tokens: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.browser = self.live.http()
        self.api = self.live.http()

    def close(self) -> None:
        self.browser.close()
        self.api.close()

    def register(self, **overrides: Any) -> httpx.Response:
        body = {
            "client_name": self.name,
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            **overrides,
        }
        r = self.api.post("/register", json=body)
        if r.status_code == 201:
            self.client_id = r.json()["client_id"]
        return r

    def authorize(self, **overrides: Any) -> httpx.Response:
        self.verifier, self.challenge = pkce()
        self.state = secrets.token_urlsafe(8)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            "resource": self.live.resource,
            **overrides,
        }
        params = {k: v for k, v in params.items() if v is not None}
        return self.browser.get("/authorize", params=params)

    def open_page(self, location: str, mode: str | None = None) -> httpx.Response:
        path = urlsplit(location)
        target = f"{path.path}?{path.query}"
        if mode:
            target += f"&mode={mode}"
        return self.browser.get(target)

    def submit(
        self,
        page: httpx.Response,
        *,
        mode: str,
        email: str,
        password: str = PASSWORD,
        handle: str | None = None,
        **tamper: str,
    ) -> httpx.Response:
        data = {
            "req": form_field(page.text, "req"),
            "csrf": form_field(page.text, "csrf"),
            "mode": mode,
            "email": email,
            "password": password,
            **({"handle": handle} if handle else {}),
            **tamper,
        }
        return self.browser.post("/signin", data=data)

    def sign_in(
        self, mode: str, email: str, password: str = PASSWORD, handle: str | None = None
    ) -> httpx.Response:
        """authorize + page + submit; returns the POST response."""
        auth = self.authorize()
        assert auth.status_code == 302, auth.text
        page = self.open_page(auth.headers["location"], mode)
        assert page.status_code == 200, page.text
        return self.submit(
            page, mode=mode, email=email, password=password, handle=handle
        )

    def code(self, response: httpx.Response) -> str:
        assert response.status_code == 303, response.text[:300]
        location = response.headers["location"]
        assert location.startswith(self.redirect_uri), location
        q = query_of(location)
        assert q["state"] == self.state
        return q["code"]

    def exchange(self, code: str, **overrides: Any) -> httpx.Response:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": self.verifier,
            **overrides,
        }
        r = self.api.post("/token", data=data)
        if r.status_code == 200:
            self.tokens = r.json()
        return r

    def refresh(self, refresh_token: str | None = None) -> httpx.Response:
        r = self.api.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token or self.tokens["refresh_token"],
                "client_id": self.client_id,
            },
        )
        if r.status_code == 200:
            self.tokens = r.json()
        return r

    def connect(
        self, mode: str, email: str, password: str = PASSWORD
    ) -> dict[str, Any]:
        """The whole dance for a ready account; returns the token response."""
        assert self.register().status_code == 201
        r = self.exchange(self.code(self.sign_in(mode, email, password)))
        assert r.status_code == 200, r.text
        return self.tokens

    def mcp_status(self, token: str | None = None) -> int:
        headers = {"Accept": "application/json, text/event-stream"}
        token = token or self.tokens["access_token"]
        headers["Authorization"] = f"Bearer {token}"
        r = self.api.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers,
        )
        return r.status_code


def run_mcp(live: Live, token: str, work: Callable[..., Any]) -> Any:
    async def go() -> Any:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=30
        ) as client:
            async with streamable_http_client(
                f"{live.url}/mcp", http_client=client
            ) as (
                r,
                w,
                _,
            ):
                async with ClientSession(r, w) as session:
                    init = await session.initialize()
                    return await work(session, init)

    return asyncio.run(go())
