"""Limits on the tools that only read, per account (readlimit.py): how many
start in a window, how many run at once, how long one may take. The lines
have the form of the write limit's."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from reticula_mcp import accounts
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Database
from reticula_mcp.readlimit import ReadLimits, ReadRefused, deadline
from reticula_mcp.search import handlers as search_handlers
from tests.auth_support import run_mcp, start_live
from tests.conftest import PASSWORD


def call(live: Any, token: str, tool: str, **args: Any) -> str:
    async def work(session: Any, _init: Any) -> str:
        result = await session.call_tool(tool, args)
        return "\n".join(c.text for c in result.content if hasattr(c, "text"))

    out: str = run_mcp(live, token, work)
    return out


@pytest.fixture
def token(fresh_db_url) -> str:
    with connect(fresh_db_url) as c:
        ada = accounts.create_account(c, "ada@example.com", PASSWORD, "ada")
        minted = accounts.mint_token(c, ada, "test")
        c.commit()
    return minted


# ------------------------------------------------------------ the limits alone


def test_the_rate_is_a_sliding_window():
    now = [1000.0]
    limits = ReadLimits("3/minute", 0, clock=lambda: now[0])
    for _ in range(3):
        with limits.read("7"):
            pass
    with pytest.raises(ReadRefused) as refused:
        with limits.read("7"):
            pass
    assert refused.value.wait == 60
    with limits.read("8"):  # another account has its own window
        pass
    now[0] += 30
    with pytest.raises(ReadRefused) as refused:
        with limits.read("7"):
            pass
    assert refused.value.wait == 30
    now[0] += 30.5
    with limits.read("7"):
        pass


def test_a_count_of_zero_switches_the_rate_off():
    limits = ReadLimits("0/minute", 0)
    for _ in range(50):
        with limits.read("7"):
            pass


def test_reads_beyond_the_concurrency_wait_then_are_refused():
    limits = ReadLimits("0/minute", 2, wait=0.2)
    gate = threading.Event()
    inside = threading.Barrier(3)

    def hold() -> None:
        with limits.read("7"):
            inside.wait(5)
            gate.wait(5)

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for t in threads:
        t.start()
    inside.wait(5)  # both are in
    started = time.monotonic()
    with pytest.raises(ReadRefused) as refused:
        with limits.read("7"):
            pass
    assert refused.value.wait is None
    assert time.monotonic() - started >= 0.19  # it waited before refusing
    with limits.read("8"):  # another account is not held up
        pass
    gate.set()
    for t in threads:
        t.join(5)
    with limits.read("7"):  # a place is free again
        pass


# ------------------------------------------------------------- the deadline


def test_a_deadline_that_fires_closes_the_connection(fresh_db_url):
    db = Database(fresh_db_url, max_size=1)
    db.open()
    try:
        with db.connection() as conn:
            pid = conn.info.backend_pid
            with pytest.raises(Exception) as caught:
                with deadline(conn, 0.2) as stopped:
                    conn.execute("SELECT pg_sleep(3)")
            assert stopped.fired and conn.closed
            assert "cancel" in str(caught.value).lower()
        with db.connection() as conn:  # the pool gives a new one
            assert conn.info.backend_pid != pid
            conn.execute("SELECT 1")
    finally:
        db.close()


def test_a_late_cancel_does_nothing_to_the_next_call(fresh_db_url):
    """A deadline that has not fired when its call ends never fires after:
    the next call on the same pooled connection runs its statement whole."""
    db = Database(fresh_db_url, max_size=1)
    db.open()
    try:
        with db.connection() as conn:
            pid = conn.info.backend_pid
            with deadline(conn, 0.2) as stopped:
                conn.execute("SELECT 1")
            assert not stopped.fired
        time.sleep(0.4)  # past the first call's deadline
        with db.connection() as conn:
            assert conn.info.backend_pid == pid  # the same connection
            conn.execute("SELECT pg_sleep(0.3)")  # not cancelled
    finally:
        db.close()


# --------------------------------------------------------- through the server


def test_the_read_rate_through_the_server(fresh_db_url, token):
    with start_live(fresh_db_url, read_rate="2/minute") as live:
        assert "search" in call(live, token, "search", query="kiln")
        assert "search" in call(live, token, "search", query="kiln")
        third = call(live, token, "search", query="kiln")
    assert third.startswith("[error:RateLimited] read limit reached (2/minute);")
    assert "try again in" in third


def test_reads_at_once_through_the_server(fresh_db_url, token, monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    real = search_handlers.search

    def slow(conn, viewer, **params):
        entered.set()
        gate.wait(10)
        return real(conn, viewer, **params)

    monkeypatch.setattr(search_handlers, "search", slow)
    monkeypatch.setattr("reticula_mcp.server.READ_WAIT_SECONDS", 0.3)
    with start_live(fresh_db_url, read_concurrency=1) as live:
        first: dict[str, str] = {}
        holder = threading.Thread(
            target=lambda: first.update(out=call(live, token, "search", query="kiln"))
        )
        holder.start()
        assert entered.wait(10)
        second = call(live, token, "get", ref="abcdef12")
        gate.set()
        holder.join(10)
    assert second == (
        "[error:RateLimited] read limit reached (1 at once); try again in a moment"
    )
    assert "search" in first["out"]


def test_a_read_past_its_time_is_stopped(fresh_db_url, token, monkeypatch):
    def sleeps(conn, viewer, **params):
        conn.execute("SELECT pg_sleep(5)")
        return "never"

    monkeypatch.setattr(search_handlers, "search", sleeps)
    with start_live(fresh_db_url, read_seconds=0.5) as live:
        out = call(live, token, "search", query="kiln")
        assert out.startswith("[error:Unavailable] the request took longer than 0.5 s")
        monkeypatch.setattr(search_handlers, "search", lambda c, v, **p: "fine")
        assert call(live, token, "search", query="kiln") == "fine"  # the pool works


def test_the_settings_are_read_and_checked():
    from reticula_mcp.config import Settings
    from reticula_mcp.errors import ReticulaError

    given = Settings.from_env(
        {
            "RETICULA_READ_RATE": "10/second",
            "RETICULA_READ_CONCURRENCY": "8",
            "RETICULA_READ_SECONDS": "2.5",
        }
    )
    assert (given.read_rate, given.read_concurrency, given.read_seconds) == (
        "10/second",
        8,
        2.5,
    )
    default = Settings.from_env({})
    assert (default.read_rate, default.read_concurrency, default.read_seconds) == (
        "300/minute",
        4,
        20.0,
    )
    for name, value in (
        ("RETICULA_READ_RATE", "lots"),
        ("RETICULA_READ_CONCURRENCY", "four"),
        ("RETICULA_READ_CONCURRENCY", "-1"),
        ("RETICULA_READ_SECONDS", "soon"),
    ):
        with pytest.raises(ReticulaError):
            Settings.from_env({name: value})
