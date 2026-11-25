"""Adversarial tests for readable_nodes, can_add and the grant rules: try to
read what should be hidden through every route."""

from __future__ import annotations

import itertools
import random

import pytest

from reticula_mcp import accounts, permissions, records
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import access, api, handlers
from reticula_mcp.graph.materialize import apply_record
from tests.graph_support import (
    addr,
    can_read,
    insert_membership,
    mk_hub,
    mk_note,
    node,
    readable,
    share,
    sql_node_id,
)


def code_of(call, *args, **kwargs) -> str:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value.code


# --- debug hub and the implicit admin --------------------------------------------


def test_debug_note_lands_in_the_instance_debug_hub(conn, make_account):
    system = accounts.ensure_system_account(conn)
    debug_hub = api.create_atom(
        conn,
        system,
        kind="hub",
        text="debug intake",
        title="Debug",
        tags=["slug:hub-debug"],
        payload_extra={"accept": "anyone", "preset": "group"},
    )["address"]
    alice, bob = make_account(), make_account()
    # an imposter's hub with the same tag is not the debug hub
    api.create_atom(
        conn, bob, kind="hub", text="fake", title="Fake", tags=["slug:hub-debug"]
    )
    out = handlers.put(conn, alice, text="it broke", kind="debug", model="some-model")
    note = addr(out)
    assert debug_hub in out
    member = conn.execute(
        "SELECT h.address FROM membership m JOIN node h ON h.id = m.hub_node_id"
        " JOIN node n ON n.id = m.node_id WHERE n.address = %s AND NOT m.withdrawn",
        (note,),
    ).fetchone()
    assert member["address"] == debug_hub
    assert can_read(conn, alice, note)
    assert not can_read(conn, bob, note)
    assert can_read(conn, system, note)
    assert not can_read(conn, alice, debug_hub)
    payload = node(conn, note)["payload"]
    assert payload["origin"] == {"by": "agent", "model": "some-model"}


def test_debug_note_without_a_debug_hub_stays_unfiled(conn, make_account):
    alice = make_account()
    out = handlers.put(conn, alice, text="no hub yet", kind="debug")
    assert "unfiled" in out
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM membership m JOIN node n ON n.id = m.node_id"
            " WHERE n.address = %s",
            (addr(out),),
        ).fetchone()["n"]
        == 0
    )
    assert can_read(conn, alice, addr(out))


def test_hub_author_reads_everything_filed_in_it_without_a_grant_row(
    conn, make_account
):
    alice, bob, carol = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, bob)
    share(conn, alice, hub, carol, "read")
    sub = mk_hub(conn, bob, "Bob's sub-hub", parent=hub)
    bn = mk_note(conn, bob, "bob note", hub=hub)
    bpriv = mk_note(conn, bob, "bob private", hub=hub, private=True)
    deep = mk_note(conn, bob, "deep", hub=sub)
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM hub_grant g JOIN node h ON h.id = g.hub_node_id"
            " WHERE h.address = %s AND g.grantee = %s",
            (hub, alice.person_id),
        ).fetchone()["n"]
        == 0
    )
    seen = readable(conn, alice)
    assert {bn, sub, deep} <= seen
    assert bpriv not in seen
    # but the admin of a hub does not read what lies beside it
    assert not readable(conn, carol) & {bpriv}


# --- the routes to a note ---------------------------------------------------------


def test_withdrawn_membership_hides_the_atom_again(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob, "read")
    note = mk_note(conn, alice, "n", hub=hub)
    assert can_read(conn, bob, note)
    handlers.link(conn, alice, ref=note, to=hub, remove=True)
    assert not can_read(conn, bob, note)
    # filing it again (a new version of the same statement) restores it
    handlers.link(conn, alice, ref=note, to=hub)
    assert can_read(conn, bob, note)
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM membership m JOIN node n ON n.id = m.node_id"
            " WHERE n.address = %s",
            (note,),
        ).fetchone()["n"]
        == 1
    )


def test_admin_can_remove_someone_elses_atom_but_a_member_cannot(conn, make_account):
    alice, bob, carol = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob)
    share(conn, alice, hub, carol)
    note = mk_note(conn, bob, "bob's", hub=hub)
    assert can_read(conn, carol, note)
    assert (
        code_of(handlers.link, conn, carol, ref=note, to=hub, remove=True)
        == "Forbidden"
    )
    assert can_read(conn, carol, note)
    handlers.link(conn, alice, ref=note, to=hub, remove=True)
    assert not can_read(conn, carol, note) and not can_read(conn, alice, note)
    assert can_read(conn, bob, note)  # still his


def test_membership_with_audience_private_reveals_nothing(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob, "read")
    note = mk_note(conn, alice, "n")
    insert_membership(conn, note, hub, alice, audience="private")
    assert not can_read(conn, bob, note)
    insert_membership(conn, note, hub, alice, audience="weird-unknown")
    assert not can_read(conn, bob, note)
    insert_membership(conn, note, hub, alice, audience="hub")
    assert can_read(conn, bob, note)


def test_membership_filed_by_someone_else_than_the_author_is_ignored(
    conn, make_account
):
    alice, bob, eve = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob, "read")
    victim = mk_note(conn, eve, "eve's note")
    insert_membership(conn, victim, hub, alice)  # alice 'files' eve's note
    assert not can_read(conn, bob, victim)
    assert not can_read(conn, alice, victim)


def test_foreign_atoms_filed_by_a_reader_confer_no_access(conn, make_account):
    """A reader who can see Bob's note may organise it into a hub of their own,
    but that never shows it to the hub's other members."""
    alice, bob, mallory, eve = (make_account() for _ in range(4))
    shared = mk_hub(conn, alice, "Shared")
    share(conn, alice, shared, bob)
    share(conn, alice, shared, mallory)
    bobs = mk_note(conn, bob, "for the shared hub only", hub=shared)
    mine = mk_hub(conn, mallory, "Mallory's")
    share(conn, mallory, mine, eve)
    handlers.link(conn, mallory, ref=bobs, to=mine)
    assert not can_read(conn, eve, bobs)
    assert not can_read(conn, eve, shared)


def test_forged_records_that_bypass_the_handlers_are_not_applied(conn, make_account):
    """Records signed locally but outside the API still go through the authority
    checks of the materialiser (the path ingest from elsewhere would take)."""
    alice, bob, eve = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob, "read")  # bob may only read
    hub_node = node(conn, hub)
    bobs = mk_note(conn, bob, "bob's own")
    # bob (read-only) signs a membership of his own note into the hub
    from reticula_mcp import format as fmt

    rec = records.sign_and_append(
        conn,
        bob,
        type="member",
        address=fmt.new_address(bob.person_id),
        payload={"atom": bobs, "hub": hub, "withdrawn": False},
        hub_id=fmt.hub_id_from_address(hub),
    )
    assert apply_record(conn, rec) is None
    assert not can_read(conn, alice, bobs)
    # bob signs a grant for eve
    rec = records.sign_and_append(
        conn,
        bob,
        type="grant",
        address=fmt.new_address(bob.person_id),
        payload={"hub": hub, "to": eve.person_id, "mode": "add", "wrapped_key": None},
        hub_id=fmt.hub_id_from_address(hub),
    )
    apply_record(conn, rec)
    assert not can_read(conn, eve, hub)
    # eve signs a grant for herself
    rec = records.sign_and_append(
        conn,
        eve,
        type="grant",
        address=fmt.new_address(eve.person_id),
        payload={"hub": hub, "to": eve.person_id, "mode": "add", "wrapped_key": None},
        hub_id=fmt.hub_id_from_address(hub),
    )
    apply_record(conn, rec)
    assert not can_read(conn, eve, hub)
    # eve forges someone else's withdrawal of alice's filing
    note = mk_note(conn, alice, "alice's", hub=hub)
    rec = records.sign_and_append(
        conn,
        eve,
        type="member",
        address=fmt.new_address(eve.person_id),
        payload={"atom": note, "hub": hub, "withdrawn": True},
        hub_id=fmt.hub_id_from_address(hub),
    )
    apply_record(conn, rec)
    assert can_read(conn, bob, note)
    assert hub_node["id"] == sql_node_id(conn, hub)


def test_revoked_grant_none_row_confers_nothing(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    note = mk_note(conn, alice, "n", hub=hub)
    share(conn, alice, hub, bob)
    share(conn, alice, hub, bob, "none")
    assert not readable(conn, bob) & {hub, note}
    assert not access.can_add_person(conn, bob.person_id, sql_node_id(conn, hub))
    # and can be granted again
    share(conn, alice, hub, bob, "read")
    assert can_read(conn, bob, note)


def test_none_on_a_sub_hub_does_not_override_access_from_above(conn, make_account):
    alice, bob = make_account(), make_account()
    parent = mk_hub(conn, alice, "P")
    sub = mk_hub(conn, alice, "S", parent=parent)
    inside = mk_note(conn, alice, "in S", hub=sub)
    share(conn, alice, parent, bob, "read")
    out = share(conn, alice, sub, bob, "none")
    assert "still has read access" in out
    assert can_read(conn, bob, inside)  # no deny rules: revoke on the parent
    share(conn, alice, parent, bob, "none")
    assert not can_read(conn, bob, inside)


def test_grant_on_sub_hub_does_not_reveal_the_parent(conn, make_account):
    alice, dave = make_account(), make_account()
    parent = mk_hub(conn, alice, "P")
    sub = mk_hub(conn, alice, "S", parent=parent)
    share(conn, alice, sub, dave)
    assert can_read(conn, dave, sub)
    assert not can_read(conn, dave, parent)
    # dave may add to the sub-hub, not to the parent
    mk_note(conn, dave, "ok", hub=sub)
    assert code_of(handlers.put, conn, dave, text="no", hub=parent) == "NotFound"
    # and cannot file his own hub under the parent
    mine = mk_hub(conn, dave, "Dave's")
    assert code_of(handlers.link, conn, dave, ref=mine, to=parent) == "NotFound"


def test_atom_filed_in_two_hubs_is_readable_through_either(conn, make_account):
    alice, bob, carol = (make_account() for _ in range(3))
    h1, h2 = mk_hub(conn, alice, "H1"), mk_hub(conn, alice, "H2")
    share(conn, alice, h1, bob, "read")
    share(conn, alice, h2, carol, "read")
    note = mk_note(conn, alice, "both", hub=h1)
    handlers.link(conn, alice, ref=note, to=h2)
    assert can_read(conn, bob, note) and can_read(conn, carol, note)
    handlers.link(conn, alice, ref=note, to=h1, remove=True)
    assert not can_read(conn, bob, note) and can_read(conn, carol, note)


def test_tombstoned_hub_cuts_off_what_is_filed_beneath_it(conn, make_account):
    alice, bob, carol = (make_account() for _ in range(3))
    top = mk_hub(conn, alice, "Top")
    mid = mk_hub(conn, alice, "Mid", parent=top)
    leaf = mk_note(conn, alice, "leaf", hub=mid)
    share(conn, alice, top, bob, "read")
    share(conn, alice, mid, carol, "read")
    assert can_read(conn, bob, leaf) and can_read(conn, carol, leaf)
    handlers.delete(conn, alice, ref=mid)
    assert not can_read(conn, bob, leaf)  # the middle hub no longer carries access
    assert not can_read(conn, carol, leaf) and not can_read(conn, carol, mid)
    assert can_read(conn, alice, leaf)
    assert code_of(handlers.share, conn, alice, hub=mid) == "NotFound"


def test_tombstoned_atom_is_unreadable_to_everyone(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob)
    note = mk_note(conn, bob, "n", hub=hub)
    handlers.delete(conn, bob, ref=note)
    assert note not in readable(conn, bob) | readable(conn, alice)


def test_private_atom_is_never_widened_by_hub_structure(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob)
    note = mk_note(conn, alice, "mine", private=True)
    handlers.link(conn, alice, ref=note, to=hub)  # filing a private note
    assert not can_read(conn, bob, note)
    # switching it public publishes it; switching back hides it again
    handlers.edit(conn, alice, ref=note, private=False)
    assert can_read(conn, bob, note)
    handlers.edit(conn, alice, ref=note, private=True)
    assert not can_read(conn, bob, note)


def test_unknown_audience_fails_closed(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob)
    note = mk_note(conn, alice, "n", hub=hub)
    assert can_read(conn, bob, note)
    conn.execute("UPDATE node SET audience = 'public' WHERE address = %s", (note,))
    assert not can_read(conn, bob, note)
    assert can_read(conn, alice, note)


def test_private_hub_cannot_be_shared_and_grants_on_it_confer_nothing(
    conn, make_account
):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    note = mk_note(conn, alice, "n", hub=hub)
    share(conn, alice, hub, bob)
    assert can_read(conn, bob, note)
    handlers.edit(conn, alice, ref=hub, private=True)
    assert not readable(conn, bob) & {hub, note}
    assert code_of(share, conn, alice, hub, bob) == "BadInput"


def test_membership_cycle_rows_terminate(conn, make_account):
    alice, bob = make_account(), make_account()
    a, b = mk_hub(conn, alice, "A"), mk_hub(conn, alice, "B")
    note = mk_note(conn, alice, "n", hub=a)
    share(conn, alice, a, bob, "read")
    insert_membership(conn, a, b, alice)
    insert_membership(conn, b, a, alice)  # a cycle forged straight into the table
    assert can_read(conn, bob, note) and can_read(conn, bob, b)
    assert access.hub_access(conn, bob.person_id, sql_node_id(conn, b)) == "read"


def test_unknown_viewer_reads_nothing(conn, make_account):
    alice = make_account()
    mk_note(conn, alice, "n")
    assert (
        conn.execute("SELECT count(*) AS n FROM readable_nodes(%s)", (-1,)).fetchone()[
            "n"
        ]
        == 0
    )


def test_edit_keeps_visibility_and_sub_hub_membership(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob, "read")
    note = mk_note(conn, alice, "v1", hub=hub)
    handlers.edit(conn, alice, ref=note, text="v2", tags=["x"])
    assert can_read(conn, bob, note)
    assert node(conn, note)["text"] == "v2" and node(conn, note)["tags"] == ["x"]


# --- can_add -----------------------------------------------------------------------


def test_can_add_rules(conn, make_account):
    alice, adder, reader, revoked, outsider = (make_account() for _ in range(5))
    parent = mk_hub(conn, alice, "P")
    sub = mk_hub(conn, alice, "S", parent=parent)
    share(conn, alice, parent, adder, "add")
    share(conn, alice, parent, reader, "read")
    share(conn, alice, parent, revoked, "add")
    share(conn, alice, parent, revoked, "none")
    pid, sid = sql_node_id(conn, parent), sql_node_id(conn, sub)
    for hub_id in (pid, sid):
        assert permissions.can_add(conn, alice, hub_id)
        assert permissions.can_add(conn, adder, hub_id)  # ancestor grant
        assert not permissions.can_add(conn, reader, hub_id)
        assert not permissions.can_add(conn, revoked, hub_id)
        assert not permissions.can_add(conn, outsider, hub_id)
    # a plain note is not a hub; a tombstoned hub takes nothing
    plain = mk_note(conn, alice, "n")
    assert not permissions.can_add(conn, alice, sql_node_id(conn, plain))
    handlers.delete(conn, alice, ref=sub)
    assert not permissions.can_add(conn, alice, sid)
    # accept: anyone
    api.update_atom(conn, alice, pid, payload_extra={"accept": "anyone"})
    assert permissions.can_add(conn, outsider, pid)
    assert not can_read(conn, outsider, parent)


# --- the grant rules ---------------------------------------------------------------


def test_grant_rules(conn, make_account):
    alice, bob, carol, dan = (make_account() for _ in range(4))
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob, "add")
    share(conn, alice, hub, carol, "read")
    # a reader cannot invite
    assert code_of(share, conn, carol, hub, dan, "read") == "Forbidden"
    # a member cannot change or revoke someone else, nor themself upward
    assert code_of(share, conn, bob, hub, carol, "add") == "Forbidden"
    assert code_of(share, conn, bob, hub, carol, "none") == "Forbidden"
    assert code_of(share, conn, carol, hub, carol, "add") == "Forbidden"
    # nobody changes the admin
    assert code_of(share, conn, bob, hub, alice, "none") == "Forbidden"
    assert code_of(share, conn, alice, hub, alice, "read") == "Forbidden"
    # a member invites, but cannot restore someone the admin revoked
    share(conn, bob, hub, dan, "read")
    share(conn, alice, hub, dan, "none")
    assert code_of(share, conn, bob, hub, dan, "read") == "Forbidden"
    share(conn, alice, hub, dan, "read")
    # anyone may leave, and a member may bring back someone who left
    out = share(conn, carol, hub, carol, "none")
    assert "no longer" in out
    assert not can_read(conn, carol, hub)
    share(conn, bob, hub, carol, "read")
    assert can_read(conn, carol, hub)
    # listing: visible to readers, with modes and granters
    listing = handlers.share(conn, dan, hub=hub)
    assert f"@{alice.handle} admin" in listing
    assert f"@{bob.handle} add (granted by @{alice.handle})" in listing
    assert f"@{carol.handle} read (granted by @{bob.handle})" in listing
    # outsiders cannot list
    assert code_of(handlers.share, conn, make_account(), hub=hub) == "NotFound"
    assert code_of(share, conn, alice, hub, bob, "owner") == "BadInput"
    with pytest.raises(ReticulaError) as e:
        handlers.share(conn, alice, hub=hub, person="@nobody-here", mode="read")
    assert e.value.code == "NotFound"


def test_personal_hub_grants_default_to_read(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Mine", preset="personal")
    out = share(conn, alice, hub, bob, "add")
    assert "read" in out and "personal" in out
    assert code_of(handlers.put, conn, bob, text="x", hub=hub) == "Forbidden"
    handlers.edit(conn, alice, ref=hub, preset="group")
    share(conn, alice, hub, bob, "add")
    mk_note(conn, bob, "now allowed", hub=hub)
    assert code_of(handlers.edit, conn, bob, ref=hub, preset="personal") == "Forbidden"


def test_hub_access_agrees_with_readable_hubs_on_random_hierarchies(conn, make_account):
    rng = random.Random(7)
    people = [make_account() for _ in range(4)]
    hubs: list[str] = []
    for i in range(9):
        owner = rng.choice(people)
        hubs.append(mk_hub(conn, owner, f"h{i}"))
    # a random DAG filed by each hub's own author (forged rows for the cross-people
    # filings the tools would refuse); grants straight into the table
    for child, parent in itertools.combinations(range(len(hubs)), 2):
        if rng.random() < 0.3:
            author = next(
                p for p in people if p.person_id == node(conn, hubs[child])["author"]
            )
            insert_membership(conn, hubs[child], hubs[parent], author)
    for h in hubs:
        for p in people:
            if rng.random() < 0.3:
                conn.execute(
                    "INSERT INTO hub_grant (hub_node_id, grantee, mode, granted_by,"
                    " address, record_id) VALUES (%s,%s,%s,%s,%s,"
                    " (SELECT max(id) FROM record)) ON CONFLICT DO NOTHING",
                    (
                        sql_node_id(conn, h),
                        p.person_id,
                        rng.choice(["read", "add", "none"]),
                        "x",
                        f"g:{h}:{p.person_id}",
                    ),
                )
    for p in people:
        seen = readable(conn, p)
        for h in hubs:
            has_access = access.hub_access(conn, p.person_id, sql_node_id(conn, h))
            assert (has_access is not None) == (h in seen), (p.handle, h)
