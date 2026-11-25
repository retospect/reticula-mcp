"""link(ref, to=<new hub>, from_hub=<old hub>) moves a filing in one call:
the filing under from_hub is withdrawn and the one under to added, in one
transaction, refused as a whole if either half is refused."""

from __future__ import annotations

import pytest

from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers
from tests.graph_support import mk_hub, mk_note, share


def hubs_of(conn, address: str) -> set[str]:
    rows = conn.execute(
        "SELECT h.address FROM membership m JOIN node h ON h.id = m.hub_node_id"
        " JOIN node n ON n.id = m.node_id WHERE n.address = %s AND NOT m.withdrawn",
        (address,),
    ).fetchall()
    return {r["address"] for r in rows}


def refused(call, *args, **kwargs) -> ReticulaError:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value


def test_a_note_moves_in_one_call(conn, make_account):
    z = make_account()
    old, new = mk_hub(conn, z, "Old"), mk_hub(conn, z, "New")
    note = mk_note(conn, z, "x", title="Item", hub=old)
    out = handlers.link(conn, z, ref=note[-8:], to=new[-8:], from_hub=old[-8:])
    assert hubs_of(conn, note) == {new}
    assert out == (
        f'moved {note[-8:]} "Item" ({note}) from hub {old[-8:]} "Old" ({old})'
        f' to hub {new[-8:]} "New" ({new})'
    )


def test_a_hub_moves_and_its_contents_go_with_it(conn, make_account):
    z = make_account()
    a, b = mk_hub(conn, z, "A"), mk_hub(conn, z, "B")
    sub = mk_hub(conn, z, "Sub", parent=a)
    inside = mk_note(conn, z, "x", hub=sub)
    handlers.link(conn, z, ref=sub, to=b, from_hub=a)
    assert hubs_of(conn, sub) == {b}
    assert hubs_of(conn, inside) == {sub}


def test_other_filings_stay(conn, make_account):
    z = make_account()
    old, new, third = (mk_hub(conn, z, t) for t in ("Old", "New", "Third"))
    note = mk_note(conn, z, "x", hub=old)
    handlers.link(conn, z, ref=note, to=third)
    handlers.link(conn, z, ref=note, to=new, from_hub=old)
    assert hubs_of(conn, note) == {new, third}


def test_a_move_that_closes_a_cycle_is_refused_whole(conn, make_account):
    z = make_account()
    top, other = mk_hub(conn, z, "Top"), mk_hub(conn, z, "Other")
    sub = mk_hub(conn, z, "Sub", parent=top)
    err = refused(handlers.link, conn, z, ref=top, to=sub, from_hub=other)
    assert err.code in ("NotFound", "Conflict")
    handlers.link(conn, z, ref=top, to=other)
    err = refused(handlers.link, conn, z, ref=top, to=sub, from_hub=other)
    assert err.code == "Conflict"
    assert hubs_of(conn, top) == {other}  # the withdrawal was undone


def test_a_move_into_a_hub_the_writer_cannot_add_to_is_refused_whole(
    conn, make_account
):
    z, y = make_account(), make_account()
    mine = mk_hub(conn, z, "Mine")
    theirs = mk_hub(conn, y, "Theirs")
    share(conn, y, theirs, z, "read")
    note = mk_note(conn, z, "x", hub=mine)
    err = refused(handlers.link, conn, z, ref=note, to=theirs, from_hub=mine)
    assert err.code == "Forbidden"
    assert hubs_of(conn, note) == {mine}


def test_a_move_from_a_hub_it_is_not_in_is_refused_whole(conn, make_account):
    z = make_account()
    a, b, c = (mk_hub(conn, z, t) for t in ("A", "B", "C"))
    note = mk_note(conn, z, "x", hub=a)
    err = refused(handlers.link, conn, z, ref=note, to=c, from_hub=b)
    assert err.code == "NotFound"
    assert hubs_of(conn, note) == {a}


def test_the_writer_can_go_on_after_a_refused_move(conn, make_account):
    """The refusal rolls back to before the move only, not the whole call's
    transaction."""
    z = make_account()
    a, b, c = (mk_hub(conn, z, t) for t in ("A", "B", "C"))
    note = mk_note(conn, z, "x", hub=a)
    refused(handlers.link, conn, z, ref=note, to=c, from_hub=b)
    handlers.link(conn, z, ref=note, to=c, from_hub=a)
    assert hubs_of(conn, note) == {c}


def test_from_hub_needs_part_of_and_no_remove(conn, make_account):
    z = make_account()
    a, b = mk_hub(conn, z, "A"), mk_hub(conn, z, "B")
    note, other = mk_note(conn, z, "x", hub=a), mk_note(conn, z, "y")
    for kwargs in (
        {"to": other, "rel": "supports"},
        {"to": b, "remove": True},
    ):
        err = refused(handlers.link, conn, z, ref=note, from_hub=a, **kwargs)
        assert err.code == "BadInput"
    assert hubs_of(conn, note) == {a}


def test_from_hub_equal_to_to_is_refused(conn, make_account):
    z = make_account()
    a = mk_hub(conn, z, "A")
    note = mk_note(conn, z, "x", hub=a)
    err = refused(handlers.link, conn, z, ref=note, to=a, from_hub=a)
    assert err.code == "BadInput" and "leave out from_hub" in (err.hint or "")


def test_someone_elses_note_moves_only_their_own_statement(conn, make_account):
    owner, member = make_account(), make_account()
    a, b = mk_hub(conn, owner, "A"), mk_hub(conn, owner, "B")
    share(conn, owner, a, member, "add")
    share(conn, owner, b, member, "add")
    note = mk_note(conn, owner, "x", hub=a)
    handlers.link(conn, member, ref=note, to=a)  # the member's own filing too
    out = handlers.link(conn, member, ref=note, to=b, from_hub=a)
    assert "as your statement" in out
    assert a in hubs_of(conn, note)  # the author's filing stays
    assert b in hubs_of(conn, note)


def test_another_persons_hub_cannot_be_moved(conn, make_account):
    owner, member = make_account(), make_account()
    a, b = mk_hub(conn, owner, "A"), mk_hub(conn, owner, "B")
    share(conn, owner, a, member, "add")
    share(conn, owner, b, member, "add")
    sub = mk_hub(conn, owner, "Sub", parent=a)
    err = refused(handlers.link, conn, member, ref=sub, to=b, from_hub=a)
    assert err.code == "Forbidden"
    assert hubs_of(conn, sub) == {a}
