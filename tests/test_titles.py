"""Titles are one line, at most 100 characters (200 for a paper), and a note's
own header in ``get`` shows all of it."""

from __future__ import annotations

import pytest

from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, handlers
from reticula_mcp.search import handlers as search
from tests.graph_support import addr, mk_hub, mk_note, node


def refused(call, *args, **kwargs) -> ReticulaError:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    assert e.value.code == "BadInput"
    return e.value


# --- one line ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("Two\nlines", "Two lines"),
        ("Tab\there", "Tab here"),
        ("Windows\r\nbreak", "Windows break"),
        ("  many   spaces  ", "many spaces"),
        ("\n\tAll kinds \x0b\x0c\x85 of breaks too\n", "All kinds of breaks too"),
    ],
)
def test_whitespace_in_a_title_becomes_one_space(conn, make_account, given, stored):
    z = make_account()
    note = addr(handlers.put(conn, z, text="x", title=given))
    assert node(conn, note)["title"] == stored


def test_an_edit_flattens_the_title_too(conn, make_account):
    z = make_account()
    note = mk_note(conn, z, "x")
    handlers.edit(conn, z, ref=note, title="New\n\ntitle\t ")
    assert node(conn, note)["title"] == "New title"


def test_a_hub_title_is_flattened(conn, make_account):
    z = make_account()
    hub = mk_hub(conn, z, "Hub\nwith a break")
    assert node(conn, hub)["title"] == "Hub with a break"


@pytest.mark.parametrize("ch", ["\x00", "\x07", "\x1b", "\x7f", "\x9b"])
def test_other_control_characters_are_refused_with_a_hint(conn, make_account, ch):
    z = make_account()
    err = refused(handlers.put, conn, z, text="x", title=f"bad{ch}title")
    assert f"U+{ord(ch):04X}" in err.message
    assert "one line" in (err.hint or "")
    note = mk_note(conn, z, "x")
    refused(handlers.edit, conn, z, ref=note, title=f"bad{ch}title")


def test_a_title_of_only_whitespace_is_no_title(conn, make_account):
    z = make_account()
    note = addr(handlers.put(conn, z, text="body line", title=" \n\t "))
    assert node(conn, note)["title"] is None
    refused(handlers.put, conn, z, text="x", kind="hub", title="\n \t")


def test_emoji_and_accents_are_kept(conn, make_account):
    z = make_account()
    title = "Café notes 👩‍🔬 and 日本語"
    note = addr(handlers.put(conn, z, text="x", title=title))
    assert node(conn, note)["title"] == title


# --- length --------------------------------------------------------------------


def test_a_note_or_hub_title_over_100_is_refused_naming_the_limit(conn, make_account):
    z = make_account()
    err = refused(handlers.put, conn, z, text="x", title="a" * 101)
    assert "100" in err.message and "100" in (err.hint or "")
    refused(handlers.put, conn, z, text="x", kind="hub", title="h" * 101)
    assert "saved" in handlers.put(conn, z, text="x", kind="hub", title="h" * 100)


def test_the_limit_counts_after_flattening(conn, make_account):
    z = make_account()
    title = "a" * 50 + "\n\n\n\n" + "b" * 49
    note = addr(handlers.put(conn, z, text="x", title=title))
    assert len(node(conn, note)["title"]) == 100


def test_a_paper_title_may_have_200(conn, make_account):
    z = make_account()
    out = handlers.put(
        conn, z, text="", kind="paper", title="p" * 200, ext_id="doi:10.1/long"
    )
    assert "saved" in out
    err = refused(
        handlers.put,
        conn,
        z,
        text="",
        kind="paper",
        title="p" * 201,
        ext_id="doi:10.1/x",
    )
    assert "200" in err.message


def test_an_edit_over_the_limit_is_refused(conn, make_account):
    z = make_account()
    hub = mk_hub(conn, z, "h")
    refused(handlers.edit, conn, z, ref=hub, title="h" * 101)
    assert node(conn, hub)["title"] == "h"


def test_an_existing_long_title_is_left_as_it_is(conn, make_account):
    """Titles written under the old limit are not rewritten, and an edit that
    does not touch the title keeps it."""
    z = make_account()
    note = mk_note(conn, z, "x")
    long = "L" * 150
    conn.execute("UPDATE node SET title = %s WHERE address = %s", (long, note))
    assert "updated" in handlers.edit(conn, z, ref=note, text="new text")


# --- get shows the whole title -------------------------------------------------


def test_get_shows_the_whole_title_in_its_own_header(conn, make_account):
    z = make_account()
    title = "A title of exactly one hundred characters, " + "x" * 57
    assert len(title) == 100
    note = addr(handlers.put(conn, z, text="body", title=title))
    head = search.get(conn, z, ref=note).splitlines()[0]
    assert f'"{title}"' in head


def test_get_shows_a_whole_paper_title(conn, make_account):
    z = make_account()
    title = "On the " + "very " * 30 + "long title"
    assert 100 < len(title) <= 200
    paper = addr(
        handlers.put(conn, z, text="", kind="paper", title=title, ext_id="doi:10.1/p")
    )
    head = search.get(conn, z, ref=paper).splitlines()[0]
    assert f'"{title}"' in head


def test_get_header_stays_bounded_for_a_stored_huge_title(conn, make_account):
    z = make_account()
    note = mk_note(conn, z, "x")
    conn.execute("UPDATE node SET title = %s WHERE address = %s", ("T" * 5000, note))
    head = search.get(conn, z, ref=note).splitlines()[0]
    assert len(head) < 300


def test_the_header_limit_matches_the_longest_title_a_write_accepts():
    assert api.MAX_PAPER_TITLE == 200
