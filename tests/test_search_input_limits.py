"""Limits and checks on what the tools take, applied before anything is looked
up: the size of a ref, the number of notes a put replaces and of its tags,
characters the database cannot store, invisible characters in titles and
tags, whole-string matching of identifiers, and who may resolve a deleted
address."""

from __future__ import annotations

import pytest

from reticula_mcp import format as fmt
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api
from reticula_mcp.graph import handlers as graph
from reticula_mcp.resolve import MAX_REF, resolve_ref
from reticula_mcp.search import handlers, read
from tests.graph_support import mk_hub, mk_note, share
from tests.helpers import make_atom
from tests.search_support import spy_on_sql, use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def refused(call, *args, **kwargs) -> ReticulaError:
    with pytest.raises(ReticulaError) as exc:
        call(*args, **kwargs)
    assert exc.value.code == "BadInput", exc.value.render()
    return exc.value


# ------------------------------------------------------------- the ref of get


def test_a_long_ref_is_refused_before_it_is_split(conn, make_account, monkeypatch):
    ada = make_account()

    def never(ref: str) -> list[str]:
        raise AssertionError("split_refs ran")

    monkeypatch.setattr(handlers, "split_refs", never)
    long = " ".join(["abcdef12"] * 300)
    error = refused(handlers.get, conn, ada, ref=long)
    assert f"the limit is {handlers.MAX_REF_CHARS}" in error.message


def test_refs_are_kept_once_each_in_their_order():
    assert handlers.split_refs("abcdef12 0123abcd, abcdef12 ffff0000") == [
        "abcdef12",
        "0123abcd",
        "ffff0000",
    ]


def test_ten_addresses_fit(conn, make_account):
    ada = make_account()
    notes = [mk_note(conn, ada, f"note {i}") for i in range(handlers.MAX_REFS)]
    out = handlers.get(conn, ada, ref=" ".join(notes))
    assert out.count("--- ") == handlers.MAX_REFS


# ------------------------------------------------------- what a put may name


def test_too_many_superseded_notes_are_refused_before_any_lookup(
    conn, make_account, monkeypatch
):
    ada = make_account()
    refs = [f"{i:08x}" for i in range(api.MAX_SUPERSEDES + 1)]
    sql = spy_on_sql(monkeypatch)
    error = refused(api.create_atom, conn, ada, kind="note", text="x", supersedes=refs)
    assert str(api.MAX_SUPERSEDES) in error.message
    assert sql.statements == []


def test_too_many_tags_are_refused_as_given(conn, make_account, monkeypatch):
    """Counted before duplicates are dropped and before any lookup."""
    ada = make_account()
    sql = spy_on_sql(monkeypatch)
    refused(
        api.create_atom,
        conn,
        ada,
        kind="note",
        text="x",
        tags=["x"] * (api.MAX_TAGS + 1),
    )
    assert sql.statements == []
    note = api.create_atom(conn, ada, kind="note", text="x", tags=["x"] * api.MAX_TAGS)
    assert note["tags"] == ["x"]


# ----------------------------------------- characters that cannot be stored


@pytest.mark.parametrize("bad", ["\x00", "\ud800"], ids=["nul", "lone surrogate"])
def test_unstorable_characters_are_refused_by_every_tool(conn, make_account, bad):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(conn, ada, "Kiln")
    note = mk_note(conn, ada, "kiln note")
    # a title or a tag refuses them as control or invisible characters
    for kw in ({"title": f"t{bad}"}, {"tags": [f"t{bad}"]}):
        error = refused(graph.put, conn, ada, text="a", **kw)
        assert f"U+{ord(bad):04X}" in error.message, error.message
    calls = [
        lambda: graph.put(conn, ada, text=f"a{bad}b"),
        lambda: graph.put(conn, ada, text="a", supersedes=[f"{note}{bad}"]),
        lambda: graph.put(conn, ada, text="a", anchor=f"x{bad}"),
        lambda: graph.edit(conn, ada, ref=note, text=f"a{bad}b"),
        lambda: graph.link(conn, ada, ref=note, to=f"{hub}{bad}"),
        lambda: graph.share(conn, ada, hub=hub, person=f"@ben{bad}"),
        lambda: handlers.search(conn, ada, query="kiln", kind=f"note{bad}"),
        lambda: handlers.get(conn, ada, ref=f"{note}{bad}"),
    ]
    for call in calls:
        error = refused(call)
        assert "cannot be stored" in error.message, error.message
    assert ben is not None
    # a query is cleaned instead, as before: the rest of it is searched
    assert "search" in handlers.search(conn, ada, query=f"kiln{bad}")


def test_a_page_offset_of_many_digits_is_past_the_end(conn, make_account):
    """get refuses so long a ref first; the offset itself is never made an
    integer of that size either."""
    assert read._split_offset("abcdef12+" + "9" * 5000) == ("abcdef12", read.MAX_OFFSET)
    ada = make_account()
    note = mk_note(conn, ada, "a short note")
    assert "no more" in handlers.get(conn, ada, ref=f"{note}+" + "9" * 1000)


# ------------------------------------------- invisible characters in titles


@pytest.mark.parametrize(
    "ch",
    ["‮", "⁦", "​", "⁠", "﻿", "­", ""],
    ids=[
        "rtl override",
        "isolate",
        "zero width space",
        "word joiner",
        "bom",
        "soft hyphen",
        "private use",
    ],
)
def test_an_invisible_character_in_a_title_or_tag_is_refused(conn, make_account, ch):
    ada = make_account()
    for kw in ({"title": f"Kiln{ch}log"}, {"tags": [f"kiln{ch}"]}):
        error = refused(api.create_atom, conn, ada, kind="note", text="x", **kw)
        assert f"U+{ord(ch):04X}" in error.message
    refused(api.create_atom, conn, ada, kind="hub", text="x", title=f"Hub{ch}")


def test_joiners_and_emoji_stay(conn, make_account):
    ada = make_account()
    family = "\U0001f468‍\U0001f469‍\U0001f467"  # a ZWJ sequence
    persian = "می‌خواهم"  # with a ZWNJ
    note = api.create_atom(
        conn, ada, kind="note", text="x", title=f"{family} {persian}", tags=[family]
    )
    assert note["title"] == f"{family} {persian}" and note["tags"] == [family]


def test_an_edit_checks_title_and_tags_the_same_way(conn, make_account):
    ada = make_account()
    note = make_atom(conn, ada, "x", title="Kiln")
    refused(api.update_atom, conn, ada, note["id"], title="Kiln‮")
    refused(api.update_atom, conn, ada, note["id"], tags=["a​"])


# --------------------------------------------- identifiers match as a whole


def test_an_identifier_with_a_trailing_newline_is_not_one():
    person = "p" + "a" * 26
    assert fmt.is_person_id(person) and not fmt.is_person_id(person + "\n")
    uuid = "01a2155b-9444-7f41-993f-558062db9dee"
    assert fmt.is_uuid7(uuid) and not fmt.is_uuid7(uuid + "\n")
    with pytest.raises(ValueError):
        fmt.parse_created("2026-11-26T10:00:00.000Z\n")
    with pytest.raises(ValueError):
        fmt.b64u_decode("abcd\n")


def test_a_reference_longer_than_any_address_is_refused(conn, make_account):
    ada = make_account()
    error = refused(resolve_ref, conn, ada, "a" * (MAX_REF + 1))
    assert str(MAX_REF) in error.message
    assert len(error.render()) < 400  # the reference is not echoed back


# ----------------------------------------------- a deleted address, resolved


def test_a_deleted_address_resolves_only_for_who_could_have_read_it(conn, make_account):
    ada, ben, cleo = (make_account(handle=h) for h in ("ada", "ben", "cleo"))
    hub = mk_hub(conn, ada, "Kiln")
    share(conn, ada, hub, ben, "read")
    note = mk_note(conn, ada, "kiln note", hub=hub)
    graph.delete(conn, ada, ref=note)
    for viewer in (ada, ben):
        assert resolve_ref(conn, viewer, note, allow_tombstoned=True)["address"] == note
    with pytest.raises(ReticulaError) as exc:
        resolve_ref(conn, cleo, note, allow_tombstoned=True)
    assert exc.value.code == "NotFound"
