"""Limits on the running server: statement time, request size, writes per
account; and the command line staying free of the server's limits."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator

import httpx
import pytest
import uvicorn

from reticula_mcp import cli
from reticula_mcp.config import Settings, parse_rate
from reticula_mcp.db import connect
from reticula_mcp.db.pool import STATEMENT_TIMEOUT_SECONDS, Database
from reticula_mcp.errors import RATE_LIMITED, UNAVAILABLE, ReticulaError
from reticula_mcp.search import handlers as search_handlers
from reticula_mcp.server import build_app
from tests.test_server_skeleton import Live, run_mcp


@pytest.fixture
def serve(fresh_db_url: str) -> Iterator[Callable[..., Live]]:
    """Start the app on a free port; ``serve(db=..., **settings)``."""
    started: list[tuple[uvicorn.Server, threading.Thread, Database | None]] = []

    def start(db: Database | None = None, **fields) -> Live:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        settings = Settings(
            database_url=fresh_db_url, host="127.0.0.1", port=port, **fields
        )
        server = uvicorn.Server(
            uvicorn.Config(build_app(settings, db), log_level="warning")
        )
        thread = threading.Thread(
            target=lambda: server.run(sockets=[sock]), daemon=True
        )
        thread.start()
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started, "server did not start"
        started.append((server, thread, db))
        return Live(f"http://127.0.0.1:{port}", fresh_db_url)

    yield start
    for server, thread, db in started:
        server.should_exit = True
        thread.join(timeout=15)
        if db is not None:
            db.close()


# --- statement timeout -------------------------------------------------------------


def test_a_slow_statement_is_stopped_and_reported_in_one_line(
    serve, fresh_db_url, monkeypatch
):
    def slow(conn, viewer, **kw):
        conn.execute("SELECT pg_sleep(5)")
        return "never"

    monkeypatch.setattr(search_handlers, "search", slow)
    live = serve(db=Database(fresh_db_url, statement_timeout=0.5))
    _, token = live.make_token()

    async def work(session, init):
        started = time.monotonic()
        result = await session.call_tool("search", {"query": "q"})
        return result.content[0].text, time.monotonic() - started

    text, took = run_mcp(live, token, work)
    assert took < 4
    assert (
        text
        == f"[error:{UNAVAILABLE}] the request took longer than 0.5 s and was stopped"
    )
    assert "\n" not in text and "Traceback" not in text and "psycopg" not in text


def test_the_server_pool_defaults_to_the_15_second_limit(fresh_db_url):
    assert STATEMENT_TIMEOUT_SECONDS == 15
    db = Database(fresh_db_url)
    try:
        with db.connection() as conn:
            row = conn.execute("SHOW statement_timeout").fetchone()
        assert row["statement_timeout"] == "15s"
    finally:
        db.close()


def test_connections_the_command_line_opens_have_no_timeout(fresh_db_url):
    with connect(fresh_db_url) as conn:
        assert (
            conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
            == "0"
        )


# --- request size ------------------------------------------------------------------


def test_every_post_route_refuses_a_huge_body(serve):
    """Found: the SDK limits the MCP endpoint to 4 MiB and its own OAuth routes to
    the same; our sign-in, registration, revoke and authorize routes cut at 8-16
    KiB. Nothing reads an unbounded body, so there is no guard of our own."""
    live = serve()
    _, token = live.make_token()
    big = b"x" * (5 * 1024 * 1024)
    with httpx.Client(base_url=live.url, timeout=30) as http:
        r = http.post(
            "/mcp",
            content=big,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code == 413
        for path in ("/token", "/register", "/revoke", "/authorize", "/signin"):
            r = http.post(
                path,
                content=big,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert r.status_code in (400, 401, 403, 413), (path, r.status_code)


# --- writes per account ------------------------------------------------------------


def test_the_write_rate_setting_is_parsed_like_the_others():
    assert Settings().write_rate == "600/hour"
    assert parse_rate(Settings.from_env({}).write_rate) == (600, 3600)
    assert (
        Settings.from_env({"RETICULA_WRITE_RATE": "3/minute"}).write_rate == "3/minute"
    )
    with pytest.raises(ReticulaError):
        Settings.from_env({"RETICULA_WRITE_RATE": "lots"})


def test_writes_over_the_rate_are_refused_with_a_time_and_reads_are_not(serve):
    live = serve(write_rate="3/hour")
    _, busy = live.make_token("busy@example.com")
    _, other = live.make_token("other@example.com")

    async def work(session, init):
        out = []
        for i in range(5):
            out.append(
                (await session.call_tool("put", {"text": f"note {i}"})).content[0].text
            )
        out.append(
            (await session.call_tool("search", {"query": "note"})).content[0].text
        )
        out.append((await session.call_tool("start", {})).content[0].text)
        return out

    results = run_mcp(live, busy, work)
    assert all(r.startswith("saved note") for r in results[:3])
    for refused in results[3:5]:
        assert refused.startswith(
            f"[error:{RATE_LIMITED}] write limit reached (3/hour);"
        )
        assert "try again in " in refused and "\n" not in refused
        assert any(unit in refused for unit in (" min", " h", " s"))
    assert not results[5].startswith("[error")  # search is not a write
    assert not results[6].startswith("[error")

    # the count is per account
    async def one(session, init):
        return (await session.call_tool("put", {"text": "mine"})).content[0].text

    assert run_mcp(live, other, one).startswith("saved note")


def test_every_write_tool_counts_and_listing_access_does_not(serve):
    live = serve(write_rate="1/hour")
    _, token = live.make_token()

    async def work(session, init):
        async def call(name, **args):
            return (await session.call_tool(name, args)).content[0].text

        hub = await call("put", kind="hub", title="h", text="h")
        address = hub.split()[2]
        return [
            hub,
            await call("share", hub=address),  # lists access: a read
            await call("edit", ref=address, text="x"),
            await call("link", ref=address, to=address, rel="cites"),
            await call("delete", ref=address),
            await call("share", hub=address, person="@nobody"),
        ]

    out = run_mcp(live, token, work)
    assert out[0].startswith("saved hub")
    assert out[1].startswith("access to")
    assert all(line.startswith(f"[error:{RATE_LIMITED}]") for line in out[2:])


def test_a_failed_write_does_not_use_up_the_allowance(serve):
    live = serve(write_rate="2/hour")
    _, token = live.make_token()

    async def work(session, init):
        async def put(**args):
            return (await session.call_tool("put", args)).content[0].text

        return [
            await put(text="x", kind="Bad Kind"),
            await put(text="x", kind="Bad Kind"),
            await put(text="x", kind="Bad Kind"),
            await put(text="one"),
            await put(text="two"),
            await put(text="three"),
        ]

    out = run_mcp(live, token, work)
    assert all(line.startswith("[error:BadInput]") for line in out[:3])
    assert out[3].startswith("saved") and out[4].startswith("saved")
    assert out[5].startswith(f"[error:{RATE_LIMITED}]")


# --- token --ttl-days -------------------------------------------------------------


@pytest.mark.parametrize("days", ["0", "-3"])
def test_a_token_needs_a_positive_lifetime(fresh_db_url, monkeypatch, capsys, days):
    from reticula_mcp import accounts
    from tests.conftest import PASSWORD

    monkeypatch.setenv("RETICULA_DATABASE_URL", fresh_db_url)
    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "t@example.com", PASSWORD)
        conn.commit()
    code = cli.main(["token", "--email", "t@example.com", f"--ttl-days={days}"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("[error:BadInput] --ttl-days must be at least 1")
    assert captured.out == ""
    with connect(fresh_db_url) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM oauth_token").fetchone()["n"] == 0
        )
    assert cli.main(["token", "--email", "t@example.com", "--ttl-days=7"]) == 0
    assert capsys.readouterr().out.startswith("ret_")
