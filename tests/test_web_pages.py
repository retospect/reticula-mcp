"""The public pages: what /hubs shows after share(person="public"), what it
never shows, and the headers and addresses of every page."""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest

from reticula_mcp import accounts
from reticula_mcp.accounts import Account
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers as graph
from reticula_mcp.graph import materialize
from reticula_mcp.web import hubs
from reticula_mcp.web.html import CSP
from tests.auth_support import PASSWORD, start_live
from tests.graph_support import delete_now, mk_hub, mk_note, share


@pytest.fixture
def people(make_account: Callable[..., Account]) -> tuple[Account, Account, Account]:
    return (
        make_account(handle="ana"),
        make_account(handle="bea"),
        make_account(handle="cid"),
    )


def short(address: str) -> str:
    return address[-8:]


def public(conn: Conn, owner: Account, hub: str, mode: str = "read") -> str:
    return graph.share(conn, owner, hub=hub, person="public", mode=mode)


def test_only_the_owners_own_live_notes_and_sub_hubs_are_shown(
    conn: Conn, people: tuple[Account, Account, Account]
) -> None:
    ana, bea, _ = people
    hub = mk_hub(conn, ana, "Conference")
    share(conn, ana, hub, bea, "add")
    mine = mk_note(conn, ana, "NOTETEXTSHOWN never", hub=hub, title="Keynote notes")
    sub = mk_hub(conn, ana, "Posters", parent=hub)
    in_sub = mk_note(conn, ana, "x", hub=sub, title="Poster 12")
    untitled = mk_note(conn, ana, "UNTITLEDTEXT first line", hub=hub)
    theirs = mk_note(conn, bea, "x", hub=hub, title="Bea's comment")
    their_sub = mk_hub(conn, bea, "Bea's corner", parent=hub)
    secret = mk_note(conn, ana, "x", hub=hub, title="Private plan", private=True)
    gone = mk_note(conn, ana, "x", hub=hub, title="Deleted talk")
    delete_now(conn, ana, gone)
    old = mk_note(conn, ana, "x", hub=hub, title="Old schedule")
    new = mk_note(conn, ana, "x", hub=hub, title="New schedule")
    graph.link(conn, ana, ref=new, to=old, rel="supersedes")
    closed = mk_hub(conn, ana, "Closed hub")
    mk_note(conn, ana, "x", hub=closed, title="Closed note")

    assert hubs.body(conn).endswith("<p>No hub is public on this server yet.</p>")
    out = public(conn, ana, hub)
    assert out.startswith(f'hub {short(hub)} "Conference" (')
    assert "is public now" in out
    body = hubs.body(conn)
    for title, ref in (
        ("Conference", hub),
        ("Keynote notes", mine),
        ("Posters", sub),
        ("Poster 12", in_sub),
        ("New schedule", new),
        ("(untitled note)", untitled),
    ):
        assert f'{title} <span class="id">{short(ref)}</span>' in body, title
    for absent in (
        "Bea",
        "Private plan",
        "Deleted talk",
        "Old schedule",
        "Closed",
        "NOTETEXTSHOWN",
        "UNTITLEDTEXT",
        "@ana",
        "@bea",
        ana.person_id,
        bea.person_id,
        "ana@",
        "bea@",
    ):
        assert absent not in body, absent
    for ref in (theirs, their_sub, secret, gone, old, closed):
        assert short(ref) not in body
    # the sub-hub is a nested list under the hub
    assert re.search(
        r"<details><summary>Conference .*?<ul>.*<details><summary>Posters", body
    )
    # off again
    assert "no longer on the public page" in public(conn, ana, hub, "none")
    assert "No hub is public" in hubs.body(conn)


def test_titles_are_escaped(
    conn: Conn, people: tuple[Account, Account, Account]
) -> None:
    ana = people[0]
    hub = mk_hub(conn, ana, '<script>alert(1)</script> & "quotes"')
    mk_note(conn, ana, "x", hub=hub, title="<img src=x onerror=alert(2)>")
    public(conn, ana, hub)
    body = hubs.body(conn)
    assert "<script>" not in body and "<img" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot;" in body
    assert "&lt;img src=x onerror=alert(2)&gt;" in body


def test_only_the_owner_makes_a_hub_public_and_only_to_read(
    conn: Conn, people: tuple[Account, Account, Account]
) -> None:
    ana, bea, _ = people
    hub = mk_hub(conn, ana, "Board")
    share(conn, ana, hub, bea, "add")
    with pytest.raises(ReticulaError) as err:
        public(conn, bea, hub)
    assert err.value.code == "Forbidden"
    with pytest.raises(ReticulaError) as err:
        public(conn, ana, hub, "add")
    assert err.value.code == "BadInput"
    private_hub = mk_hub(conn, ana, "Diary")
    graph.edit(conn, ana, ref=private_hub, private=True)
    with pytest.raises(ReticulaError) as err:
        public(conn, ana, private_hub)
    assert "private hub cannot be public" in err.value.message
    # "@public" is a handle, never the public page
    with pytest.raises(ReticulaError) as err:
        graph.share(conn, ana, hub=hub, person="@public", mode="read")
    assert err.value.code == "NotFound"


def test_a_public_grant_gives_no_account_anything(
    conn: Conn, people: tuple[Account, Account, Account]
) -> None:
    ana, _, cid = people
    hub = mk_hub(conn, ana, "Open")
    note = mk_note(conn, ana, "x", hub=hub, title="Open note")
    public(conn, ana, hub)
    with pytest.raises(ReticulaError):
        graph.edit(conn, cid, ref=note, text="y")
    from reticula_mcp.search.handlers import get

    with pytest.raises(ReticulaError) as err:
        get(conn, cid, ref=note)
    assert err.value.code == "NotFound"


def test_a_rebuild_keeps_the_public_grant(
    conn: Conn, people: tuple[Account, Account, Account]
) -> None:
    ana = people[0]
    hub = mk_hub(conn, ana, "Kept")
    mk_note(conn, ana, "x", hub=hub, title="Kept note")
    public(conn, ana, hub)
    before = hubs.body(conn)
    materialize.rebuild(conn)
    assert hubs.body(conn) == before


def test_the_handle_public_is_reserved() -> None:
    with pytest.raises(ReticulaError):
        accounts.validate_handle("public")


PAGES = ("/", "/slides", "/hubs", "/connect", "/vision", "/privacy")


def test_every_page_over_http(fresh_db_url: str) -> None:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        hub = mk_hub(conn, ana, "Lab")
        mk_note(conn, ana, "x", hub=hub, title="Furnace log")
        graph.share(conn, ana, hub=hub, person="public", mode="read")
        conn.commit()
    with start_live(fresh_db_url) as live, httpx.Client(base_url=live.url) as client:
        for path in PAGES:
            r = client.get(path)
            assert r.status_code == 200, path
            assert r.headers["content-security-policy"] == CSP, path
            assert r.headers["x-content-type-options"] == "nosniff"
            assert "<script" not in r.text.lower(), path
            assert "@ana" not in r.text and ana.person_id not in r.text, path
            assert 'href="/static/site.css"' in r.text
        assert "Furnace log" in client.get("/hubs").text
        assert client.get("/hubs").headers["cache-control"] == "no-cache"
        connect_page = client.get("/connect").text
        assert (
            f"claude mcp add --transport http reticula {live.url}/mcp" in connect_page
        )
        css = client.get("/static/site.css")
        assert css.status_code == 200 and css.headers["content-type"].startswith(
            "text/css"
        )
        assert client.get("/robots.txt").text.startswith("User-agent: *")
        assert client.get("/no-such-page").status_code == 404
        # the connector still needs a token
        assert client.post("/mcp", json={}).status_code == 401
        # the slides: four sections, inline SVG, Mermaid under each, reticula.to
        slides = client.get("/slides").text
        assert slides.count('<section class="slide') == 4
        assert slides.count("<svg") == 4
        assert slides.count("Mermaid source of the diagram") == 4
        assert slides.count('href="https://reticula.to"') >= 4
