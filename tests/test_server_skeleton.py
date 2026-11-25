"""The real ASGI app, in-process, over real HTTP and MCP; frozen tool contract."""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from reticula_mcp import accounts
from reticula_mcp.config import Settings
from reticula_mcp.content import handlers as content_handlers
from reticula_mcp.db import connect
from reticula_mcp.graph import handlers as graph_handlers
from reticula_mcp.search import handlers as search_handlers
from reticula_mcp.server import build_app
from tests.conftest import PASSWORD

CONTRACT = json.loads((Path(__file__).parent / "contract" / "tools.json").read_text())
OWNER = {
    "start": "content", "profile": "content", "search": "search", "get": "search",
    "put": "graph", "edit": "graph", "link": "graph", "delete": "graph", "share": "graph",
}  # fmt: skip
HANDLERS = {
    "start": content_handlers.start, "profile": content_handlers.profile,
    "search": search_handlers.search, "get": search_handlers.get,
    "put": graph_handlers.put, "edit": graph_handlers.edit, "link": graph_handlers.link,
    "delete": graph_handlers.delete, "share": graph_handlers.share,
}  # fmt: skip


class Live:
    def __init__(self, url: str, db_url: str) -> None:
        self.url = url
        self.db_url = db_url

    def make_token(
        self, email: str = "mcp@example.com", label: str = "test"
    ) -> tuple[Any, str]:
        with connect(self.db_url) as conn:
            account = accounts.create_account(conn, email, PASSWORD)
            token = accounts.mint_token(conn, account, label)
            conn.commit()
        return account, token


@pytest.fixture
def live(fresh_db_url: str) -> Iterator[Live]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = Settings(database_url=fresh_db_url, host="127.0.0.1", port=port)
    server = uvicorn.Server(uvicorn.Config(build_app(settings), log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "server did not start"
    yield Live(f"http://127.0.0.1:{port}", fresh_db_url)
    server.should_exit = True
    thread.join(timeout=15)


def run_mcp(live: Live, token: str, work):
    async def go():
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=30
        ) as client:
            async with streamable_http_client(
                f"{live.url}/mcp", http_client=client
            ) as (r, w, _):
                async with ClientSession(r, w) as session:
                    init = await session.initialize()
                    return await work(session, init)

    return asyncio.run(go())


def normalise(schema: dict[str, Any]) -> dict[str, Any]:
    """Names, types, defaults, required: what a client depends on (not prose)."""
    props = {}
    for name, p in schema.get("properties", {}).items():
        props[name] = {k: v for k, v in p.items() if k not in ("description", "title")}
    return {"properties": props, "required": sorted(schema.get("required", []))}


def test_tool_contract_is_frozen(live):
    _, token = live.make_token()

    async def work(session, init):
        assert init.instructions and "start" in init.instructions
        assert init.serverInfo.name == "reticula"
        return (await session.list_tools()).tools

    tools = run_mcp(live, token, work)
    assert sorted(t.name for t in tools) == sorted(CONTRACT) == sorted(OWNER)
    for t in tools:
        assert normalise(t.inputSchema) == CONTRACT[t.name], t.name
        assert t.description and len(t.description) > 40, t.name
        for pname, p in t.inputSchema.get("properties", {}).items():
            assert p.get("description"), f"{t.name}.{pname} needs a description"


def test_handler_signatures_match_the_tools():
    for name, handler in HANDLERS.items():
        params = list(inspect.signature(handler).parameters.values())
        assert [p.name for p in params[:2]] == ["conn", "viewer"], name
        rest = params[2:]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in rest), name
        schema = CONTRACT[name]
        assert [p.name for p in rest] == list(schema["properties"]), name
        for p in rest:
            declared = schema["properties"][p.name]
            if "default" in declared:
                assert p.default == declared["default"], f"{name}.{p.name}"
            else:
                assert p.default is inspect.Parameter.empty, f"{name}.{p.name}"


def test_unauthenticated_and_bad_tokens_are_refused(live):
    account, token = live.make_token()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    hdr = {"Accept": "application/json, text/event-stream"}
    with httpx.Client(base_url=live.url) as http:
        r = http.post("/mcp", json=body, headers=hdr)
        assert r.status_code == 401 and "www-authenticate" in r.headers
        r = http.post(
            "/mcp", json=body, headers={**hdr, "Authorization": "Bearer nope"}
        )
        assert r.status_code == 401
        assert http.get("/healthz").json()["status"] == "ok"
    # a revoked token stops working
    with connect(live.db_url) as conn:
        conn.execute("UPDATE oauth_token SET revoked_at = now()")
        conn.commit()
    with httpx.Client(base_url=live.url) as http:
        r = http.post(
            "/mcp", json=body, headers={**hdr, "Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401


def test_handler_sees_the_right_viewer_and_errors_render(live, monkeypatch):
    a, ta = live.make_token("a@example.com")
    b, tb = live.make_token("b@example.com")
    seen = []

    def start(conn, viewer):
        seen.append((viewer.handle, viewer.person_id))
        conn.execute(
            "UPDATE account SET handle = handle"
        )  # a write in the call's transaction
        return f"hello @{viewer.handle}"

    def search(conn, viewer, **kw):
        from reticula_mcp.errors import ReticulaError

        raise ReticulaError("NotFound", "nothing here", hint="try something else")

    def boom(conn, viewer, **kw):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(content_handlers, "start", start)
    monkeypatch.setattr(search_handlers, "search", search)
    monkeypatch.setattr(graph_handlers, "put", boom)

    async def work(session, init):
        return [
            (await session.call_tool(n, args)).content[0].text
            for n, args in (
                ("start", {}),
                ("search", {"query": "q"}),
                ("put", {"text": "t"}),
            )
        ]

    ok_a, err, internal = run_mcp(live, ta, work)
    ok_b, _, _ = run_mcp(live, tb, work)
    assert ok_a == f"hello @{a.handle}" and ok_b == f"hello @{b.handle}"
    assert seen == [(a.handle, a.person_id), (b.handle, b.person_id)]
    assert err == "[error:NotFound] nothing here\nhint: try something else"
    assert internal.startswith("[error:Internal]") and "secret" not in internal
