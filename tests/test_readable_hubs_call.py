"""The hubs a viewer may read are asked for once per tool call that only reads
(``permissions.read_call`` and ``permissions.readable_hub_ids``, migration
0204). What the keeping may do and what it must never do: outlive the call,
answer for another viewer, or be used by a tool that writes."""

from __future__ import annotations

from typing import Any

from reticula_mcp import accounts, permissions
from reticula_mcp.content import handlers as content_handlers
from reticula_mcp.db import connect
from reticula_mcp.graph import handlers as graph_handlers
from reticula_mcp.search import handlers as search_handlers
from tests.auth_support import run_mcp, start_live
from tests.conftest import PASSWORD
from tests.graph_support import mk_hub, mk_note, share, sql_node_id
from tests.search_support import SqlSpy, spy_on_sql


def asks(sql: SqlSpy) -> int:
    """How often the database was asked for somebody's readable hubs."""
    return sum("readable_hubs(" in statement for statement in sql.statements)


def shared_hub(conn, make_account):
    """Ada's hub, shared with Ben for reading."""
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(conn, ada, "Kiln")
    share(conn, ada, hub, ben, "read")
    return ada, ben, hub, sql_node_id(conn, hub)


# ------------------------------------------------------------- inside one call


def test_inside_a_read_call_the_hubs_are_asked_for_once(
    conn, make_account, monkeypatch
):
    _ada, ben, _hub, hub_id = shared_hub(conn, make_account)
    sql = spy_on_sql(monkeypatch)
    with permissions.read_call():
        first = permissions.readable_hub_ids(conn, ben)
        again = permissions.readable_hub_ids(conn, ben)
    assert hub_id in first and again == first
    assert asks(sql) == 1


def test_what_a_caller_does_to_the_list_does_not_reach_the_next_caller(
    conn, make_account
):
    ada, ben, _hub, hub_id = shared_hub(conn, make_account)
    private = sql_node_id(conn, mk_hub(conn, ada, "Ledger", preset="personal"))
    with permissions.read_call():
        # the answer of the first ask, and an answer from the kept list
        permissions.readable_hub_ids(conn, ben).append(private)
        assert private not in permissions.readable_hub_ids(conn, ben)
        permissions.readable_hub_ids(conn, ben).append(private)
        assert private not in permissions.readable_hub_ids(conn, ben)
        assert hub_id in permissions.readable_hub_ids(conn, ben)


def test_inside_one_read_call_the_hubs_are_those_of_its_first_ask(conn, make_account):
    """The stated meaning of the keeping: a share that somebody takes away in
    the middle of a read call takes effect at the next call."""
    ada, ben, hub, hub_id = shared_hub(conn, make_account)
    with permissions.read_call():
        assert hub_id in permissions.readable_hub_ids(conn, ben)
        share(conn, ada, hub, ben, "none")
        assert hub_id in permissions.readable_hub_ids(conn, ben)
    assert hub_id not in permissions.readable_hub_ids(conn, ben)


# ------------------------------------------------------ never beyond the call


def test_the_kept_list_ends_with_the_call(conn, make_account, monkeypatch):
    """Two calls on one thread, the share taken away between them."""
    ada, ben, hub, hub_id = shared_hub(conn, make_account)
    note = mk_note(conn, ada, "cone 6 glaze fires at 1222 C", hub=hub)
    with permissions.read_call():
        assert hub_id in permissions.readable_hub_ids(conn, ben)
        assert "1222" in search_handlers.get(conn, ben, ref=note)
    share(conn, ada, hub, ben, "none")
    sql = spy_on_sql(monkeypatch)
    with permissions.read_call():
        assert hub_id not in permissions.readable_hub_ids(conn, ben)
        assert (
            search_handlers.search(conn, ben, query="glaze", hub=None).count("1222")
            == 0
        )
    assert asks(sql) == 1  # asked anew, and once


def test_a_call_inside_a_call_keeps_its_own_list(conn, make_account):
    ada, ben, hub, hub_id = shared_hub(conn, make_account)
    with permissions.read_call():
        assert hub_id in permissions.readable_hub_ids(conn, ben)
        share(conn, ada, hub, ben, "none")
        with permissions.read_call():
            assert hub_id not in permissions.readable_hub_ids(conn, ben)
        # the outer call is still the outer call
        assert hub_id in permissions.readable_hub_ids(conn, ben)


def test_with_no_call_open_every_ask_goes_to_the_database(
    conn, make_account, monkeypatch
):
    """A tool that writes, a test that calls a handler, the indexer."""
    ada, ben, hub, hub_id = shared_hub(conn, make_account)
    sql = spy_on_sql(monkeypatch)
    before = permissions.readable_hub_ids(conn, ben)
    share(conn, ada, hub, ben, "none")
    sql.reset()
    after = permissions.readable_hub_ids(conn, ben)
    again = permissions.readable_hub_ids(conn, ben)
    assert hub_id in before and hub_id not in after and again == after
    assert asks(sql) == 2


# ------------------------------------------------------ never another viewer


def test_a_call_never_answers_one_viewer_with_the_hubs_of_another(conn, make_account):
    """Two viewers, one thread, one call, one after the other."""
    ada, ben, _hub, hub_id = shared_hub(conn, make_account)
    carol = make_account(handle="carol")
    private = sql_node_id(conn, mk_hub(conn, ada, "Ledger", preset="personal"))
    with permissions.read_call():
        hers = permissions.readable_hub_ids(conn, ada)
        his = permissions.readable_hub_ids(conn, ben)
        nobody = permissions.readable_hub_ids(conn, carol)
        assert {hub_id, private} <= set(hers)
        assert hub_id in his and private not in his
        assert hub_id not in nobody and private not in nobody
        # asked in the other order the answers are the same
        assert permissions.readable_hub_ids(conn, carol) == nobody
        assert permissions.readable_hub_ids(conn, ben) == his
        assert permissions.readable_hub_ids(conn, ada) == hers
    with permissions.read_call():  # and the other one first
        assert private not in permissions.readable_hub_ids(conn, ben)
        assert private in permissions.readable_hub_ids(conn, ada)


def test_the_list_is_what_the_sql_function_returns(conn, make_account):
    ada, ben, _hub, _hub_id = shared_hub(conn, make_account)
    for viewer in (ada, ben):
        rows = conn.execute(
            "SELECT hub_id FROM readable_hubs(%s)", (viewer.id,)
        ).fetchall()
        assert sorted(permissions.readable_hub_ids(conn, viewer)) == sorted(
            r["hub_id"] for r in rows
        )


# -------------------------------------------------------- the server's tools


def call(live: Any, token: str, tool: str, **args: Any) -> str:
    async def work(session: Any, _init: Any) -> str:
        result = await session.call_tool(tool, args)
        return "\n".join(c.text for c in result.content if hasattr(c, "text"))

    out: str = run_mcp(live, token, work)
    return out


def test_the_server_opens_a_call_for_tools_that_read_and_never_for_one_that_writes(
    fresh_db_url, monkeypatch
):
    """Every tool of the server, with a handler in its place that asks for the
    hubs twice: asked once means a call was open, twice means none was."""
    sql = spy_on_sql(monkeypatch)
    asked: dict[str, int] = {}

    def stand_in(name: str):
        def handler(conn: Any, viewer: Any, **params: Any) -> str:
            before = asks(sql)
            permissions.readable_hub_ids(conn, viewer)
            permissions.readable_hub_ids(conn, viewer)
            key = name if name != "share" else f"share, person={params.get('person')}"
            asked[key] = asks(sql) - before
            return "ok"

        return handler

    for module, names in (
        (content_handlers, ("start", "profile")),
        (search_handlers, ("search", "get")),
        (graph_handlers, ("put", "edit", "link", "delete", "share")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, stand_in(name))

    with connect(fresh_db_url) as c:
        ada = accounts.create_account(c, "ada@example.com", PASSWORD, "ada")
        token = accounts.mint_token(c, ada, "test")
        c.commit()
    with start_live(fresh_db_url) as live:
        for tool, args in (
            ("start", {}),
            ("profile", {}),
            ("search", {"query": "kiln"}),
            ("get", {"ref": "abcdef12"}),
            ("put", {"text": "a note"}),
            ("edit", {"ref": "abcdef12", "text": "a note, again"}),
            ("link", {"ref": "abcdef12", "to": "abcdef13", "rel": "cites"}),
            ("delete", {"ref": "abcdef12"}),
            ("share", {"hub": "abcdef12"}),
            ("share", {"hub": "abcdef12", "person": "@ben", "mode": "read"}),
        ):
            # "ok" is the stand-in's answer: the call reached the handler
            assert call(live, token, tool, **args) == "ok", tool
    assert asked == {
        "start": 1,
        "profile": 1,
        "search": 1,
        "get": 1,
        "share, person=None": 1,  # the access list is only read
        "put": 2,
        "edit": 2,
        "link": 2,
        "delete": 2,
        "share, person=@ben": 2,
    }


def test_through_the_server_a_share_taken_away_holds_at_the_next_call(fresh_db_url):
    with connect(fresh_db_url) as c:
        ada = accounts.create_account(c, "ada@example.com", PASSWORD, "ada")
        ben = accounts.create_account(c, "ben@example.com", PASSWORD, "ben")
        hers, his = accounts.mint_token(c, ada, "a"), accounts.mint_token(c, ben, "b")
        hub = mk_hub(c, ada, "Kiln")
        sub = mk_hub(c, ada, "Glazes", parent=hub)
        note = mk_note(c, ada, "cone 6 glaze fires at 1222 C", hub=sub)
        c.commit()
    with start_live(fresh_db_url) as live:
        # not shared yet: nothing of the note. ("ok" was asked here before and
        # matched two letters of the random person id in the echoed address.)
        assert "1222" not in call(live, his, "get", ref=note)
        assert "[error:NotFound]" in call(live, his, "get", ref=note)
        call(live, hers, "share", hub=hub, person="@ben", mode="read")
        # ada and ben share no hub yet: it waits for ben's yes (0304)
        assert "[error:NotFound]" in call(live, his, "get", ref=note)
        call(live, his, "share", hub=hub[-8:], person="@ben", mode="read")
        seen = call(live, his, "get", ref=note)
        assert "1222" in seen and '"Kiln" / "Glazes"' in seen
        assert "1222" in call(live, his, "search", query="glaze", hub=hub)
        call(live, hers, "share", hub=hub, person="@ben", mode="none")
        assert "[error:NotFound]" in call(live, his, "get", ref=note)
        assert "1222" not in call(live, his, "search", query="glaze")
        assert "[error:NotFound]" in call(live, his, "search", query="glaze", hub=hub)
