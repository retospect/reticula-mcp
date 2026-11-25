"""A search inside given node ids: ``only_ids`` of ``find.hybrid_search`` and
``find.scope``. It is for a caller that has a set of its own, such as the
notes changed since a day. Asking for many matches and dropping those outside
the set misses what ranks below the cut."""

from __future__ import annotations

import pytest

from reticula_mcp.search import find
from tests.helpers import make_atom
from tests.search_support import (
    file_under,
    grant,
    hub_of,
    spy_on_sql,
    supersede,
    tombstone,
    use_hash_embedder,
)


@pytest.fixture(autouse=True)
def hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def found(conn, viewer, query: str, **kw) -> list[int]:
    return [h.node_id for h in find.hybrid_search(conn, viewer, query, **kw).hits]


def everything(conn, viewer, query: str, **kw) -> set[int]:
    """Hits and partial matches together."""
    res = find.hybrid_search(conn, viewer, query, **kw)
    return {h.node_id for h in res.hits} | {p.node_id for p in res.partial}


def test_a_search_inside_ids_finds_nothing_outside_them(conn, make_account):
    ada = make_account()
    notes = [
        make_atom(conn, ada, f"kiln firing log, entry {i}", title=f"Firing {i}")
        for i in range(6)
    ]
    inside = [notes[1]["id"], notes[4]["id"]]
    assert len(found(conn, ada, "kiln firing")) == 6
    assert sorted(found(conn, ada, "kiln firing", only_ids=inside)) == sorted(inside)
    # any sequence will do, in any order, with an id twice or an unknown one
    assert sorted(
        found(conn, ada, "kiln firing", only_ids=(*reversed(inside), inside[0], 10**12))
    ) == sorted(inside)


def test_a_note_below_the_cut_of_the_plain_search_is_found(conn, make_account):
    """What the argument is for."""
    ada = make_account()
    for i in range(12):
        make_atom(conn, ada, f"glaze recipe {i} with feldspar", title=f"Glaze {i}")
    ranked = found(conn, ada, "glaze feldspar", limit=12)
    assert len(ranked) == 12
    last = ranked[-1]
    assert last not in found(conn, ada, "glaze feldspar", limit=5)
    assert found(conn, ada, "glaze feldspar", limit=5, only_ids=[last]) == [last]


def test_without_the_argument_and_with_none_the_search_is_the_same(conn, make_account):
    ada = make_account()
    for i in range(4):
        make_atom(conn, ada, f"kiln shelf {i}", title=f"Shelf {i}")
    assert found(conn, ada, "kiln shelf", only_ids=None) == found(
        conn, ada, "kiln shelf"
    )


def test_inside_every_readable_id_the_search_is_the_plain_search(conn, make_account):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    for i in range(8):
        make_atom(conn, ada, f"glaze recipe {i} with feldspar", title=f"Glaze {i}")
        make_atom(conn, ben, f"glaze test tile {i}", title=f"Tile {i}")
    every = [r["id"] for r in conn.execute("SELECT id FROM node").fetchall()]
    for query in ("glaze feldspar", "glaze", "feldspar thermocouple"):
        plain = find.hybrid_search(conn, ada, query)
        inside = find.hybrid_search(conn, ada, query, only_ids=every)
        assert [(h.node_id, h.legs) for h in inside.hits] == [
            (h.node_id, h.legs) for h in plain.hits
        ], query
        assert (inside.partial, inside.more) == (plain.partial, plain.more), query


def test_an_empty_list_finds_nothing(conn, make_account):
    ada = make_account()
    make_atom(conn, ada, "kiln shelf wash", title="Wash")
    res = find.hybrid_search(conn, ada, "kiln shelf", only_ids=[])
    assert (res.hits, res.partial, res.more) == ([], [], 0)


def test_the_caller_need_not_have_checked_the_ids(conn, make_account):
    """An id the viewer may not read is not found, in no part of the answer."""
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    his = make_atom(conn, ben, "kiln shelf wash", title="Wash")
    hers = make_atom(conn, ada, "kiln shelf wash, second coat", title="Second coat")
    shared = make_atom(conn, ada, "kiln shelf wash, third coat", title="Third coat")
    hub = hub_of(conn, ada, "Studio")
    file_under(conn, shared, hub)
    grant(conn, hub, ben, "read")

    ids = [his["id"], hers["id"], shared["id"]]
    assert everything(conn, ben, "kiln shelf wash", only_ids=ids) == {
        his["id"],
        shared["id"],
    }
    res = find.hybrid_search(conn, ben, "kiln shelf wash", only_ids=[hers["id"]])
    assert (res.hits, res.partial, res.more) == ([], [], 0)
    # with one of the words only, too
    assert everything(conn, ben, "kiln thermocouple", only_ids=[hers["id"]]) == set()
    # its author finds it
    assert found(conn, ada, "kiln shelf wash", only_ids=[hers["id"]]) == [hers["id"]]


def test_a_replaced_note_is_found_only_when_history_is_asked_for(conn, make_account):
    ada = make_account()
    old = make_atom(conn, ada, "the kiln fires to cone 6", title="Cone")
    new = make_atom(conn, ada, "the kiln fires to cone 10", title="Cone")
    supersede(conn, new, old)
    both = [old["id"], new["id"]]
    assert found(conn, ada, "kiln cone", only_ids=both) == [new["id"]]
    # the newer note hides the older one also when it is not among the ids
    assert found(conn, ada, "kiln cone", only_ids=[old["id"]]) == []
    assert found(
        conn, ada, "kiln cone", only_ids=[old["id"]], include_superseded=True
    ) == [old["id"]]


def test_a_deleted_note_is_not_found(conn, make_account):
    ada = make_account()
    note = make_atom(conn, ada, "kiln shelf wash", title="Wash")
    assert found(conn, ada, "kiln shelf", only_ids=[note["id"]]) == [note["id"]]
    tombstone(conn, note)
    assert everything(conn, ada, "kiln shelf", only_ids=[note["id"]]) == set()


def test_hub_and_kind_still_apply(conn, make_account):
    ada = make_account()
    hub = hub_of(conn, ada, "Kiln studio")
    filed = make_atom(conn, ada, "kiln shelf wash", title="Wash")
    loose = make_atom(conn, ada, "kiln shelf posts", title="Posts")
    file_under(conn, filed, hub)
    ids = [filed["id"], loose["id"], hub["id"]]
    assert sorted(found(conn, ada, "kiln", only_ids=ids)) == sorted(ids)
    assert found(conn, ada, "kiln", only_ids=ids, hub_node_id=hub["id"]) == [
        filed["id"]
    ]
    assert found(conn, ada, "kiln", only_ids=ids, kind="hub") == [hub["id"]]
    assert found(conn, ada, "kiln", only_ids=[loose["id"]], hub_node_id=hub["id"]) == []


def test_notes_with_some_of_the_words_are_inside_the_ids_too(conn, make_account):
    ada = make_account()
    inside = make_atom(conn, ada, "the thermocouple was ordered", title="Order")
    outside = make_atom(conn, ada, "the thermocouple arrived", title="Delivery")
    query = "kiln thermocouple replaced"
    plain = find.hybrid_search(conn, ada, query)
    assert {p.node_id for p in plain.partial} == {inside["id"], outside["id"]}
    res = find.hybrid_search(conn, ada, query, only_ids=[inside["id"]])
    assert res.hits == [] and res.more == 0
    assert [p.node_id for p in res.partial] == [inside["id"]]


def test_who_may_read_is_asked_once_and_not_in_every_statement(
    conn, make_account, monkeypatch
):
    ada = make_account()
    notes = [
        make_atom(conn, ada, f"kiln firing log, entry {i}", title=f"Firing {i}")
        for i in range(6)
    ]
    inside = [notes[1]["id"], notes[4]["id"]]

    def asks(sql) -> list[str]:
        """The statements that ask who may read. The one that looks for notes
        without a vector is the index catching up, not the search."""
        return [
            " ".join(s.split())
            for s in sql.statements
            if ("current_nodes(" in s or "readable_nodes(" in s)
            and "FROM embedding e WHERE e.node_id = n.id" not in s
        ]

    find.hybrid_search(conn, ada, "kiln firing")  # vectors are written here
    sql = spy_on_sql(monkeypatch)
    find.hybrid_search(conn, ada, "kiln firing")
    plain = asks(sql)
    sql.reset()
    find.hybrid_search(conn, ada, "kiln firing", only_ids=inside)
    assert len(plain) > 3
    assert len(asks(sql)) == 1, asks(sql)
    assert "= ANY(%(ids)s)" in asks(sql)[0]
    assert sum(find.ONLY_IDS_SQL in s for s in sql.statements) >= len(plain)


def test_the_scope_names_the_ids_that_passed(conn, make_account):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    his = make_atom(conn, ben, "kiln shelf wash", title="Wash")
    hers = make_atom(conn, ada, "kiln shelf wash, second coat", title="Second coat")
    where, params = find.scope(
        conn,
        ben,
        hub_node_id=None,
        kind=None,
        include_superseded=False,
        only_ids=[hers["id"], his["id"], his["id"]],
    )
    assert params["only_ids"] == [his["id"]]
    assert find.ONLY_IDS_SQL in where and "current_nodes(" not in where
    rows = conn.execute(f"SELECT n.id FROM node n WHERE {where}", params).fetchall()
    assert [r["id"] for r in rows] == [his["id"]]

    where, params = find.scope(
        conn, ben, hub_node_id=None, kind=None, include_superseded=False
    )
    assert "only_ids" not in params and "current_nodes(" in where
