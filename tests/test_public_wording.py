"""'public' is the public page, never a person: each place that lists or
counts the people of a hub leaves it out, and the ones that show the whole
access list say "the public page". One test per place (RETICULA1)."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from reticula_mcp.accounts import Account
from reticula_mcp.content.handlers import start
from reticula_mcp.db.pool import Conn
from reticula_mcp.graph import access
from reticula_mcp.graph import handlers as graph
from reticula_mcp.permissions import read_through_above_sql
from reticula_mcp.search.handlers import get
from reticula_mcp.web import hubs
from tests.graph_support import mk_hub, mk_note, share, sql_node_id


@pytest.fixture
def world(
    conn: Conn, make_account: Callable[..., Account]
) -> tuple[Account, Account, Account, str, str]:
    """ana owns a public hub with a sub-hub; bea reads it, cid adds to it."""
    ana, bea, cid = (make_account(handle=h) for h in ("ana", "bea", "cid"))
    hub = mk_hub(conn, ana, "Conference")
    sub = mk_hub(conn, ana, "Posters", parent=hub)
    mk_note(conn, ana, "x", hub=sub, title="Poster 1")
    share(conn, ana, hub, bea, "read")
    share(conn, ana, hub, cid, "add")
    graph.share(conn, ana, hub=hub, person="public", mode="read")
    return ana, bea, cid, hub, sub


def hub_line(out: str, title: str) -> str:
    return next(ln for ln in out.splitlines() if f'"{title}"' in ln and " id " in ln)


def test_start_names_the_public_page_and_counts_no_person(
    conn: Conn, world: tuple[Account, Account, Account, str, str]
) -> None:
    ana, _, cid, _, _ = world
    out = start(conn, ana)
    assert "shared with @bea, @cid, and the public page," in hub_line(out, "Conference")
    assert (
        "public" not in hub_line(out, "Posters").split("shared with")[1].split(",")[0]
    )
    assert "and the public page," in hub_line(out, "Posters")
    # a member who adds is told the people besides them: bea and the owner
    assert "you, the owner and 1 more have access," in hub_line(
        start(conn, cid), "Conference"
    )


def test_a_hub_shared_with_nobody_but_the_page(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    hub = mk_hub(conn, ana, "Open")
    graph.share(conn, ana, hub=hub, person="public", mode="read")
    assert "shared with the public page," in hub_line(start(conn, ana), "Open")


def test_readers_above_never_names_public(
    conn: Conn, world: tuple[Account, Account, Account, str, str]
) -> None:
    ana, _, _, _, sub = world
    above, _ = access.readers_above(conn, ana.person_id, sql_node_id(conn, sub))
    assert "public" not in {p["person"] for p in above}
    assert {p["handle"] for p in above} == {"bea", "cid"}


def test_get_access_line_says_the_public_page(
    conn: Conn, world: tuple[Account, Account, Account, str, str]
) -> None:
    ana, bea, _, hub, sub = world
    line = next(
        ln for ln in get(conn, ana, ref=hub).splitlines() if ln.startswith("access:")
    )
    assert (
        line == "access: @ana owner, @bea read, @cid add, the public page (titles only)"
    )
    sub_line = next(
        ln for ln in get(conn, ana, ref=sub).splitlines() if ln.startswith("access:")
    )
    assert sub_line.endswith(", the public page (titles only)")
    assert "public read" not in sub_line
    # a reader is shown only the owner and themselves, as before
    assert "public" not in next(
        ln for ln in get(conn, bea, ref=hub).splitlines() if ln.startswith("access:")
    )


def test_read_through_above_ignores_the_public_grant(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    hub = mk_hub(conn, ana, "Alone")
    sub = mk_hub(conn, ana, "Below", parent=hub)
    graph.share(conn, ana, hub=hub, person="public", mode="read")
    row = conn.execute(
        f"SELECT {read_through_above_sql('%(hub)s')} AS above",
        {"hub": sql_node_id(conn, sub)},
    ).fetchone()
    assert row is not None and row["above"] is False


def test_share_access_list_says_the_public_page(
    conn: Conn, world: tuple[Account, Account, Account, str, str]
) -> None:
    ana, bea, cid, hub, sub = world
    for who in (ana, cid):
        out = graph.share(conn, who, hub=hub)
        assert "public read" not in out and "granted by @ana)" in out
        assert (
            "the public page /hubs shows the titles of this hub and of the owner's"
            " own notes in it, to anyone"
        ) in out
    assert "public" not in graph.share(conn, ana, hub=sub).split("the public page")[0]
    assert "the public page" not in graph.share(conn, bea, hub=hub)


def test_an_owners_private_sub_hub_is_only_the_words_private_hub(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """Its title and short id appear nowhere in /hubs, and nothing filed under
    it does; a member's sub-hub and private notes have no placeholder."""
    ana, bea = make_account(handle="ana"), make_account(handle="bea")
    hub = mk_hub(conn, ana, "Lab")
    share(conn, ana, hub, bea, "add")
    secret = mk_hub(conn, ana, "SECRETHUBTITLE", parent=hub)
    graph.edit(conn, ana, ref=secret, private=True)
    inner = mk_note(conn, ana, "x", hub=secret, title="INNERNOTETITLE")
    deeper = mk_hub(conn, ana, "DEEPERHUBTITLE", parent=secret)
    hers = mk_hub(conn, bea, "BEASHUBTITLE", parent=hub)
    hidden = mk_note(conn, ana, "x", hub=hub, title="PRIVATENOTETITLE", private=True)
    graph.share(conn, ana, hub=hub, person="public", mode="read")
    body = hubs.body(conn)
    assert body.count('<span class="private">private hub</span>') == 1
    for word in (
        "SECRETHUBTITLE",
        "INNERNOTETITLE",
        "DEEPERHUBTITLE",
        "BEASHUBTITLE",
        "PRIVATENOTETITLE",
    ):
        assert word not in body, word
    for ref in (secret, inner, deeper, hers, hidden):
        assert ref[-8:] not in body, ref
    assert body.count("private hub") == 1
    # it is under the public hub, as a leaf
    assert re.search(r"<details><summary>Lab .*?<li><span class=\"private\">", body)
    # the access line of the private sub-hub does not claim the public page
    line = next(
        ln for ln in get(conn, ana, ref=secret).splitlines() if ln.startswith("access:")
    )
    assert "public page" not in line


def test_the_share_reply_says_how_much_it_opens(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana = make_account(handle="ana")
    hub = mk_hub(conn, ana, "Big")
    mk_note(conn, ana, "x", hub=hub, title="Top note")
    for i in range(7):
        s = mk_hub(conn, ana, f"Sub {i}", parent=hub)
        mk_note(conn, ana, "x", hub=s, title=f"Note {i}")
    closed = mk_hub(conn, ana, "Closed one", parent=hub)
    graph.edit(conn, ana, ref=closed, private=True)
    out = graph.share(conn, ana, hub=hub, person="public", mode="read")
    assert "is public now, with 8 notes of yours and 7 of your sub-hubs (" in out
    assert " and 2 more): anyone can see" in out
    assert out.count('"Sub ') == 5
    assert "Closed one" not in out
    assert (
        "Your private sub-hub shows as 'private hub', without title or id, and"
        " nothing in it is shown."
    ) in out
    single = mk_hub(conn, ana, "Small")
    mk_note(conn, ana, "x", hub=single, title="Only")
    small = graph.share(conn, ana, hub=single, person="public", mode="read")
    assert "is public now, with 1 note of yours: anyone can see" in small
    assert "private" not in small.split("nor private notes.")[1]


def test_only_the_owners_own_correction_takes_a_note_off(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    ana, bea = make_account(handle="ana"), make_account(handle="bea")
    hub = mk_hub(conn, ana, "Board")
    share(conn, ana, hub, bea, "add")
    kept = mk_note(conn, ana, "x", hub=hub, title="Kept by ana")
    theirs = mk_note(conn, bea, "y", hub=hub, title="Bea disagrees")
    graph.link(conn, bea, ref=theirs, to=kept, rel="supersedes")
    gone = mk_note(conn, ana, "x", hub=hub, title="Old by ana")
    newer = mk_note(conn, ana, "y", hub=hub, title="New by ana")
    graph.link(conn, ana, ref=newer, to=gone, rel="supersedes")
    graph.share(conn, ana, hub=hub, person="public", mode="read")
    body = hubs.body(conn)
    assert "Kept by ana" in body and "New by ana" in body
    assert "Old by ana" not in body and "Bea disagrees" not in body
