"""What ``get`` tells a viewer about other people: a hub's access list, and
whether a deleted note ever existed.

These run against the real write path (graph handlers) and the real
``readable_nodes``, with no test-local stand-in.
"""

from __future__ import annotations

import re

import pytest

from reticula_mcp import accounts
from reticula_mcp.content import bootstrap
from reticula_mcp.content import hooks as content_hooks
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import handlers, read
from tests.search_support import use_hash_embedder

_ADDRESS = re.compile(r"ret:p[a-z2-7]{26}/[0-9a-f-]{36}")


@pytest.fixture(autouse=True)
def hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def address(out: str) -> str:
    """The address in the reply of a ``put``."""
    found = _ADDRESS.search(out)
    assert found, out
    return found.group(0)


def access_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.startswith("access:")]
    assert len(lines) == 1, out
    return lines[0]


def reader_line(owner: str, me: str) -> str:
    return f"access: @{owner} owner, @{me} read (you); {read.READERS_NOTE}"


# --------------------------------------------------------------- the access list


def test_a_reader_of_the_instructions_hub_sees_no_other_account(conn, make_account):
    system = accounts.ensure_system_account(conn)
    content_hooks.on_migrated(conn)  # seeds the shipped hub
    alice, bob, carol = (make_account(handle=h) for h in ("alice", "bob", "carol"))
    hub = bootstrap.find_shipped_node(conn, system, bootstrap.INSTRUCTIONS_HUB_SLUG)
    assert hub is not None
    grantees = conn.execute(
        "SELECT count(*) AS n FROM hub_grant WHERE hub_node_id = %s AND mode = 'read'",
        (hub["id"],),
    ).fetchone()["n"]
    assert grantees >= 3  # every account holds a read grant: the leak's setting

    for viewer, others in ((bob, ("alice", "carol")), (alice, ("bob", "carol"))):
        out = handlers.get(conn, viewer, ref=hub["address"])
        assert access_line(out) == reader_line(system.handle, viewer.handle)
        for handle in others:
            assert f"@{handle}" not in out
        for person in (alice, bob, carol):
            if person.id != viewer.id:
                assert person.person_id not in out

    # the hub's admin still sees everyone
    full = access_line(handlers.get(conn, system, ref=hub["address"]))
    for handle in ("alice", "bob", "carol"):
        assert f"@{handle} read" in full


def test_a_reader_of_a_personal_hub_sees_the_owner_and_themself_only(
    conn, make_account
):
    alice, bob, carol, dave = (
        make_account(handle=h) for h in ("alice", "bob", "carol", "dave")
    )
    hub = address(
        graph.put(conn, alice, kind="hub", title="Alice's journal", preset="personal")
    )
    # a personal hub turns an add invitation into read
    graph.share(conn, alice, hub=hub, person="@bob", mode="add")
    alone = handlers.get(conn, bob, ref=hub)
    assert access_line(alone) == reader_line("alice", "bob")

    graph.share(conn, alice, hub=hub, person="@carol", mode="read")
    graph.share(conn, alice, hub=hub, person="@dave", mode="read")
    crowded = handlers.get(conn, bob, ref=hub)
    # no other reader, and nothing that counts them: the line did not change
    assert access_line(crowded) == access_line(alone)
    assert "@carol" not in crowded and "@dave" not in crowded
    assert carol.person_id not in crowded and dave.person_id not in crowded

    owner = access_line(handlers.get(conn, alice, ref=hub))
    assert owner == "access: @alice owner, @bob read, @carol read, @dave read"


def test_a_member_of_a_group_hub_sees_everyone_and_a_reader_does_not(
    conn, make_account
):
    alice, bob, carol, dave = (
        make_account(handle=h) for h in ("alice", "bob", "carol", "dave")
    )
    hub = address(graph.put(conn, alice, kind="hub", title="Team", preset="group"))
    graph.share(conn, alice, hub=hub, person="@bob", mode="add")
    graph.share(conn, alice, hub=hub, person="@carol", mode="read")
    graph.share(conn, bob, hub=hub, person="@dave", mode="add")  # members invite

    everyone = "access: @alice owner, @bob add, @carol read, @dave add"
    for member in (alice, bob, dave):  # the admin and the people who can add
        assert access_line(handlers.get(conn, member, ref=hub)) == everyone
    reader = handlers.get(conn, carol, ref=hub)
    assert access_line(reader) == reader_line("alice", "carol")
    assert "@bob" not in reader and "@dave" not in reader


def test_access_through_a_hub_above_follows_the_same_rule(conn, make_account):
    alice, bob, carol, dave = (
        make_account(handle=h) for h in ("alice", "bob", "carol", "dave")
    )
    top = address(graph.put(conn, alice, kind="hub", title="Top"))
    sub = address(graph.put(conn, alice, kind="hub", title="Sub", hub=top))
    graph.share(conn, alice, hub=top, person="@bob", mode="add")
    graph.share(conn, alice, hub=top, person="@carol", mode="read")
    graph.share(conn, alice, hub=sub, person="@dave", mode="read")

    # bob can add (through Top): the whole list of the sub-hub, and the people
    # on Top, whose list he may see there too
    assert (
        access_line(handlers.get(conn, bob, ref=sub))
        == "access: @alice owner, @dave read,"
        " @bob add (through a hub above), @carol read (through a hub above)"
    )
    # carol only reads (through Top): the owner and herself
    seen = handlers.get(conn, carol, ref=sub)
    assert access_line(seen) == reader_line("alice", "carol")
    assert "@dave" not in seen and "@bob" not in seen


# ---------------------------------------------------------- the withdrawn answer


def test_withdrawn_is_answered_only_to_those_the_author_shared_with(conn, make_account):
    alice, bob, carol, erin = (
        make_account(handle=h) for h in ("alice", "bob", "carol", "erin")
    )
    team = address(graph.put(conn, alice, kind="hub", title="Team"))
    graph.share(conn, alice, hub=team, person="@bob", mode="add")
    note = address(
        graph.put(
            conn, alice, text="confidential okapi census", title="Okapi", hub=team
        )
    )
    # bob, who can read the note, files it under a hub of his own: that only
    # organises, it gives his readers no access to the note
    shelf = address(graph.put(conn, bob, kind="hub", title="Bob's shelf"))
    graph.link(conn, bob, ref=note, to=shelf)
    graph.share(conn, bob, hub=shelf, person="@carol", mode="read")
    with pytest.raises(ReticulaError) as before:
        handlers.get(conn, carol, ref=note)
    assert before.value.code == "NotFound"
    assert "Okapi" not in handlers.get(conn, carol, ref=shelf)

    graph.delete(conn, alice, ref=note)

    # carol reads a hub that holds a filing about the note, but not one made by
    # its author: she learns nothing, exactly like a stranger or a made-up address
    never = note[:-6] + "abcdef"
    for viewer in (carol, erin):
        errors = []
        for ref in (note, note[-8:], never):
            with pytest.raises(ReticulaError) as exc:
                handlers.get(conn, viewer, ref=ref)
            assert exc.value.code == "NotFound"
            assert "withdrawn" not in exc.value.render()
            errors.append(exc.value.render().replace(ref, "<ref>"))
        assert errors[0] == errors[2]  # deleted and never-existing read the same

    # the author, and a reader of the hub the author filed it in, are told
    for viewer in (alice, bob):
        out = handlers.get(conn, viewer, ref=note)
        assert note in out and "withdrawn" in out
        assert "okapi" not in out.lower()


def test_a_private_note_is_withdrawn_only_for_its_author(conn, make_account):
    alice, bob = make_account(handle="alice"), make_account(handle="bob")
    team = address(graph.put(conn, alice, kind="hub", title="Team"))
    graph.share(conn, alice, hub=team, person="@bob", mode="add")
    note = address(
        graph.put(conn, alice, text="private tapir memo", hub=team, private=True)
    )
    graph.delete(conn, alice, ref=note)
    assert "withdrawn" in handlers.get(conn, alice, ref=note)
    with pytest.raises(ReticulaError) as exc:
        handlers.get(conn, bob, ref=note)
    assert exc.value.code == "NotFound"


# ------------------------------------------------- the hub a result is shown in


def test_a_result_names_only_a_hub_the_viewer_may_read(conn, make_account):
    """A note filed in two hubs, the first of them not shared. The one who reads
    it through the second hub is shown the second and nothing of the first."""
    alice, bob = make_account(handle="alice"), make_account(handle="bob")
    ledger = address(graph.put(conn, alice, kind="hub", title="Ledger"))
    kiln = address(graph.put(conn, alice, kind="hub", title="Kiln"))
    note = address(
        graph.put(conn, alice, text="cone 6 glaze fires at 1222 C", hub=ledger)
    )
    graph.link(conn, alice, ref=note, to=kiln, rel="part-of")
    graph.share(conn, alice, hub=kiln, person="@bob", mode="read")

    hers = handlers.search(conn, alice, query="glaze")
    assert '· in "Ledger"' in hers  # the hub it was filed in first

    for his in (
        handlers.search(conn, bob, query="glaze"),
        handlers.search(conn, bob, query="", kind="note"),
        handlers.get(conn, bob, ref=note),
        handlers.get(conn, bob, ref=kiln),
    ):
        assert "1222" in his and "Kiln" in his
        assert "Ledger" not in his and ledger not in his
    assert '· in "Kiln"' in handlers.search(conn, bob, query="glaze")
