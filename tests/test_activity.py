"""The person's own activity log (activity.py, migration 0357).

Two kinds of test. Those that drive tool calls go through the server's own
``run`` (``create_mcp``, called in process with the access token the sign-in
layer would hand it) and read the rows with a second connection; those about
partitions, the writing statement, ``lines`` and the account's deletion use the
rolled-back ``conn`` of the suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from reticula_mcp import accounts, activity
from reticula_mcp.accounts import Account
from reticula_mcp.config import MAX_ACTIVITY_DAYS, Settings
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Conn, Database
from reticula_mcp.errors import BAD_INPUT, ReticulaError
from reticula_mcp.server import create_mcp
from tests.auth_support import Assistant, run_mcp, start_live
from tests.conftest import PASSWORD
from tests.graph_support import addr
from tests.helpers import make_atom

COLUMNS = {"at", "account_id", "connection", "client", "tool", "outcome", "ref"}


def utc_today() -> date:
    return datetime.now(UTC).date()


# ---------------------------------------------------------------------- harness


@dataclass
class App:
    """The server's ``run`` without HTTP: ``create_mcp`` on a pool of its own."""

    url: str
    db: Database
    mcp: Any
    log: activity.ActivityLog | None
    verifier: Any

    def person(self, email: str, handle: str | None = None) -> Account:
        with connect(self.url) as conn:
            account = accounts.create_account(conn, email, PASSWORD, handle)
            conn.commit()
        return account

    def token(self, account: Account, label: str = "laptop") -> AccessToken:
        """The access token the sign-in layer builds for a minted token."""
        with connect(self.url) as conn:
            raw = accounts.mint_token(conn, account, label)
            conn.commit()

        found = asyncio.run(self.verifier.verify_token(raw))
        assert found is not None
        return found

    def call(self, access: AccessToken | None, tool: str, **args: Any) -> str:
        async def go() -> str:
            auth_context_var.set(AuthenticatedUser(access) if access else None)
            blocks = await self.mcp.call_tool(tool, args)
            return str(blocks[0].text)

        return asyncio.run(go())

    def settle(self) -> None:
        assert self.log is not None
        assert self.log.wait_idle(15), "the log did not settle"

    def rows(self) -> list[dict[str, Any]]:
        return sql(self.url, "SELECT * FROM activity ORDER BY at")


def sql(url: str, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect(url) as conn:
        cur = conn.execute(query, params or None)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
    return rows


@pytest.fixture
def make_app(fresh_db_url: str) -> Iterator[Callable[..., App]]:
    made: list[App] = []

    def make(days: int = 30, **settings_kw: Any) -> App:
        settings = Settings(
            database_url=fresh_db_url, activity_days=days, **settings_kw
        )
        db = Database(fresh_db_url)
        db.open()
        mcp, wiring = create_mcp(settings, db)
        activity.start(db)
        app = App(
            fresh_db_url, db, mcp, activity.log_of(db), _Verifier(wiring.provider)
        )
        made.append(app)
        return app

    yield make
    for app in made:
        activity.stop(app.db)
        app.db.close()


class _Verifier:
    """What the bearer middleware does with the provider: ask it for the token."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def verify_token(self, raw: str) -> AccessToken | None:
        found: AccessToken | None = await self.provider.load_access_token(raw)
        return found


@pytest.fixture
def app(make_app: Callable[..., App]) -> App:
    return make_app()


# ------------------------------------------------------------------ what a row is


def test_the_migration_is_a_partitioned_table_with_these_columns_and_no_partition(
    conn: Conn,
) -> None:
    cols = conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'activity'"
    ).fetchall()
    assert {c["column_name"] for c in cols} == COLUMNS
    kind = conn.execute(
        "SELECT relkind FROM pg_class WHERE relname = 'activity'"
    ).fetchone()
    assert kind["relkind"] == "p"
    assert not conn.execute(
        "SELECT 1 FROM pg_inherits WHERE inhparent = 'activity'::regclass"
    ).fetchall()
    index = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'activity'"
    ).fetchone()
    assert "(account_id, at DESC)" in index["indexdef"]


def test_an_ok_call_and_a_refused_call_each_leave_one_row_of_these_fields(
    app: App,
) -> None:
    ana = app.person("ana@example.com", "ana")
    access = app.token(ana, "laptop")
    before = datetime.now(UTC)
    assert app.call(access, "profile").startswith("@ana")
    refused = app.call(access, "get", ref="deadbeef")
    assert refused.startswith("[error:NotFound]")
    app.settle()
    rows = app.rows()
    assert [r["tool"] for r in rows] == ["profile", "get"]
    ok, bad = rows
    assert set(ok) == COLUMNS
    assert before - timedelta(seconds=1) <= ok["at"] <= datetime.now(UTC)
    assert ok["at"].utcoffset() is not None
    assert ok["account_id"] == ana.id
    token_id = sql(app.url, "SELECT id FROM oauth_token")[0]["id"]
    assert ok["connection"] == f"token:{token_id}"
    # an operator token: its label, then the start of the client id that the
    # request log prints as client=
    assert ok["client"] == "laptop (reticula-t)"
    assert (ok["outcome"], ok["ref"]) == ("ok", None)
    assert (bad["outcome"], bad["ref"]) == ("NotFound", None)
    assert bad["account_id"] == ana.id


def test_every_error_code_is_the_outcome_and_an_unexpected_failure_is_internal(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reticula_mcp.search import handlers as search_handlers

    ana = app.person("ana@example.com", "ana")
    access = app.token(ana)
    app.call(access, "profile", handle="x")  # BadInput
    app.call(access, "share", hub="deadbeef", person="@nobody")  # NotFound
    app.call(access, "delete", ref="deadbeef")  # NotFound

    def broken(conn: Conn, viewer: Account, **kw: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(search_handlers, "search", broken)
    assert app.call(access, "search", query="x").startswith("[error:Internal]")
    app.settle()
    got = [(r["tool"], r["outcome"]) for r in app.rows()]
    assert got == [
        ("profile", "BadInput"),
        ("share", "NotFound"),
        ("delete", "NotFound"),
        ("broken", "Internal"),
    ]


def test_a_call_with_nobody_signed_in_leaves_nothing(app: App) -> None:
    out = app.call(None, "profile")
    assert out.startswith("[error:Forbidden]")
    app.settle()
    assert app.rows() == []


def test_a_refusal_by_the_write_limit_is_logged_with_its_code(
    make_app: Callable[..., App],
) -> None:
    app = make_app(30, write_rate="1/hour")
    ana = app.person("ana@example.com", "ana")
    access = app.token(ana)
    assert app.call(access, "put", text="one").startswith("saved note")
    assert app.call(access, "put", text="two").startswith("[error:RateLimited]")
    app.settle()
    assert [(r["tool"], r["outcome"]) for r in app.rows()] == [
        ("put", "ok"),
        ("put", "RateLimited"),
    ]


# --------------------------------------------------------------------- the ref


def test_ref_is_the_short_id_of_the_callers_own_note_and_never_another_persons(
    app: App,
) -> None:
    ana = app.person("ana@example.com", "ana")
    bo = app.person("bo@example.com", "zorbulax")
    a, b = app.token(ana), app.token(bo)

    mine = addr(app.call(a, "put", text="alpha"))
    hub = addr(app.call(a, "put", text="a hub of ana", title="Hub", kind="hub"))
    assert app.call(a, "share", hub=hub[-8:], person="@zorbulax").strip()
    # they share no hub yet: Bo's yes (0304); it names Ana's hub, so no ref
    assert "you can now" in app.call(
        b, "share", hub=hub[-8:], person="@zorbulax", mode="add"
    )
    filed = addr(app.call(a, "put", text="beta", hub=hub[-8:]))
    app.call(a, "get", ref=mine[-8:])
    app.call(a, "get", ref=mine)  # a full address
    app.call(a, "get", ref=f"{mine[-8:]}:A")  # the form of a group
    app.call(a, "edit", ref=mine[-8:], title="t")
    app.settle()
    ana_rows = [(r["tool"], r["ref"]) for r in app.rows() if r["account_id"] == ana.id]
    assert (
        ana_rows
        == [
            ("put", None),  # a new note names nothing
            ("put", None),  # a new hub names nothing
            ("share", hub[-8:]),
            ("put", hub[-8:]),
            ("get", mine[-8:]),
            ("get", mine[-8:]),
            ("get", mine[-8:]),
            ("edit", mine[-8:]),
        ]
    )

    # Bo reads Ana's note in the shared hub, and a hub of Ana's: no ref
    assert "beta" in app.call(b, "get", ref=filed[-8:])
    app.call(b, "search", query="beta", hub=hub[-8:])
    app.call(b, "put", text="gamma", hub=hub[-8:])
    app.call(b, "get", ref=mine[-8:])  # not readable by Bo
    own = addr(app.call(b, "put", text="delta"))
    app.call(b, "link", ref=own[-8:], to=hub[-8:])  # first named: Bo's own
    # first named is Ana's; the first of Bo's own is `to`
    app.call(b, "link", ref=filed[-8:], to=own[-8:])
    app.call(b, "get", ref=own[-8:])
    app.settle()
    bo_rows = [(r["tool"], r["ref"]) for r in app.rows() if r["account_id"] == bo.id]
    assert (
        bo_rows
        == [
            ("share", None),  # the yes to Ana's share: Ana's hub is not named
            ("get", None),
            ("search", None),
            ("put", None),
            ("get", None),
            ("put", None),
            ("link", own[-8:]),
            ("link", own[-8:]),
            ("get", own[-8:]),
        ]
    )


def test_no_column_of_any_row_holds_a_query_a_text_a_title_a_reply_or_a_handle(
    app: App,
) -> None:
    ana = app.person("ana@example.com", "ana")
    bo = app.person("bo@example.com", "zorbulax")
    a = app.token(ana)
    note = addr(
        app.call(a, "put", text="wombatnote77 about kumquats", title="zebratitle88")
    )
    hub = addr(app.call(a, "put", text="hubtext99", kind="hub", title="hubtitle99"))
    app.call(a, "search", query="quasarflux42 and more")
    app.call(a, "edit", ref=note[-8:], text="replykumquat55", tags=["tagquince66"])
    app.call(a, "share", hub=hub[-8:], person="@zorbulax")
    app.call(a, "share", hub=hub[-8:], person=bo.person_id)
    app.call(a, "profile", handle="ana2renamed")
    app.call(
        a, "get", ref="ret:" + bo.person_id + "/00000000-0000-7000-8000-000000000000"
    )
    app.settle()
    rows = app.rows()
    assert len(rows) == 8
    forbidden = [
        "wombatnote77", "kumquat", "zebratitle88", "hubtext99", "hubtitle99",
        "quasarflux42", "replykumquat55", "tagquince66", "zorbulax", "ana2renamed",
        bo.person_id, "ret:", "@",
    ]  # fmt: skip
    for row in rows:
        whole = " | ".join(str(v) for v in row.values())
        for word in forbidden:
            assert word not in whole, (word, whole)
        assert set(row) == COLUMNS


# ------------------------------------------------------------------ the client


def test_a_connector_sign_in_and_an_operator_token_name_their_client(
    fresh_db_url: str,
) -> None:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        minted = accounts.mint_token(conn, ana, "my laptop")
        conn.commit()

    async def profile(session: Any, init: Any) -> str:
        result = await session.call_tool("profile", {})
        return str(result.content[0].text)

    with start_live(fresh_db_url) as live:
        a = Assistant(live, name="Claude")
        a.connect("signin", "ana@example.com")
        assert run_mcp(live, a.tokens["access_token"], profile).startswith("@ana")
        assert run_mcp(live, minted, profile).startswith("@ana")
        deadline = time.time() + 15
        rows: list[dict[str, Any]] = []
        while time.time() < deadline and len(rows) < 2:
            rows = live.sql("SELECT * FROM activity ORDER BY at")
            time.sleep(0.1)
        a.close()
    assert len(rows) == 2
    connector, operator = rows
    assert connector["client"] == f"Claude ({a.client_id[:10]})"
    assert a.client_id[:10].startswith("c_")
    assert connector["connection"].startswith("grant:")
    assert operator["client"] == "my laptop (reticula-t)"
    assert operator["connection"].startswith("token:")
    assert {r["account_id"] for r in rows} == {ana.id}


# -------------------------------------------------------- the writing statement


def _entry(
    account: Account,
    *,
    tool: str = "get",
    outcome: str = "ok",
    connection: str | None = "grant:g1",
    client: str | None = "c_4f2a9e1bzzzzzzzzzzzz",
    refs: tuple[str, ...] = (),
    at: datetime | None = None,
) -> activity.Entry:
    return (
        at or datetime.now(UTC),
        account.id,
        connection,
        client,
        tool,
        outcome,
        refs,
    )


def _written(conn: Conn, entries: list[activity.Entry]) -> list[dict[str, Any]]:
    activity.maintain_in(conn, 30)
    n = activity.write_batch(conn, entries)
    rows = conn.execute("SELECT * FROM activity ORDER BY at, tool").fetchall()
    assert n == len(rows)
    return rows


def test_the_client_is_the_registered_name_cut_to_60_then_the_id_in_brackets(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    info = '{"client_name": %s}'
    for cid, name in (
        ("c_4f2a9e1b0000", "Claude"),
        ("c_longname000", "L" * 100),
        ("c_messy000000", "line\nbreak\t and  gaps "),
        ("c_noname00000", ""),
    ):
        conn.execute(
            "INSERT INTO oauth_client (client_id, client_info) VALUES (%s, %s::jsonb)",
            (cid, info % json.dumps(name)),
        )
    conn.execute(
        "INSERT INTO oauth_client (client_id, client_info) VALUES"
        " ('c_nokey000000', '{}'::jsonb)"
    )
    conn.execute(
        "INSERT INTO oauth_token (token_hash, kind, account_id, label)"
        " VALUES ('h1', 'access', %s, 'my laptop') RETURNING id",
        (ana.id,),
    )
    token_id = conn.execute("SELECT id FROM oauth_token").fetchone()["id"]
    base = datetime.now(UTC)
    ids = ["c_4f2a9e1b0000", "c_longname000", "c_messy000000", "c_noname00000"]
    entries = [
        _entry(ana, client=cid, tool=f"t{i}", at=base + timedelta(seconds=i))
        for i, cid in enumerate(ids)
    ]
    entries.append(
        _entry(ana, client="c_nokey000000", tool="t4", at=base + timedelta(seconds=4))
    )
    entries.append(
        _entry(ana, client="c_unregistered", tool="t5", at=base + timedelta(seconds=5))
    )
    entries.append(
        _entry(
            ana,
            client="reticula-token",
            connection=f"token:{token_id}",
            tool="t6",
            at=base + timedelta(seconds=6),
        )
    )
    entries.append(
        _entry(
            ana, client=None, connection=None, tool="t7", at=base + timedelta(seconds=7)
        )
    )
    got = [r["client"] for r in _written(conn, entries)]
    assert got == [
        "Claude (c_4f2a9e1b)",
        "L" * 60 + " (c_longname)",
        "line break and gaps (c_messy000)",
        "(c_noname00)",
        "(c_nokey000)",
        "(c_unregist)",
        "my laptop (reticula-t)",
        None,
    ]


def test_the_ref_is_looked_up_the_way_resolve_ref_does_and_only_for_the_author(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana, bo = make_account(handle="ana"), make_account(handle="bob")
    mine = make_atom(conn, ana, "ana's note")["address"]
    hub = make_atom(conn, ana, "ana's hub", kind="hub")["address"]
    theirs = make_atom(conn, bo, "bo's note")["address"]
    short = mine[-8:]
    cases: list[tuple[tuple[str, ...], str | None]] = [
        ((short,), short),
        ((mine[-6:],), short),  # 6 characters are enough
        ((mine,), short),  # a full address
        ((mine.upper(),), None),  # an address is exact; a suffix is lowered
        ((short.upper(),), short),
        ((f"{short}:A",), short),  # a group of a hub
        ((f"{short}:yes-ab12",), short),  # the confirming call of a delete
        ((f"nonsense {short}",), None),  # the first word only
        ((f"{short} {theirs[-8:]}",), short),  # several refs: the first
        ((hub[-8:],), hub[-8:]),
        ((theirs[-8:],), None),  # another person's note
        ((theirs,), None),
        (("zzzzzz",), None),  # not a ref at all
        (("abc",), None),  # too short
        (("deadbeef",), None),  # names no note
        (("not a ref", short), short),  # the first argument that is a ref
        ((theirs[-8:], short), short),  # the first that is the caller's own
        ((theirs[-8:], hub), hub[-8:]),  # link(ref=<theirs>, to=<my hub>)
        ((short, hub[-8:]), short),  # both own: the first
        (("deadbeef", theirs, hub[-8:]), hub[-8:]),  # third argument
        ((theirs[-8:], "deadbeef"), None),  # none of the caller's own
        ((), None),
    ]
    base = datetime.now(UTC)
    entries = [
        _entry(ana, tool=f"t{i:02d}", refs=refs, at=base + timedelta(seconds=i))
        for i, (refs, _) in enumerate(cases)
    ]
    got = [r["ref"] for r in _written(conn, entries)]
    assert got == [want for _, want in cases]


def test_an_account_that_is_gone_is_skipped_and_the_rest_is_written(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    ghost = _entry(ana, tool="ghost")
    ghost = (ghost[0], 987654321, *ghost[2:])
    rows = _written(conn, [ghost, _entry(ana, tool="real")])
    assert [r["tool"] for r in rows] == ["real"]


def test_what_is_not_a_ref_never_reaches_the_database(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    nul = _entry(ana, tool="nul", refs=("ab\x00cdef0123",))
    assert [r["ref"] for r in _written(conn, [nul])] == [None]


def test_parse_ref_names_only_an_address_or_a_hex_suffix() -> None:
    uid = "01970000-0000-7000-8000-00000000abcd"
    full = f"ret:pabcdefghijklmnopqrstuvwxy2/{uid}"
    assert activity.parse_ref(full) == (full, None)
    assert activity.parse_ref(f"{full}:A") == (full, None)
    assert activity.parse_ref("542B1DBC") == (None, "542b1dbc")
    assert activity.parse_ref("542b1dbc:yes-ab12") == (None, "542b1dbc")
    assert activity.parse_ref("  542b1dbc, 99999999") == (None, "542b1dbc")
    for text in (
        "",
        "   ",
        "@ana",
        "pabcdefghijklmnopqrstuvwxy2",
        "a b",
        "12345",
        "ret:x",
    ):
        assert activity.parse_ref(text) == (None, None), text


# ------------------------------------------------------------------- partitions


def test_partitions_for_today_and_two_days_are_made_once(conn: Conn) -> None:
    now = datetime(2026, 11, 26, 13, 5, tzinfo=UTC)
    assert activity.maintain_in(conn, 30, now) == (3, 0)
    assert activity.maintain_in(conn, 30, now) == (0, 0)
    names = [
        r["relname"]
        for r in conn.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
            " WHERE i.inhparent = 'activity'::regclass ORDER BY 1"
        ).fetchall()
    ]
    assert names == ["activity_20261126", "activity_20261127", "activity_20261128"]
    # the bounds are whole UTC days
    bound = conn.execute(
        "SELECT pg_get_expr(relpartbound, oid) AS b FROM pg_class"
        " WHERE relname = 'activity_20261127'"
    ).fetchone()["b"]
    assert "2026-11-27 00:00:00+00" in bound and "2026-11-28 00:00:00+00" in bound
    # tomorrow it makes one more
    assert activity.maintain_in(conn, 30, now + timedelta(days=1)) == (1, 0)


def test_a_partition_whose_day_ended_more_than_the_days_ago_is_dropped_with_its_rows(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    now = datetime(2026, 11, 26, 13, 5, tzinfo=UTC)
    for back in (33, 31, 30, 29, 1):
        activity.create_partition(conn, now.date() - timedelta(days=back))
    for back in (33, 31, 30, 29, 1):
        at = datetime.combine(
            now.date() - timedelta(days=back), datetime.min.time(), UTC
        )
        conn.execute(
            "INSERT INTO activity (at, account_id, tool, outcome) VALUES (%s, %s, %s, 'ok')",
            (at + timedelta(hours=5), ana.id, f"day-{back}"),
        )
    # a table that merely starts with the name is not a partition of the log
    conn.execute("CREATE TABLE activity_keepme (x int)")
    # 30 days: the day that ended 30 days and 13 hours ago goes, the next stays
    assert activity.maintain_in(conn, 30, now) == (3, 2)
    kept = [r["tool"] for r in conn.execute("SELECT tool FROM activity").fetchall()]
    assert sorted(kept) == ["day-1", "day-29", "day-30"]
    assert conn.execute("SELECT to_regclass('activity_keepme') AS t").fetchone()["t"]
    # a shorter retention takes more
    assert activity.maintain_in(conn, 7, now) == (0, 2)
    assert [
        r["tool"] for r in conn.execute("SELECT tool FROM activity").fetchall()
    ] == ["day-1"]


def test_the_database_role_of_the_server_needs_no_superuser_right(conn: Conn) -> None:
    """It owns the table, and the owner may make and drop its partitions."""
    conn.execute("CREATE ROLE activity_owner NOSUPERUSER NOLOGIN")
    conn.execute("GRANT ALL ON SCHEMA public TO activity_owner")
    conn.execute("ALTER TABLE activity OWNER TO activity_owner")
    conn.execute("SET LOCAL ROLE activity_owner")
    now = datetime(2026, 11, 26, 13, 5, tzinfo=UTC)
    assert activity.maintain_in(conn, 30, now) == (3, 0)
    activity.create_partition(conn, date(2026, 10, 1))
    assert activity.maintain_in(conn, 30, now) == (0, 1)
    sup = conn.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert sup["rolsuper"] is False
    conn.execute("RESET ROLE")


def test_the_log_made_by_the_server_start_has_today_and_two_days_ahead(
    make_app: Callable[..., App],
) -> None:
    app = make_app(30)
    today = utc_today()
    names = {
        r["relname"]
        for r in sql(
            app.url,
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
            " WHERE i.inhparent = 'activity'::regclass",
        )
    }
    assert {
        activity.partition_name(today + timedelta(days=i)) for i in range(3)
    } <= names


def test_with_0_days_nothing_is_queued_written_or_made(
    make_app: Callable[..., App],
) -> None:
    app = make_app(0)
    assert app.log is None
    ana = app.person("ana@example.com", "ana")
    access = app.token(ana)
    assert app.call(access, "profile").startswith("@ana")
    assert app.call(access, "put", text="x").startswith("saved note")
    assert app.rows() == []
    assert not sql(
        app.url,
        "SELECT 1 FROM pg_inherits WHERE inhparent = 'activity'::regclass",
    )


def test_the_setting_is_a_whole_number_of_days_from_0_to_3650() -> None:
    assert Settings.from_env({}).activity_days == 30
    assert Settings.from_env({"RETICULA_ACTIVITY_DAYS": "0"}).activity_days == 0
    assert Settings.from_env({"RETICULA_ACTIVITY_DAYS": "3650"}).activity_days == 3650
    assert Settings.from_env({"RETICULA_ACTIVITY_DAYS": " 7 "}).activity_days == 7
    for bad in ("abc", "1.5", "-1", "3651", "30 days"):
        with pytest.raises(ReticulaError) as caught:
            Settings.from_env({"RETICULA_ACTIVITY_DAYS": bad})
        assert caught.value.code == BAD_INPUT
        assert "RETICULA_ACTIVITY_DAYS" in caught.value.message, bad
    assert MAX_ACTIVITY_DAYS == 3650


# ---------------------------------------------------------- queue and the writer


def test_a_full_queue_drops_and_counts_and_the_call_still_answers(
    make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activity, "QUEUE_SIZE", 2)
    monkeypatch.setattr(activity, "BATCH_ROWS", 1)
    inside, release = threading.Event(), threading.Event()
    seen: list[int] = []

    def stuck(conn: Conn, entries: Any) -> int:
        seen.append(len(entries))
        inside.set()
        release.wait(30)
        return len(entries)

    monkeypatch.setattr(activity, "write_batch", stuck)
    app = make_app(30)
    log = app.log
    assert log is not None
    ana = app.person("ana@example.com", "ana")
    access = app.token(ana)
    assert app.call(access, "profile").startswith("@ana")
    assert inside.wait(10)  # the writer holds the first row, writing
    for _ in range(4):  # two fit in the queue, two do not
        assert app.call(access, "profile").startswith("@ana")
    assert log.dropped == 2
    release.set()
    app.settle()
    assert log.written == 3
    assert log.dropped == 2


def test_a_failing_write_is_invisible_to_the_caller_and_warns_once_a_minute(
    app: App, caplog: pytest.LogCaptureFixture
) -> None:
    log = app.log
    assert log is not None
    ana = app.person("ana@example.com", "ana")
    access = app.token(ana)
    assert app.call(access, "profile").startswith("@ana")
    app.settle()
    assert (log.written, log.dropped) == (1, 0)
    today = activity.partition_name(utc_today())
    sql(app.url, f"DROP TABLE {today}")  # nothing to write to today
    with caplog.at_level(logging.WARNING, logger="reticula_mcp.activity"):
        for _ in range(3):
            assert app.call(access, "profile").startswith("@ana")
            app.settle()
    assert log.dropped == 3
    warned = [r for r in caplog.records if r.name == "reticula_mcp.activity"]
    assert len(warned) == 1
    assert warned[0].levelno == logging.WARNING
    assert "not written" in warned[0].getMessage()
    # the log goes on when the partition is back
    with connect(app.url) as c:
        activity.create_partition(c, utc_today())
        c.commit()
    assert app.call(access, "profile").startswith("@ana")
    app.settle()
    assert log.written == 2


def test_what_is_queued_is_written_when_the_server_stops(
    make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activity, "FLUSH_SECONDS", 60.0)
    app = make_app(30)
    ana = app.person("ana@example.com", "ana")
    access = app.token(ana)
    for _ in range(3):
        app.call(access, "profile")
    assert app.rows() == []  # still waiting for its second
    activity.stop(app.db)
    assert [r["tool"] for r in app.rows()] == ["profile"] * 3
    assert app.log is not None and not app.log.alive()


# ----------------------------------------------------------------------- lines


def _row(
    conn: Conn,
    account: Account,
    at: datetime,
    tool: str,
    outcome: str = "ok",
    client: str | None = "Claude (c_4f2a9e1b)",
    ref: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO activity (at, account_id, connection, client, tool, outcome, ref)"
        " VALUES (%s, %s, 'grant:g1', %s, %s, %s, %s)",
        (at, account.id, client, tool, outcome, ref),
    )


def test_lines_are_newest_first_with_the_documented_form(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana, bo = make_account(handle="ana"), make_account(handle="bob")
    activity.maintain_in(conn, 30, datetime(2026, 11, 26, 13, 0, tzinfo=UTC))
    day = datetime(2026, 11, 26, tzinfo=UTC)
    _row(conn, ana, day + timedelta(hours=12, minutes=3), "put", ref="542b1dbc")
    _row(conn, ana, day + timedelta(hours=12, minutes=9), "get", "NotFound", None)
    _row(conn, ana, day + timedelta(hours=1), "start")
    _row(conn, bo, day + timedelta(hours=12, minutes=4), "search")
    assert activity.lines(conn, ana.id) == [
        "2026-11-26 12:09 UTC · unknown client · get · NotFound",
        "2026-11-26 12:03 UTC · Claude (c_4f2a9e1b) · put · ok · 542b1dbc",
        "2026-11-26 01:00 UTC · Claude (c_4f2a9e1b) · start · ok",
    ]
    assert activity.lines(conn, bo.id) == [
        "2026-11-26 12:04 UTC · Claude (c_4f2a9e1b) · search · ok"
    ]
    assert activity.lines(conn, make_account(handle="cyrus").id) == []


def test_lines_limit_is_clamped_and_before_cuts_the_list(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    activity.maintain_in(conn, 30, datetime(2026, 11, 26, 13, 0, tzinfo=UTC))
    day = datetime(2026, 11, 26, tzinfo=UTC)
    for i in range(600):
        _row(conn, ana, day + timedelta(seconds=i), f"t{i}")
    assert len(activity.lines(conn, ana.id)) == 50
    assert len(activity.lines(conn, ana.id, limit=7)) == 7
    assert len(activity.lines(conn, ana.id, limit=0)) == 1
    assert len(activity.lines(conn, ana.id, limit=-5)) == 1
    assert len(activity.lines(conn, ana.id, limit=100000)) == 500
    assert activity.lines(conn, ana.id, limit=2)[0].endswith("· t599 · ok")
    cut = day + timedelta(seconds=100)  # rows older than this: t0 .. t99
    older = activity.lines(conn, ana.id, limit=3, before=cut)
    assert [ln.rsplit(" · ", 2)[1] for ln in older] == ["t99", "t98", "t97"]
    assert len(activity.lines(conn, ana.id, limit=500, before=cut)) == 100


def test_lines_use_the_account_and_time_index(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    now = datetime(2026, 11, 26, 13, 0, tzinfo=UTC)
    activity.maintain_in(conn, 30, now)
    conn.execute(
        "INSERT INTO activity (at, account_id, tool, outcome)"
        " SELECT %s + g * interval '1 second', %s, 't', 'ok' FROM generate_series(1, 20000) g",
        (datetime(2026, 11, 26, tzinfo=UTC), ana.id),
    )
    conn.execute("ANALYZE activity")
    plan = "\n".join(
        r["QUERY PLAN"]
        for r in conn.execute(
            "EXPLAIN SELECT at FROM activity WHERE account_id = %s"
            " ORDER BY at DESC LIMIT 50",
            (ana.id,),
        ).fetchall()
    )
    assert "activity_account_at_idx" in plan or "_account_id_at_idx" in plan, plan


# ----------------------------------------------------------- deleting an account


def _bare_account(conn: Conn, n: int) -> int:
    return conn.execute(
        "INSERT INTO account (person_id, email, password_hash, handle, identity_address)"
        " VALUES (%s, %s, '!', %s, %s) RETURNING id",
        (f"p-bare-{n}", f"bare{n}@example.com", f"bare{n}", f"ret:bare/{n}"),
    ).fetchone()["id"]


def test_deleting_an_account_removes_its_rows_and_only_its_rows(conn: Conn) -> None:
    gone, kept = _bare_account(conn, 1), _bare_account(conn, 2)
    activity.maintain_in(conn, 30)
    conn.execute(
        "INSERT INTO activity (at, account_id, tool, outcome)"
        " SELECT now(), a, 'get', 'ok' FROM unnest(%s::bigint[]) a, generate_series(1, 5)",
        ([gone, kept],),
    )
    assert conn.execute("SELECT count(*) AS n FROM activity").fetchone()["n"] == 10
    conn.execute("DELETE FROM account WHERE id = %s", (gone,))
    rows = conn.execute("SELECT DISTINCT account_id FROM activity").fetchall()
    assert [r["account_id"] for r in rows] == [kept]


def test_the_cascade_of_a_delete_over_30_partitions_and_31000_rows_takes_under_2_s(
    conn: Conn,
) -> None:
    others = [_bare_account(conn, n) for n in range(10, 15)]
    gone = _bare_account(conn, 99)
    today = utc_today()
    for back in range(30):
        activity.create_partition(conn, today - timedelta(days=back))
    midnight = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    conn.execute(
        f"INSERT INTO activity (at, account_id, connection, client, tool, outcome, ref)"
        f" SELECT {midnight} - (g %% 30) * interval '1 day' + interval '1 hour',"
        f" %s[1 + g %% 5], 'grant:g1', 'Claude (c_4f2a9e1b)', 'get', 'ok', '542b1dbc'"
        f" FROM generate_series(1, 30000) g",
        (others,),
    )
    conn.execute(
        f"INSERT INTO activity (at, account_id, connection, client, tool, outcome, ref)"
        f" SELECT {midnight} - (g %% 30) * interval '1 day' + interval '2 hours',"
        f" %s, 'grant:g1', 'Claude (c_4f2a9e1b)', 'get', 'ok', '542b1dbc'"
        f" FROM generate_series(1, 1000) g",
        (gone,),
    )
    conn.execute("ANALYZE activity")
    started = time.perf_counter()
    conn.execute("DELETE FROM account WHERE id = %s", (gone,))
    took = time.perf_counter() - started
    print(f"\ncascade delete of 1,000 rows among 31,000 in 30 partitions: {took:.3f} s")
    left = conn.execute("SELECT count(*) AS n FROM activity").fetchone()["n"]
    assert left == 30000
    assert took < 2.0


# ------------------------------------------------------------ events of the web page


def test_record_queues_a_row_for_an_event_of_the_web_page(app: App) -> None:
    ana = app.person("ana@example.com", "ana")
    activity.record(ana.id, "web sign-in")
    activity.record(ana.id, "web sign-in failed", outcome="Failed")
    activity.record(ana.id, "x" * 100)
    app.settle()
    rows = app.rows()
    assert [r["tool"] for r in rows] == ["web sign-in", "web sign-in failed", "x" * 40]
    assert [r["outcome"] for r in rows] == ["ok", "Failed", "ok"]
    for row in rows:
        assert set(row) == COLUMNS
        assert row["connection"] == "web"
        assert row["client"] is None and row["ref"] is None
        assert row["account_id"] == ana.id
    with connect(app.url) as c:
        shown = activity.lines(c, ana.id, limit=2)
    assert [ln.split(" · ", 1)[1] for ln in shown] == [
        "web · " + "x" * 40 + " · ok",
        "web · web sign-in failed · Failed",
    ]


def test_record_does_nothing_when_the_log_is_off_and_never_raises(
    make_app: Callable[..., App],
) -> None:
    app = make_app(0)
    ana = app.person("ana@example.com", "ana")
    activity.record(ana.id, "export")
    assert app.rows() == []
    assert not sql(
        app.url, "SELECT 1 FROM pg_inherits WHERE inhparent = 'activity'::regclass"
    )
    activity.record(ana.id, None)
    activity.record(ana.id, "export\x00\né")


def test_a_web_row_shows_web_in_place_of_the_client(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    activity.maintain_in(conn, 30, datetime(2026, 11, 26, 13, 0, tzinfo=UTC))
    at = datetime(2026, 11, 26, 12, 3, tzinfo=UTC)
    conn.execute(
        "INSERT INTO activity (at, account_id, connection, tool, outcome)"
        " VALUES (%s, %s, 'web', 'web sign-in', 'ok')",
        (at, ana.id),
    )
    assert activity.lines(conn, ana.id) == [
        "2026-11-26 12:03 UTC · web · web sign-in · ok"
    ]


# ------------------------------------------------------ an account that is deleted


@pytest.fixture(autouse=True)
def _forget_erased() -> Iterator[None]:
    yield
    activity._erased.clear()  # ids repeat from one test database to the next


def test_erase_removes_all_rows_of_the_account_and_no_other(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana, bo = make_account(handle="ana"), make_account(handle="bob")
    activity.maintain_in(conn, 30)
    for who in (ana, bo):
        conn.execute(
            "INSERT INTO activity (at, account_id, tool, outcome)"
            " SELECT now(), %s, 'get', 'ok' FROM generate_series(1, 5)",
            (who.id,),
        )
    activity.erase(conn, ana.id)
    assert activity.lines(conn, ana.id) == []
    assert conn.execute(
        "SELECT count(*) AS n FROM activity WHERE account_id = %s", (ana.id,)
    ).fetchone() == {"n": 0}
    assert len(activity.lines(conn, bo.id)) == 5


def test_a_row_queued_before_erase_is_not_written_after_it(
    make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activity, "FLUSH_SECONDS", 60.0)
    app = make_app(30)
    ana = app.person("ana@example.com", "ana")
    bo = app.person("bo@example.com", "bobby")
    for who in (ana, bo):
        access = app.token(who)
        app.call(access, "profile")
        app.call(access, "profile")
    with connect(app.url) as c:
        activity.erase(c, ana.id)
        c.commit()
    # what comes after is not queued either
    app.call(app.token(ana), "profile")
    activity.record(ana.id, "web sign-in")
    activity.stop(app.db)
    rows = app.rows()
    assert {r["account_id"] for r in rows} == {bo.id}
    assert len(rows) == 2


def test_a_disabled_account_gets_no_rows_through_the_writing_statement(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana, bo = make_account(handle="ana"), make_account(handle="bob")
    conn.execute("UPDATE account SET status = 'disabled' WHERE id = %s", (ana.id,))
    rows = _written(conn, [_entry(ana, tool="late"), _entry(bo, tool="fine")])
    assert [(r["account_id"], r["tool"]) for r in rows] == [(bo.id, "fine")]


def test_erase_time_over_30_partitions_and_31000_rows(conn: Conn) -> None:
    others = [_bare_account(conn, n) for n in range(10, 15)]
    gone = _bare_account(conn, 99)
    for back in range(30):
        activity.create_partition(conn, utc_today() - timedelta(days=back))
    midnight = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    conn.execute(
        "INSERT INTO activity (at, account_id, tool, outcome)"
        f" SELECT {midnight} - (g %% 30) * interval '1 day' + interval '1 hour',"
        " %s[1 + g %% 5], 'get', 'ok' FROM generate_series(1, 30000) g",
        (others,),
    )
    conn.execute(
        "INSERT INTO activity (at, account_id, tool, outcome)"
        f" SELECT {midnight} - (g %% 30) * interval '1 day' + interval '2 hours',"
        " %s, 'get', 'ok' FROM generate_series(1, 1000) g",
        (gone,),
    )
    conn.execute("ANALYZE activity")
    started = time.perf_counter()
    activity.erase(conn, gone)
    took = time.perf_counter() - started
    print(f"\nerase of 1,000 rows among 31,000 in 30 partitions: {took:.3f} s")
    assert conn.execute("SELECT count(*) AS n FROM activity").fetchone()["n"] == 30000
    assert took < 2.0
