"""Onboarding once per connection: the first ``start`` of a connection points
to the instruction on using Reticula as the person's memory; later ones do
not. A connection is a connector sign-in (its grant) or a minted token."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from reticula_mcp import accounts, connection
from reticula_mcp.accounts import Account
from reticula_mcp.content import hooks as content_hooks
from reticula_mcp.content.handlers import start
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Conn
from reticula_mcp.search.handlers import get, search
from tests.auth_support import Assistant, run_mcp, start_live

POINTER = re.compile(
    r'^First time on this connection: read the instruction get\(ref="([0-9a-f-]{8})"\)'
    r" and follow it once\.$",
    re.M,
)
TITLE = "How to make Reticula the person's memory in every conversation"


@pytest.fixture
def shipped(conn: Conn) -> None:
    accounts.ensure_system_account(conn)
    content_hooks.on_migrated(conn)


def test_no_connection_no_line_and_nothing_written(
    conn: Conn, make_account: Callable[..., Account], shipped: None
) -> None:
    ana = make_account(handle="ana")
    assert not POINTER.search(start(conn, ana))
    assert (
        conn.execute("SELECT count(*) AS n FROM onboarding_shown").fetchone()["n"] == 0
    )


def test_the_first_start_of_a_connection_points_to_the_instruction(
    conn: Conn, make_account: Callable[..., Account], shipped: None
) -> None:
    ana = make_account(handle="ana")
    with connection.during({"connection": "grant:abc"}):
        first = start(conn, ana)
        again = start(conn, ana)
    found = POINTER.search(first)
    assert found, first
    assert not POINTER.search(again)
    lines = first.splitlines()
    assert lines[lines.index(found.group(0)) + 1].startswith("next: ")
    # the line points to the instruction, which is also found by search
    assert f'"{TITLE}"' in get(conn, ana, ref=found.group(1)).splitlines()[0]
    assert found.group(1) in search(conn, ana, query="memory", kind="instruction")
    # every other line of start is what it is without the pointer
    assert [ln for ln in lines if ln != found.group(0)] == again.splitlines()
    assert len(lines) <= 60


def test_each_connection_is_onboarded_once(
    conn: Conn, make_account: Callable[..., Account], shipped: None
) -> None:
    ana = make_account(handle="ana")
    for key in ("grant:abc", "token:7"):
        with connection.during({"connection": key}):
            assert POINTER.search(start(conn, ana)), key
            assert not POINTER.search(start(conn, ana)), key
    rows = conn.execute(
        "SELECT connection, account_id FROM onboarding_shown ORDER BY connection"
    ).fetchall()
    assert [(r["connection"], r["account_id"]) for r in rows] == [
        ("grant:abc", ana.id),
        ("token:7", ana.id),
    ]


def test_without_the_shipped_instruction_nothing_is_shown_or_written(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    with connection.during({"connection": "grant:abc"}):
        assert not POINTER.search(start(conn, ana))
    assert (
        conn.execute("SELECT count(*) AS n FROM onboarding_shown").fetchone()["n"] == 0
    )


def test_the_key_of_a_connection() -> None:
    assert connection.key(grant_id="g1", token_id=5) == "grant:g1"
    assert connection.key(grant_id=None, token_id=5) == "token:5"
    assert connection.key(grant_id="", token_id=None) is None
    assert connection.current() is None
    with connection.during({"connection": 3}):  # not a string: no connection
        assert connection.current() is None


def test_through_the_server_a_sign_in_and_a_minted_token(fresh_db_url: str) -> None:
    """A connector sign-in sees the pointer once, also after its tokens are
    refreshed (same grant); a minted token of the same account is another
    connection and sees it once too."""
    with connect(fresh_db_url) as conn:
        accounts.ensure_system_account(conn)
        content_hooks.on_migrated(conn)
        ana = accounts.create_account(
            conn, "ana@example.com", "correct horse battery staple", "ana"
        )
        minted = accounts.mint_token(conn, ana, "laptop")
        conn.commit()

    async def call_start(session, init):
        result = await session.call_tool("start", {})
        return result.content[0].text

    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        tokens = a.connect("signin", "ana@example.com")
        assert POINTER.search(run_mcp(live, tokens["access_token"], call_start))
        assert not POINTER.search(run_mcp(live, tokens["access_token"], call_start))
        assert a.refresh().status_code == 200
        assert not POINTER.search(run_mcp(live, a.tokens["access_token"], call_start))
        assert POINTER.search(run_mcp(live, minted, call_start))
        assert not POINTER.search(run_mcp(live, minted, call_start))
        rows = live.sql("SELECT connection FROM onboarding_shown ORDER BY connection")
        kinds = sorted(r["connection"].split(":")[0] for r in rows)
        assert kinds == ["grant", "token"]
        a.close()
