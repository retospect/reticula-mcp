"""When link files a note the writer has also filed in another hub, the reply
ends with that hub and the call that completes a move: withdraw it there."""

from __future__ import annotations

from reticula_mcp.graph import api, handlers
from tests.graph_support import mk_hub, mk_note, share


def hubs_of(conn, address: str) -> set[str]:
    rows = conn.execute(
        "SELECT h.address FROM membership m JOIN node h ON h.id = m.hub_node_id"
        " JOIN node n ON n.id = m.node_id WHERE n.address = %s AND NOT m.withdrawn",
        (address,),
    ).fetchall()
    return {r["address"] for r in rows}


def test_a_new_filing_names_the_other_hub_and_the_second_call(conn, make_account):
    z = make_account()
    old, new = mk_hub(conn, z, "Garden"), mk_hub(conn, z, "Kitchen")
    note = mk_note(conn, z, "x", title="Item", hub=old)
    out = handlers.link(conn, z, ref=note, to=new)
    assert out.splitlines()[-1] == (
        f'still filed in hub {old[-8:]} "Garden"; to move it, also run'
        f" link(ref='{note[-8:]}', to='{old[-8:]}', remove=true)"
    )


def test_the_call_in_the_line_completes_the_move(conn, make_account):
    z = make_account()
    old, new = mk_hub(conn, z, "Garden"), mk_hub(conn, z, "Kitchen")
    note = mk_note(conn, z, "x", hub=old)
    out = handlers.link(conn, z, ref=note, to=new)
    call = out.splitlines()[-1].split("also run ", 1)[1]
    ref = call.split("ref='", 1)[1].split("'", 1)[0]
    to = call.split("to='", 1)[1].split("'", 1)[0]
    handlers.link(conn, z, ref=ref, to=to, remove=True)
    assert hubs_of(conn, note) == {new}


def test_several_other_hubs_are_counted(conn, make_account):
    z = make_account()
    a, b, c = (mk_hub(conn, z, t) for t in ("A", "B", "C"))
    note = mk_note(conn, z, "x", hub=a)
    handlers.link(conn, z, ref=note, to=b)
    out = handlers.link(conn, z, ref=note, to=c)
    assert out.splitlines()[-1] == (
        f'still filed in 2 other hubs, among them hub {a[-8:]} "A"; to move it out'
        f" of that one, also run link(ref='{note[-8:]}', to='{a[-8:]}', remove=true)"
    )


def test_no_line_for_a_first_filing_or_a_repeat(conn, make_account):
    z = make_account()
    a = mk_hub(conn, z, "A")
    note = mk_note(conn, z, "x")
    assert "still filed" not in handlers.link(conn, z, ref=note, to=a)
    assert "still filed" not in handlers.link(conn, z, ref=note, to=a)


def test_only_the_writers_own_filings_are_named(conn, make_account):
    owner, member = make_account(), make_account()
    a, b = mk_hub(conn, owner, "A"), mk_hub(conn, owner, "B")
    share(conn, owner, a, member, "add")
    share(conn, owner, b, member, "add")
    note = mk_note(conn, owner, "x", hub=a)  # filed in A by its author
    assert "still filed" not in handlers.link(conn, member, ref=note, to=b)


def test_a_hub_the_writer_cannot_read_is_named_by_its_id_alone(conn, make_account):
    owner, z = make_account(), make_account()
    open_hub = api.create_atom(
        conn,
        owner,
        kind="hub",
        text="",
        title="Owner's private name",
        payload_extra={"accept": "anyone", "preset": "group"},
    )["address"]
    note = mk_note(conn, z, "x")
    handlers.link(conn, z, ref=note, to=open_hub)
    mine = mk_hub(conn, z, "Mine")
    out = handlers.link(conn, z, ref=note, to=mine)
    assert f"still filed in hub {open_hub[-8:]}; to move it" in out
    assert "Owner's private name" not in out
