"""Filing someone else's atom under a hub: it organises, it never confers access."""

from __future__ import annotations

import pytest

from reticula_mcp import format as fmt
from reticula_mcp import records
from reticula_mcp.content import handlers as content_handlers
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import access, handlers
from reticula_mcp.graph.materialize import apply_record, rebuild
from reticula_mcp.search import handlers as search_handlers
from tests.graph_support import (
    can_read,
    insert_membership,
    mk_hub,
    mk_note,
    node,
    readable,
    share,
    sql_node_id,
)
from tests.test_graph_records import snapshot


def code_of(call, *args, **kwargs) -> str:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value.code


def statements(conn, atom: str, hub: str) -> list[tuple[str, bool]]:
    """(author person id, withdrawn) of every statement about atom in hub."""
    return [
        (r["author"], r["withdrawn"])
        for r in conn.execute(
            "SELECT m.author, m.withdrawn FROM membership m"
            " JOIN node a ON a.id = m.node_id JOIN node h ON h.id = m.hub_node_id"
            " WHERE a.address = %s AND h.address = %s ORDER BY m.id",
            (atom, hub),
        )
    ]


def test_mastodon_three_notes_synthesised_into_a_hub(conn, make_account):
    a, b, c = (make_account() for _ in range(3))
    # A and C each keep a note in a hub they share (read only) with B
    ha, hc = mk_hub(conn, a, "A's reading"), mk_hub(conn, c, "C's reading")
    share(conn, a, ha, b, "read")
    share(conn, c, hc, b, "read")
    note_a = mk_note(conn, a, "mastodon note from A", hub=ha)
    note_c = mk_note(conn, c, "mastodon note from C", hub=hc)
    # B synthesises a hub, files the others' notes and their own, and shares it
    hub = mk_hub(conn, b, "Mastodon")
    note_b = mk_note(conn, b, "mastodon note from B", hub=hub)
    out_a = handlers.link(conn, b, ref=note_a, to=hub)
    out_c = handlers.link(conn, b, ref=note_c, to=hub)
    assert "will not see it" in out_a and "only its author" in out_a
    assert "will not see it" in out_c
    share(conn, b, hub, a)
    share(conn, b, hub, c)
    # A sees their own note, B's hub and B's note; not C's note
    seen = readable(conn, a)
    assert {note_a, hub, note_b} <= seen
    assert note_c not in seen
    assert note_a not in readable(conn, c)
    # the hub listing for A does not hint at C's note either
    listing = search_handlers.get(conn, a, ref=hub)
    assert "mastodon note from C" not in listing and note_c not in listing
    assert "mastodon note from B" in listing and "mastodon note from A" in listing
    # C adopts the placement: from then on hub members read it
    handlers.link(conn, c, ref=note_c, to=hub)
    assert can_read(conn, a, note_c)
    assert "mastodon note from C" in search_handlers.get(conn, a, ref=hub)
    # A's own note is still not readable by C until A adopts it as well
    assert not can_read(conn, c, note_a)
    handlers.link(conn, a, ref=note_a, to=hub)
    assert can_read(conn, c, note_a)


def test_non_author_filing_confers_no_access_through_any_route(conn, make_account):
    a, b, d = (make_account() for _ in range(3))
    pool = mk_hub(conn, a, "A and B")
    share(conn, a, pool, b)
    secret = mk_note(conn, a, "zebrafish confidential words", hub=pool)
    mine = mk_hub(conn, b, "B's hub")
    share(conn, b, mine, d)
    handlers.link(conn, b, ref=secret, to=mine)
    assert statements(conn, secret, mine) == [(b.person_id, False)]
    # readable_nodes
    assert not can_read(conn, d, secret)
    assert can_read(conn, d, mine)
    # get: by address and by suffix, and the hub's own page
    for ref in (secret, secret[-8:]):
        assert code_of(search_handlers.get, conn, d, ref=ref) == "NotFound"
    page = search_handlers.get(conn, d, ref=mine)
    assert "zebrafish" not in page and secret not in page and secret[-8:] not in page
    page = search_handlers.get(conn, d, ref=mine, history=True, depth=3)
    assert "zebrafish" not in page and secret not in page
    # search, with and without the hub scope
    for hub in (None, mine):
        found = search_handlers.search(conn, d, query="zebrafish", hub=hub)
        assert "no results" in found and secret not in found
    # start does not count it
    start_d = content_handlers.start(conn, d)
    assert "zebrafish" not in start_d
    assert "0 notes" in next(ln for ln in start_d.splitlines() if "B's hub" in ln)
    start_b = content_handlers.start(conn, b)
    assert "1 note" in next(ln for ln in start_b.splitlines() if "B's hub" in ln)
    # B (who can read it) does see it in the hub
    assert "zebrafish" in search_handlers.get(conn, b, ref=mine)
    # and D cannot get it by superseding or linking it either
    assert code_of(handlers.link, conn, d, ref=secret, to=mine) == "NotFound"


def test_non_author_cannot_file_a_hub_nor_move_grants(conn, make_account):
    a, b, d = (make_account() for _ in range(3))
    top = mk_hub(conn, a, "Top")
    other = mk_hub(conn, a, "Other")
    share(conn, a, top, b, "read")
    share(conn, a, other, b, "add")
    mine = mk_hub(conn, b, "B's hub")
    share(conn, b, mine, d)
    # B can read A's hub `top` but may not hang it under B's hub
    out = code_of(handlers.link, conn, b, ref=top, to=mine)
    assert out == "Forbidden"
    with pytest.raises(ReticulaError) as e:
        handlers.link(conn, b, ref=other, to=mine)
    assert "own hubs" in e.value.message
    assert not can_read(conn, d, top) and not can_read(conn, d, other)
    # nor under another hub B can add to
    assert code_of(handlers.link, conn, b, ref=top, to=other) == "Forbidden"
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM membership WHERE author = %s", (b.person_id,)
        ).fetchone()["n"]
        == 0
    )
    # a statement signed straight into the log is not applied either
    rec = records.sign_and_append(
        conn,
        b,
        type="member",
        address=fmt.new_address(b.person_id),
        payload={"atom": top, "hub": mine, "withdrawn": False},
        hub_id=fmt.hub_id_from_address(mine),
    )
    assert apply_record(conn, rec) is None
    assert statements(conn, top, mine) == []
    # ... and a row forced into the table changes neither access nor the cycle check
    insert_membership(conn, top, mine, b)
    assert not can_read(conn, d, top)
    assert access.hub_access(conn, d.person_id, sql_node_id(conn, top)) is None
    assert access.hub_access(conn, d.person_id, sql_node_id(conn, mine)) == "add"
    mine_id = sql_node_id(conn, mine)
    # a non-author statement about an ordinary atom never touches the hierarchy
    note = mk_note(conn, a, "n", hub=other)
    handlers.link(conn, b, ref=note, to=mine)
    assert not access.closes_cycle(conn, mine_id, sql_node_id(conn, other))
    assert access.hub_access(conn, d.person_id, sql_node_id(conn, other)) is None
    assert not can_read(conn, d, note)
    grants_before = conn.execute("SELECT count(*) AS n FROM hub_grant").fetchone()["n"]
    assert grants_before == 3  # b on top, b on other, d on mine


def test_withdrawal_rights(conn, make_account):
    a, b, c, e = (make_account() for _ in range(4))
    hub = mk_hub(conn, b, "B's hub")
    share(conn, b, hub, c)
    share(conn, b, hub, e)
    pool = mk_hub(conn, a, "pool")
    for p in (b, c, e):
        share(conn, a, pool, p, "read")
    note = mk_note(conn, a, "a's note", hub=pool)
    # c files a's note; e (a member, not the filer) may not withdraw it
    handlers.link(conn, c, ref=note, to=hub)
    assert code_of(handlers.link, conn, e, ref=note, to=hub, remove=True) == "Forbidden"
    assert statements(conn, note, hub) == [(c.person_id, False)]
    # the statement's own author may
    handlers.link(conn, c, ref=note, to=hub, remove=True)
    assert statements(conn, note, hub) == [(c.person_id, True)]
    # the hub's admin may withdraw someone else's statement
    handlers.link(conn, c, ref=note, to=hub)
    assert statements(conn, note, hub) == [
        (c.person_id, False)
    ]  # same statement, revived
    handlers.link(conn, b, ref=note, to=hub, remove=True)
    assert statements(conn, note, hub)[0] == (c.person_id, True)
    # the atom's author may withdraw any statement about their own atom
    handlers.link(conn, c, ref=note, to=hub)
    assert statements(conn, note, hub)[0] == (c.person_id, False)
    assert code_of(handlers.link, conn, a, ref=note, to=hub, remove=True) == "NotFound"
    share(conn, b, hub, a, "read")
    handlers.link(conn, a, ref=note, to=hub, remove=True)
    assert all(w for _, w in statements(conn, note, hub))
    # nothing left to withdraw
    assert code_of(handlers.link, conn, a, ref=note, to=hub, remove=True) == "NotFound"


def test_author_and_non_author_statements_do_not_block_each_other(conn, make_account):
    a, b, c = (make_account() for _ in range(3))
    hub = mk_hub(conn, b, "B's hub")
    share(conn, b, hub, a)
    share(conn, b, hub, c, "read")
    pool = mk_hub(conn, a, "pool")
    share(conn, a, pool, b, "read")
    # order one: non-author first, author after
    n1 = mk_note(conn, a, "first order", hub=pool)
    assert "filed" in handlers.link(conn, b, ref=n1, to=hub)
    assert "already filed" in handlers.link(conn, b, ref=n1, to=hub)  # idempotent
    assert not can_read(conn, c, n1)
    out = handlers.link(conn, a, ref=n1, to=hub)
    assert out.startswith("filed")  # the author's own statement is not "already"
    assert can_read(conn, c, n1)
    assert len(statements(conn, n1, hub)) == 2
    assert "already filed" in handlers.link(conn, a, ref=n1, to=hub)
    # order two: author first, non-author after
    n2 = mk_note(conn, a, "second order", hub=hub)
    assert can_read(conn, c, n2)
    out = handlers.link(conn, b, ref=n2, to=hub)
    assert out.startswith("filed")
    assert len(statements(conn, n2, hub)) == 2
    assert can_read(conn, c, n2)
    # the author withdrawing removes the placement entirely (both statements)
    handlers.link(conn, a, ref=n2, to=hub, remove=True)
    assert all(w for _, w in statements(conn, n2, hub))
    assert not can_read(conn, c, n2)
    # a non-author withdrawing only their own statement leaves the author's intact
    # (c holds add on a second hub where the author has filed n1 too)
    other = mk_hub(conn, b, "Other")
    share(conn, b, other, c, "add")
    share(conn, b, other, a, "add")
    n3 = mk_note(conn, a, "third", hub=other)
    handlers.link(conn, c, ref=n3, to=other)  # c reads n3 as a member of `other`
    assert len(statements(conn, n3, other)) == 2
    handlers.link(conn, c, ref=n3, to=other, remove=True)
    assert (c.person_id, True) in statements(conn, n3, other)
    assert (a.person_id, False) in statements(conn, n3, other)
    assert can_read(conn, b, n3)


def test_rebuild_reproduces_non_author_statements(conn, make_account):
    a, b, c = (make_account() for _ in range(3))
    hub = mk_hub(conn, b, "B's hub")
    share(conn, b, hub, a)
    share(conn, b, hub, c, "read")
    pool = mk_hub(conn, a, "pool")
    share(conn, a, pool, b, "read")
    n1 = mk_note(conn, a, "one", hub=pool)
    n2 = mk_note(conn, a, "two", hub=pool)
    n3 = mk_note(conn, a, "three", hub=pool)
    for n in (n1, n2, n3):
        handlers.link(conn, b, ref=n, to=hub)
    handlers.link(conn, a, ref=n1, to=hub)  # adoption
    handlers.link(conn, b, ref=n2, to=hub, remove=True)  # own withdrawal
    handlers.link(conn, b, ref=n3, to=hub, remove=True)
    handlers.link(conn, b, ref=n3, to=hub)  # revived
    handlers.link(conn, a, ref=n3, to=hub, remove=True)  # author withdraws
    before = snapshot(conn)
    people = {p.handle: readable(conn, p) for p in (a, b, c)}
    rebuild(conn)
    assert snapshot(conn) == before
    assert {p.handle: readable(conn, p) for p in (a, b, c)} == people
    for table in ("link", "supersedes", "membership", "hub_grant", "node"):
        conn.execute(f"DELETE FROM {table}")
    rebuild(conn)
    assert snapshot(conn) == before
    assert {p.handle: readable(conn, p) for p in (a, b, c)} == people
    assert node(conn, n1)["text"] == "one"


def test_hub_listing_and_start_count_only_what_the_viewer_reads(conn, make_account):
    a, b, d = (make_account() for _ in range(3))
    pool = mk_hub(conn, a, "pool")
    share(conn, a, pool, b, "read")
    mine = mk_hub(conn, b, "B's hub")
    share(conn, b, mine, d)
    own = mk_note(conn, b, "b's own", hub=mine)
    hidden = [mk_note(conn, a, f"hidden {i}", hub=pool) for i in range(3)]
    for n in hidden:
        handlers.link(conn, b, ref=n, to=mine)
    start_d = content_handlers.start(conn, d)
    line = next(ln for ln in start_d.splitlines() if "B's hub" in ln)
    assert "1 note" in line and "4 notes" not in line
    page = search_handlers.get(conn, d, ref=mine)
    assert "b's own" in page and "hidden" not in page
    assert own in readable(conn, d)
    assert not set(hidden) & readable(conn, d)
