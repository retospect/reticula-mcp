"""Settings through one ref: edit(ref="settings", text="name: value").
Word handles per connection, listed per account; listed in profile."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from reticula_mcp import connection, permissions, resolve, settings
from reticula_mcp.accounts import Account
from reticula_mcp.content.handlers import profile
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search.handlers import get
from tests.graph_support import mk_note


@pytest.fixture
def ana(make_account: Callable[..., Account]) -> Account:
    return make_account(handle="ana")


def edit_settings(conn: Conn, viewer: Account, text: str, key: str = "grant:g1") -> str:
    with connection.during({"connection": key}):
        return graph.edit(conn, viewer, ref="settings", text=text)


def test_profile_lists_the_settings_and_the_call(conn: Conn, ana: Account) -> None:
    with connection.during({"connection": "grant:g1"}):
        out = profile(conn, ana)
    lines = out.splitlines()
    at = lines.index("settings:")
    assert lines[at + 1].startswith("- word handles: off (this connection): ")
    assert lines[at + 2].startswith("- listed: yes (this account): ")
    assert (
        lines[at + 3] == 'change one with edit(ref="settings", text="word handles: on")'
    )


def test_word_handles_per_connection_and_listed_per_account(
    conn: Conn, ana: Account
) -> None:
    out = edit_settings(conn, ana, "word handles: on\nlisted: no")
    assert out.startswith("Settings changed.")
    assert "- word handles: on (this connection)" in out
    assert "- listed: no (this account)" in out
    assert settings.listed(conn, ana) is False
    with connection.during({"connection": "token:5"}):
        other = profile(conn, ana)
    assert "- word handles: off (this connection)" in other
    assert "- listed: no (this account)" in other
    edit_settings(conn, ana, "Listed: YES; word handles : off")
    assert settings.listed(conn, ana) is True


def test_listed_is_yes_until_set(conn: Conn, ana: Account) -> None:
    assert settings.listed(conn, ana) is True


def test_the_sql_condition_leaves_out_who_set_listed_no(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """For a query that lists accounts: its count and limit then agree."""
    ana, bea, cid = (make_account(handle=h) for h in ("ana", "bea", "cid"))
    edit_settings(conn, bea, "listed: no")
    edit_settings(conn, cid, "listed: yes")
    rows = conn.execute(
        "SELECT a.handle FROM account a WHERE a.id = ANY(%s)"
        f" AND NOT ({settings.unlisted_sql('a.id')}) ORDER BY a.handle",
        ([ana.id, bea.id, cid.id],),
    ).fetchall()
    assert [r["handle"] for r in rows] == ["ana", "cid"]


def test_profile_says_what_each_value_does(conn: Conn, ana: Account) -> None:
    edit_settings(conn, ana, "listed: no")
    out = profile(conn, ana)
    assert (
        '- listed: no (this account): search(kind="person") leaves this account out;'
        " others can still share with it by its exact handle"
    ) in out


@pytest.mark.parametrize(
    "text", ["word handles: maybe", "colour: blue", "word handles", "", "  \n "]
)
def test_a_bad_line_is_bad_input_naming_the_settings(
    conn: Conn, ana: Account, text: str
) -> None:
    with pytest.raises(ReticulaError) as err:
        edit_settings(conn, ana, text)
    assert err.value.code == "BadInput"
    assert "word handles: off or on; listed: yes or no" in err.value.message


def test_settings_take_text_only(conn: Conn, ana: Account) -> None:
    with connection.during({"connection": "grant:g1"}), pytest.raises(ReticulaError):
        graph.edit(conn, ana, ref="settings", text="listed: no", title="x")


def test_word_handles_need_a_connection_listed_does_not(
    conn: Conn, ana: Account
) -> None:
    with pytest.raises(ReticulaError) as err:
        graph.edit(conn, ana, ref="settings", text="word handles: on")
    assert "per connection" in err.value.message
    assert "Settings changed." in graph.edit(
        conn, ana, ref="settings", text="listed: no"
    )


def test_editing_settings_never_opens_a_read_call_nor_resolves_a_ref(
    conn: Conn, ana: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("settings must not read notes")

    monkeypatch.setattr(graph, "resolve_ref", refuse)
    monkeypatch.setattr(permissions, "readable_hub_ids", refuse)
    assert "Settings changed." in edit_settings(conn, ana, "word handles: on")
    assert permissions._KEPT.get() is None


@pytest.mark.parametrize("ref", ["settings", "Settings", " SETTINGS "])
def test_settings_never_resolves_to_a_note(conn: Conn, ana: Account, ref: str) -> None:
    mk_note(conn, ana, "a note titled settings", title="settings")
    with pytest.raises(ReticulaError) as err:
        resolve.resolve_ref(conn, ana, ref)
    assert err.value.code == "BadInput"
    with pytest.raises(ReticulaError):
        get(conn, ana, ref=ref)
