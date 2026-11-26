"""Fixes from the review of the account pages (W1-W8); each test failed on
005caa3."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import pytest

from reticula_mcp import account_admin, accounts
from reticula_mcp.auth import grants, websession
from reticula_mcp.db import connect
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api
from reticula_mcp.graph import handlers as graph
from tests.auth_support import PASSWORD, Assistant, run_mcp, start_live
from tests.graph_support import mk_hub, mk_note, share
from tests.test_account_web import COOKIE, Browser, _value, db


@pytest.fixture
def site(fresh_db_url: str) -> Any:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    with start_live(fresh_db_url) as live:
        yield live, ana


# --- W1: export is bounded ----------------------------------------------------


def test_a_second_export_within_ten_minutes_is_refused(site: Any) -> None:
    live, _ = site
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    assert (
        b.post("/account/export", {"csrf": b.csrf("/account/export")}).status_code
        == 200
    )
    again = b.post("/account/export", {"csrf": b.csrf("/account/export")})
    assert again.status_code == 429 and "10 minutes" in again.text


def test_an_export_over_the_size_cap_is_refused_with_a_message(
    site: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, ana = site
    monkeypatch.setattr(account_admin, "EXPORT_MAX_BYTES", 100)
    with db(live.db_url) as conn:
        mk_note(conn, ana, "x" * 500, title="Big")
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    r = b.post("/account/export", {"csrf": b.csrf("/account/export")})
    assert r.status_code == 413 and "too large" in r.text


def test_only_one_export_per_account_at_a_time() -> None:
    with account_admin.export_slot(41):
        with pytest.raises(ReticulaError):
            with account_admin.export_slot(41):
                pass
        with account_admin.export_slot(42):  # another account is not held up
            pass
    with account_admin.export_slot(41):
        pass


def test_the_zip_is_built_without_a_database_connection(
    conn: Any, make_account: Any
) -> None:
    acct = make_account(handle="ana")
    mk_note(conn, acct, "x", title="One")
    rows = account_admin.export_rows(conn, acct)
    with account_admin.build_zip(acct, rows) as spooled:  # takes no connection
        data = spooled.read()
    assert data[:2] == b"PK"


# --- W2: delete against a concurrent write -------------------------------------


def test_a_write_that_starts_before_the_delete_is_deleted_with_it(
    fresh_db_url: str,
) -> None:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    writer = connect(fresh_db_url)
    accounts.lock_for_write(writer, ana)  # what the write path does first
    late = graph.put(writer, ana, text="written while the delete waits", title="Late")
    done: list[Any] = []

    def delete() -> None:
        with connect(fresh_db_url) as conn, conn.transaction():
            done.append(account_admin.delete_everything(conn, ana))

    t = threading.Thread(target=delete)
    t.start()
    time.sleep(0.5)
    assert not done  # the delete waits for the write
    writer.commit()
    writer.close()
    t.join(10)
    assert done
    with connect(fresh_db_url) as conn:
        live = conn.execute(
            "SELECT count(*) AS n FROM node WHERE author = %s AND tombstoned_at IS NULL",
            (ana.person_id,),
        ).fetchone()["n"]
        assert live == 0, late
        with pytest.raises(ReticulaError):
            accounts.lock_for_write(conn, ana)  # a write after it is refused


def test_the_write_path_takes_the_account_lock(
    fresh_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        tok = accounts.mint_token(conn, ana, "t")
        conn.commit()
    taken: list[int] = []
    real = accounts.lock_for_write

    def spy(conn: Any, account: Any) -> None:
        taken.append(account.id)
        real(conn, account)

    monkeypatch.setattr(accounts, "lock_for_write", spy)

    async def work(session: Any, init: Any) -> str:
        r = await session.call_tool("put", {"text": "a note", "title": "T"})
        return r.content[0].text

    with start_live(fresh_db_url) as live:
        assert "saved" in run_mcp(live, tok, work)
    assert taken == [ana.id]


# --- W3: a failed withdrawal rolls the deletion back ----------------------------


def test_a_failed_step_deletes_nothing_and_says_so(
    site: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, ana = site
    with db(live.db_url) as conn:
        bea = accounts.create_account(conn, "bea@example.com", PASSWORD, "bea")
        hub = mk_hub(conn, bea, "Bea's")
        share(conn, bea, hub, ana, "add")
        theirs = mk_note(conn, bea, "x", hub=hub, title="Bea's note")
        mine_hub = mk_hub(conn, ana, "Ana's")
        graph.link(conn, ana, ref=theirs, to=mine_hub)
        mk_note(conn, ana, "kept", title="Kept")

    def broken(*a: Any, **k: Any) -> None:
        raise ReticulaError("Conflict", "that filing changed meanwhile")

    monkeypatch.setattr(api, "remove_membership", broken)
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    r = b.post(
        "/account/delete",
        {"csrf": b.csrf("/account/delete"), "handle": "ana", "password": PASSWORD},
    )
    assert r.status_code == 409 and "Nothing was deleted" in r.text
    with db(live.db_url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM node WHERE author = %s AND tombstoned_at IS NULL",
                (ana.person_id,),
            ).fetchone()["n"]
            == 2
        )
        assert accounts.authenticate(conn, "ana@example.com", PASSWORD) is not None


# --- W4: non-ASCII form values --------------------------------------------------


def test_non_ascii_form_tokens_are_refused_not_an_error(site: Any) -> None:
    live, _ = site
    b = Browser(live.url)
    b.http.get("/account/signin")
    r = b.http.post(
        "/account/signin",
        content=b"form=%C3%A9&email=ana%40example.com&password=x",
        headers={
            "Origin": live.url,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert r.status_code == 403
    b.sign_in("ana@example.com")
    r = b.http.post(
        "/account/settings",
        content=b"csrf=%C3%A9&listed=no",
        headers={
            "Origin": live.url,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert r.status_code == 403


# --- W5: sessions -------------------------------------------------------------


def test_signing_in_ends_the_session_presented(site: Any) -> None:
    live, _ = site
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    old = b.http.cookies[COOKIE]
    b.sign_in("ana@example.com")
    with db(live.db_url) as conn:
        assert websession.load(conn, old) is None
        assert (
            websession.count(
                conn, accounts.get_account_by_email(conn, "ana@example.com").id
            )
            == 1
        )


def test_sessions_are_capped_and_stale_ones_purged(
    conn: Any, make_account: Any
) -> None:
    acct = make_account(handle="ana")
    tokens = [
        websession.create(conn, acct.id, keep=False)
        for _ in range(websession.MAX_PER_ACCOUNT + 3)
    ]
    assert websession.count(conn, acct.id) == websession.MAX_PER_ACCOUNT
    assert websession.load(conn, tokens[0]) is None  # the oldest ended
    assert websession.load(conn, tokens[-1]) is not None
    conn.execute("UPDATE web_session SET last_seen_at = now() - interval '31 minutes'")
    assert websession.count(conn, acct.id) == 0  # idle ones do not count
    grants.purge_stale(conn)
    assert conn.execute("SELECT count(*) AS n FROM web_session").fetchone()["n"] == 0


# --- W6: nothing of the account is left ---------------------------------------


def test_delete_leaves_no_settings_grants_counters_or_codes(site: Any) -> None:
    live, ana = site
    with db(live.db_url) as conn:
        bea = accounts.create_account(conn, "bea@example.com", PASSWORD, "bea")
        hub = mk_hub(conn, bea, "Bea's")
        share(conn, bea, hub, ana, "read")
        conn.execute(
            "INSERT INTO account_setting (account_id, name, value) VALUES (%s, 'listed', 'no')",
            (ana.id,),
        )
        conn.execute(
            "INSERT INTO connection_setting (connection, account_id, name, value)"
            " VALUES ('token:1', %s, 'word_handles', 'on')",
            (ana.id,),
        )
        conn.execute(
            "INSERT INTO onboarding_shown (connection, account_id) VALUES ('token:1', %s)",
            (ana.id,),
        )
        conn.execute(
            "INSERT INTO signup_attempt (email, ip) VALUES ('ana@example.com', '10.0.0.0/24')"
        )
    a = Assistant(live)
    a.connect("signin", "ana@example.com")
    a.close()
    b = Browser(live.url)
    b.sign_in("ana@example.com", "a wrong password")  # leaves a counter with the e-mail
    b.sign_in("ana@example.com")
    b.post(
        "/account/delete",
        {"csrf": b.csrf("/account/delete"), "handle": "ana", "password": PASSWORD},
    )
    with db(live.db_url) as conn:
        for table in (
            "account_setting",
            "connection_setting",
            "onboarding_shown",
            "oauth_code",
        ):
            n = conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE account_id = %s", (ana.id,)
            ).fetchone()["n"]
            assert n == 0, table
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM hub_grant WHERE grantee = %s",
                (ana.person_id,),
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM auth_event WHERE subject ILIKE %s",
                ("%ana@example.com%",),
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM signup_attempt WHERE email ILIKE 'ana@example.com'"
            ).fetchone()["n"]
            == 0
        )


# --- W8: origins, HSTS ---------------------------------


def _https_client(fresh_db_url: str) -> Any:
    from starlette.testclient import TestClient

    from reticula_mcp.config import Settings
    from reticula_mcp.server import build_app

    settings = Settings(
        database_url=fresh_db_url,
        public_url="https://reticula.example.com",
        trusted_proxies=("127.0.0.1",),
    )
    return TestClient(build_app(settings), base_url="https://reticula.example.com")


def test_origins_are_compared_normalised_and_https_pages_send_hsts(
    fresh_db_url: str,
) -> None:
    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    with _https_client(fresh_db_url) as client:
        form = client.get("/account/signin")
        assert form.headers["strict-transport-security"] == "max-age=31536000"
        r = client.post(
            "/account/signin",
            data={
                "form": _value(form.text, "form"),
                "email": "ana@example.com",
                "password": PASSWORD,
            },
            headers={"Origin": "https://RETICULA.example.com:443"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["strict-transport-security"] == "max-age=31536000"


def test_plain_http_pages_send_no_hsts(site: Any) -> None:
    live, _ = site
    assert (
        "strict-transport-security"
        not in httpx.get(f"{live.url}/account/signin").headers
    )
