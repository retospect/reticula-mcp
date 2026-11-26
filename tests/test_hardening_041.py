"""0.4.1 hardening of the web and sign-in paths: /hubs renders any depth and
caches what it renders briefly, the sign-in page keeps the token it set, a
sign-in does not hold a database connection while it checks a password, and
a path with a trailing slash is not redirected."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import pytest

from reticula_mcp import accounts
from reticula_mcp.accounts import Account
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Conn
from reticula_mcp.graph import handlers as graph
from reticula_mcp.web import hubs, pages
from tests.auth_support import PASSWORD, Assistant, form_field, start_live
from tests.graph_support import mk_hub, mk_note


def nested_public_hubs(conn: Conn, owner: Account, depth: int) -> str:
    """A public hub with ``depth`` hubs nested one in the next, by SQL (signing
    a thousand records one by one is slow). The chain borrows the record of a
    real hub: /hubs reads only node, membership and the grant."""
    root = mk_hub(conn, owner, "Root")
    graph.share(conn, owner, hub=root, person="public", mode="read")
    conn.execute(
        """
        WITH r AS (SELECT id, head_record_id, head_version FROM node WHERE address = %(root)s),
        ins AS (
          INSERT INTO node (address, author, author_account_id, kind, head_record_id,
                 head_version, title, text, audience, payload, created_at, updated_at)
          SELECT 'ret:' || %(p)s || '/' || gen_random_uuid()::text, %(p)s, %(a)s, 'hub',
                 r.head_record_id, r.head_version, 'Level ' || g, '', 'hub', '{}'::jsonb,
                 now(), now()
          FROM generate_series(1, %(n)s) g, r
          RETURNING id, title)
        SELECT 1
        """,
        {"root": root, "p": owner.person_id, "a": owner.id, "n": depth},
    )
    rows = conn.execute(
        "SELECT id FROM node WHERE author = %s AND title LIKE 'Level %%'"
        " ORDER BY substring(title from 7)::int",
        (owner.person_id,),
    ).fetchall()
    root_row = conn.execute(
        "SELECT id, head_record_id FROM node WHERE address = %s", (root,)
    ).fetchone()
    chain = [root_row["id"], *(r["id"] for r in rows)]
    conn.execute(
        "INSERT INTO membership (address, node_id, hub_node_id, author, audience,"
        " withdrawn, record_id) SELECT 'ret:' || %s || '/' || gen_random_uuid()::text,"
        " c, p, %s, 'hub', false, %s FROM unnest(%s::bigint[], %s::bigint[]) AS t(c, p)",
        (
            owner.person_id,
            owner.person_id,
            root_row["head_record_id"],
            chain[1:],
            chain[:-1],
        ),
    )
    return root


def test_hubs_renders_a_very_deep_tree(conn: Conn, make_account: Any) -> None:
    """C4: 1,200 nested hubs render, cut at the depth cap, without an error."""
    ana = make_account(handle="ana")
    nested_public_hubs(conn, ana, 1200)
    body = hubs.body(conn)
    assert "Root" in body and "Level 1 " in body
    assert f"Level {hubs.MAX_DEPTH} " in body
    assert f"Level {hubs.MAX_DEPTH + 1} " not in body
    assert "not shown" in body


def test_hubs_stops_at_the_row_cap(conn: Conn, make_account: Any) -> None:
    ana = make_account(handle="ana")
    hub = mk_hub(conn, ana, "Many")
    graph.share(conn, ana, hub=hub, person="public", mode="read")
    for i in range(12):
        mk_note(conn, ana, "x", hub=hub, title=f"Note {i:02d}")
    body = hubs.body(conn, max_rows=5)
    assert body.count("<li>") <= 6
    assert "not shown" in body


def test_a_failed_render_is_cached_and_answers_503(
    fresh_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def broken(conn: Conn, **kw: Any) -> str:
        calls["n"] += 1
        raise RuntimeError("broken")

    monkeypatch.setattr(hubs, "body", broken)
    with start_live(fresh_db_url) as live, httpx.Client(base_url=live.url) as client:
        first, second = client.get("/hubs"), client.get("/hubs")
        assert first.status_code == second.status_code == 503
        assert "content-security-policy" in first.headers
    assert calls["n"] == 1


def test_hubs_is_sent_no_cache_and_kept_at_most_ten_seconds(fresh_db_url: str) -> None:
    """B8: a hub taken off the page is gone within the server's short cache."""
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        hub = mk_hub(conn, ana, "Brief")
        graph.share(conn, ana, hub=hub, person="public", mode="read")
        conn.commit()
    assert pages.HUBS_SECONDS <= 10
    with start_live(fresh_db_url) as live, httpx.Client(base_url=live.url) as client:
        r = client.get("/hubs")
        assert r.headers["cache-control"] == "no-cache"
        assert "Brief" in r.text


def test_a_trailing_slash_is_not_redirected(fresh_db_url: str) -> None:
    """D5: no redirect to another host built from the Host header."""
    with start_live(fresh_db_url) as live, httpx.Client(base_url=live.url) as client:
        for path in ("/mcp/", "/hubs/", "/signin/"):
            r = client.get(path, headers={"Host": "elsewhere.example"})
            assert r.status_code != 307, path
            assert "elsewhere.example" not in r.headers.get("location", ""), path


def test_opening_the_sign_in_page_again_keeps_the_token(fresh_db_url: str) -> None:
    """A8: a second GET of the same sign-in page does not replace the token the
    first one set, so the page the person has open still submits."""
    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        assert a.register().status_code == 201
        auth = a.authorize()
        page = a.open_page(auth.headers["location"])
        # someone else opens the same link, without the person's cookie
        with live.http() as other:
            path = auth.headers["location"].split(live.url, 1)[-1]
            other.get(path)
        # and the person reloads, with their cookie
        again = a.open_page(auth.headers["location"])
        assert form_field(again.text, "csrf") == form_field(page.text, "csrf")
        done = a.submit(page, mode="signin", email="ana@example.com")
        assert done.status_code == 303, done.text[:300]
        a.close()


def test_a_slow_password_check_holds_no_database_connection(
    fresh_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7: ten sign-ins checking a password at once leave the server able to
    answer a page that needs the database."""
    real = accounts.authenticate

    def slow(conn: Conn, email: str, password: str) -> Account | None:
        time.sleep(1.5)
        return real(conn, email, password)

    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    with start_live(fresh_db_url) as live:
        people = []
        for _ in range(10):
            a = Assistant(live)
            assert a.register().status_code == 201
            auth = a.authorize()
            people.append((a, a.open_page(auth.headers["location"])))
        monkeypatch.setattr(accounts, "authenticate", slow)
        # ten addresses: the limit per e-mail address does not stop any of them
        threads = [
            threading.Thread(
                target=lambda a=a, p=p, n=n: a.submit(
                    p,
                    mode="signin",
                    email=f"person{n}@example.com",
                    password="wrong password!",
                )
            )
            for n, (a, p) in enumerate(people)
        ]
        for t in threads:
            t.start()
        time.sleep(0.3)
        with httpx.Client(base_url=live.url, timeout=10) as client:
            started = time.monotonic()
            r = client.get("/hubs")
            took = time.monotonic() - started
        for t in threads:
            t.join()
        for a, _ in people:
            a.close()
        assert r.status_code == 200
        assert took < 1.0, f"/hubs waited {took:.2f} s for a database connection"
