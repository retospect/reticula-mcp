"""The graph lane's acceptance lines, one scenario test each (real accounts,
real Postgres, the tool handlers)."""

from __future__ import annotations

import pytest

from reticula_mcp import records
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, handlers
from reticula_mcp.resolve import resolve_ref
from tests.graph_support import (
    WAITING,
    addr,
    can_read,
    current,
    delete_now,
    mk_hub,
    mk_note,
    node,
    readable,
    share,
)


def code_of(call, *args, **kwargs) -> str:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value.code


def test_01_note_in_shared_hub_is_readable_by_every_member_without_publish(
    conn, make_account
):
    alice, bob, carol, outsider = (make_account() for _ in range(4))
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, bob, "add")
    share(conn, alice, hub, carol, "read")
    n1 = mk_note(conn, alice, "alice's note", hub=hub)
    n2 = mk_note(conn, bob, "bob's note", hub=hub)
    for member in (alice, bob, carol):
        assert {hub, n1, n2} <= readable(conn, member)
        assert resolve_ref(conn, member, n2[-8:])["address"] == n2
    assert not readable(conn, outsider) & {hub, n1, n2}


def test_02_private_note_in_shared_hub_reaches_nobody_else(conn, make_account):
    alice, bob, carol = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, bob)
    share(conn, alice, hub, carol)
    secret = mk_note(conn, bob, "bob only", hub=hub, private=True)
    assert can_read(conn, bob, secret)
    assert not can_read(conn, alice, secret)  # not even the hub's admin
    assert not can_read(conn, carol, secret)
    # and it stays private when bob later edits it
    handlers.edit(conn, bob, ref=secret, text="still bob only")
    assert not can_read(conn, alice, secret)


def test_03_read_only_member_cannot_add(conn, make_account):
    alice, carol = make_account(), make_account()
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, carol, "read")
    assert code_of(handlers.put, conn, carol, text="x", hub=hub) == "Forbidden"
    own = mk_note(conn, carol, "carol's own note")
    assert code_of(handlers.link, conn, carol, ref=own, to=hub) == "Forbidden"
    assert not can_read(conn, alice, own)
    # nothing was written for the rejected attempts
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM membership WHERE author = %s",
            (carol.person_id,),
        ).fetchone()["n"]
        == 0
    )


def test_04_sub_hub_grant_reads_that_sub_hub_only(conn, make_account):
    alice, dave = make_account(), make_account()
    parent = mk_hub(conn, alice, "Parent")
    s1 = mk_hub(conn, alice, "Sub one", parent=parent)
    s2 = mk_hub(conn, alice, "Sub two", parent=parent)
    in_parent = mk_note(conn, alice, "parent note", hub=parent)
    in_s1 = mk_note(conn, alice, "s1 note", hub=s1)
    in_s2 = mk_note(conn, alice, "s2 note", hub=s2)
    deep = mk_hub(conn, alice, "Deep", parent=s1)
    in_deep = mk_note(conn, alice, "deep note", hub=deep)
    share(conn, alice, s1, dave, "read")
    seen = readable(conn, dave)
    assert {s1, in_s1, deep, in_deep} <= seen
    assert not seen & {parent, in_parent, s2, in_s2}


def test_05_revocation_removes_access(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Team")
    note = mk_note(conn, alice, "n", hub=hub)
    share(conn, alice, hub, bob)
    assert can_read(conn, bob, note) and can_read(conn, bob, hub)
    out = share(conn, alice, hub, bob, "none")
    assert "no longer" in out
    assert not readable(conn, bob) & {note, hub}
    # what bob wrote while a member stays his
    assert code_of(handlers.put, conn, bob, text="again", hub=hub) in (
        "NotFound",
        "Forbidden",
    )


def test_06_superseding_atom_replaces_its_parents_in_current_nodes(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, bob)
    old1 = mk_note(conn, alice, "first version of the idea", hub=hub)
    old2 = mk_note(conn, alice, "second take", hub=hub)
    new = mk_note(conn, alice, "merged idea", hub=hub, supersedes=[old1, old2])
    for viewer in (alice, bob):
        assert new in current(conn, viewer)
        assert not current(conn, viewer) & {old1, old2}
        for parent in (old1, old2):
            assert resolve_ref(conn, viewer, parent)["address"] == parent
    rows = conn.execute(
        "SELECT o.address FROM supersedes s JOIN node o ON o.id = s.old_node_id"
        " JOIN node n ON n.id = s.new_node_id WHERE n.address = %s",
        (new,),
    ).fetchall()
    assert {r["address"] for r in rows} == {old1, old2}
    # the old atoms were not touched: still one version each
    assert len(records.versions(conn, old1)) == 1


def test_07_tombstoned_atom_keeps_address_and_returns_no_content(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, bob)
    note = mk_note(conn, alice, "v1 secret words", hub=hub, title="Title")
    handlers.edit(conn, alice, ref=note, text="v2 more secret words")
    assert "deleted" in delete_now(conn, alice, note)
    row = node(conn, note)
    assert row["tombstoned_at"] is not None
    assert row["text"] == "" and row["title"] is None and row["payload"] == {}
    assert row["tags"] == [] and row["ext_key"] is None
    assert not readable(conn, alice) & {note} and not readable(conn, bob) & {note}
    # the address still answers as deleted, with no content
    gone = resolve_ref(conn, alice, note, allow_tombstoned=True)
    assert gone["address"] == note and gone["text"] == ""
    # earlier versions are no longer retrievable: envelopes and signatures erased
    versions = records.versions(conn, note)
    assert [v.type for v in versions] == ["atom", "atom", "tombstone"]
    for v in versions[:2]:
        assert v.blanked and v.envelope == b"" and v.signature == b""
    assert versions[2].type == "tombstone" and not versions[2].blanked
    leak = conn.execute(
        "SELECT count(*) AS n FROM record WHERE position('secret' in convert_from(envelope,'UTF8')) > 0"
    ).fetchone()
    assert leak["n"] == 0
    # nothing can follow, and only the author can delete
    assert code_of(handlers.edit, conn, alice, ref=note, text="x") == "NotFound"
    other = mk_note(conn, alice, "other", hub=hub)
    assert code_of(handlers.delete, conn, bob, ref=other) == "Forbidden"


def test_08_membership_that_closes_a_hub_cycle_is_refused(conn, make_account):
    alice = make_account()
    a = mk_hub(conn, alice, "A")
    b = mk_hub(conn, alice, "B", parent=a)
    c = mk_hub(conn, alice, "C", parent=b)
    assert code_of(handlers.link, conn, alice, ref=a, to=a) == "Conflict"
    assert code_of(handlers.link, conn, alice, ref=a, to=b) == "Conflict"
    assert code_of(handlers.link, conn, alice, ref=a, to=c) == "Conflict"
    # a diamond (two parents, no cycle) is fine
    d = mk_hub(conn, alice, "D")
    handlers.link(conn, alice, ref=c, to=d)
    assert code_of(handlers.link, conn, alice, ref=d, to=c) == "Conflict"
    # after the withdrawal the same filing is allowed
    handlers.link(conn, alice, ref=c, to=b, remove=True)
    handlers.link(conn, alice, ref=c, to=d, remove=True)
    handlers.link(conn, alice, ref=a, to=c)
    # nothing about the refused attempts reached the log
    live = {
        (r["child"], r["parent"])
        for r in conn.execute(
            "SELECT c.address AS child, p.address AS parent FROM membership m"
            " JOIN node c ON c.id = m.node_id JOIN node p ON p.id = m.hub_node_id"
            " WHERE NOT m.withdrawn"
        )
    }
    assert live == {(b, a), (a, c)}


def test_09_two_accounts_cannot_read_each_others_private_or_unfiled_atoms(
    conn, make_account
):
    alice, bob = make_account(), make_account()
    a_unfiled = mk_note(conn, alice, "alice unfiled")
    a_private = mk_note(conn, alice, "alice private", private=True)
    b_unfiled = mk_note(conn, bob, "bob unfiled")
    b_hub = mk_hub(conn, bob, "Bob hub")
    b_private = mk_note(conn, bob, "bob private in own hub", hub=b_hub, private=True)
    assert {a_unfiled, a_private} <= readable(conn, alice)
    assert not readable(conn, alice) & {b_unfiled, b_private, b_hub}
    assert not readable(conn, bob) & {a_unfiled, a_private}
    for ref in (a_unfiled, a_unfiled[-8:], a_private[-8:]):
        assert code_of(resolve_ref, conn, bob, ref) == "NotFound"
    assert code_of(handlers.edit, conn, bob, ref=a_unfiled, text="x") == "NotFound"
    assert code_of(handlers.delete, conn, bob, ref=a_private) == "NotFound"
    assert code_of(handlers.link, conn, bob, ref=a_unfiled, to=b_hub) == "NotFound"
    assert code_of(handlers.share, conn, alice, hub=b_hub) == "NotFound"


def test_10_invites_by_members_default_yes_admins_only_no(conn, make_account):
    alice, bob, newcomer, newcomer2 = (make_account() for _ in range(4))
    open_hub = mk_hub(conn, alice, "Open", preset="group")
    closed_hub = mk_hub(conn, alice, "Moderated", preset="moderated")
    for hub in (open_hub, closed_hub):
        share(conn, alice, hub, bob)
    note = mk_note(conn, alice, "n", hub=open_hub)
    # default hub: an ordinary member admits a newcomer, who then reads
    out = share(conn, bob, open_hub, newcomer)
    # a stranger to bob: it waited for the newcomer's yes (0304), which the
    # helper gave
    assert WAITING in out
    assert can_read(conn, newcomer, note)
    # moderated hub: the same invite is rejected, the admin's works
    assert code_of(share, conn, bob, closed_hub, newcomer2) == "Forbidden"
    assert not readable(conn, newcomer2)
    share(conn, alice, closed_hub, newcomer2)
    assert can_read(conn, newcomer2, closed_hub)
    # an invited member can themselves invite on a default hub (add inherited)
    share(conn, newcomer, open_hub, newcomer2, "read")
    assert can_read(conn, newcomer2, note)


def test_11_two_paper_stubs_with_the_same_doi_resolve_to_one_paper(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Reading")
    share(conn, alice, hub, bob)
    first = addr(
        handlers.put(
            conn,
            alice,
            text="",
            kind="paper",
            title="A Paper",
            ext_id="doi:10.1000/ABC.1",
            hub=hub,
        )
    )
    # another spelling of the same DOI, same author: the same paper
    again = handlers.put(
        conn,
        alice,
        text="",
        kind="paper",
        title="Other title",
        ext_id="https://doi.org/10.1000/abc.1",
    )
    assert addr(again) == first
    # another member who can read it gets the existing paper, not a duplicate
    theirs = handlers.put(
        conn, bob, text="", kind="paper", title="Dup", ext_id="DOI:10.1000/abc.1"
    )
    assert addr(theirs) == first and "already known" in theirs
    n = conn.execute("SELECT count(*) AS n FROM node WHERE kind = 'paper'").fetchone()[
        "n"
    ]
    assert n == 1
    assert node(conn, first)["ext_key"] == "doi:10.1000/abc.1"
    # a stranger who cannot read the first one makes their own
    eve = make_account()
    mine = addr(
        handlers.put(
            conn, eve, text="", kind="paper", title="Mine", ext_id="doi:10.1000/abc.1"
        )
    )
    assert mine != first


def test_12_adding_to_a_shared_hub_writes_only_new_records(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, bob)
    mk_note(conn, alice, "one", hub=hub)
    mk_note(conn, alice, "two", hub=hub)
    before_records = conn.execute(
        "SELECT id, version_hash, envelope FROM record ORDER BY id"
    ).fetchall()
    before_nodes = conn.execute("SELECT * FROM node ORDER BY id").fetchall()
    before_members = conn.execute("SELECT * FROM membership ORDER BY id").fetchall()
    before_grants = conn.execute("SELECT * FROM hub_grant ORDER BY id").fetchall()
    new = mk_note(conn, bob, "from bob", hub=hub)
    after_records = conn.execute(
        "SELECT id, version_hash, envelope FROM record ORDER BY id"
    ).fetchall()
    assert after_records[: len(before_records)] == before_records
    added = after_records[len(before_records) :]
    assert [r["id"] for r in added] == sorted(r["id"] for r in added)
    assert len(added) == 2  # the atom and its membership statement
    after_nodes = conn.execute("SELECT * FROM node ORDER BY id").fetchall()
    assert after_nodes[: len(before_nodes)] == before_nodes
    assert len(after_nodes) == len(before_nodes) + 1
    after_members = conn.execute("SELECT * FROM membership ORDER BY id").fetchall()
    assert after_members[: len(before_members)] == before_members
    assert (
        conn.execute("SELECT * FROM hub_grant ORDER BY id").fetchall() == before_grants
    )
    # and the first account sees it
    assert can_read(conn, alice, new)


def test_13_accept_anyone_hub_takes_adds_from_non_members_who_cannot_read(
    conn, make_account
):
    operator, stranger = make_account(), make_account()
    open_hub = addr(
        handlers.put(conn, operator, text="intake", kind="hub", title="Intake")
    )
    api.update_atom(
        conn,
        operator,
        node(conn, open_hub)["id"],
        payload_extra={"accept": "anyone"},
    )
    out = handlers.put(
        conn, stranger, text="hello from outside", kind="debug", hub=open_hub
    )
    note = addr(out)
    assert can_read(conn, operator, note)  # the operator reads what arrives
    assert can_read(conn, stranger, note)  # the author reads their own
    assert not can_read(conn, stranger, open_hub)  # but never the hub
    other = make_account()
    assert not can_read(conn, other, note)
    # accepting adds never opens reading, and does not apply to ordinary hubs
    closed = mk_hub(conn, operator, "Closed")
    assert code_of(handlers.put, conn, stranger, text="x", hub=closed) == "NotFound"
    # a short suffix of a hub you cannot read does not resolve
    assert (
        code_of(handlers.put, conn, stranger, text="x", hub=open_hub[-8:]) == "NotFound"
    )
