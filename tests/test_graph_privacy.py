"""Who is shown whom: access lists and head counts reveal other people only to
those who may see them. A display rule; no permission changes."""

from __future__ import annotations

import pytest

from reticula_mcp import accounts
from reticula_mcp.content import bootstrap, debug, hooks
from reticula_mcp.content import handlers as content
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, handlers
from tests.graph_support import mk_hub, mk_note, share


def err(call, *args, **kwargs) -> tuple[str, str]:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value.code, e.value.message


def line_for(text: str, title: str) -> str:
    return next(ln for ln in text.splitlines() if title in ln)


def test_new_account_cannot_learn_other_handles_or_the_account_count(
    conn, make_account
):
    system = accounts.ensure_system_account(conn)
    hooks.on_migrated(conn)
    others = [make_account(handle=h) for h in ("ada", "grace")]
    newcomer = make_account(handle="newcomer")
    hub = bootstrap.find_shipped_node(conn, system, bootstrap.INSTRUCTIONS_HUB_SLUG)
    assert hub is not None
    names = [o.handle for o in others]

    listing = handlers.share(conn, newcomer, hub=hub["address"])
    assert "@reticula admin" in listing  # the owner is shown
    assert "@newcomer read" in listing  # so is the viewer's own entry
    assert "only to people who can add" in listing
    for text in (
        listing,
        content.start(conn, newcomer),
        content.profile(conn, newcomer),
    ):
        for name in names:
            assert f"@{name}" not in text
    hub_line = line_for(content.start(conn, newcomer), hub["address"][-8:])
    assert "people" not in hub_line and "person" not in hub_line
    assert "note" in hub_line  # the note count stays, restricted to what is readable
    assert "people" not in line_for(
        content.profile(conn, newcomer), hub["address"][-8:]
    )

    # asking about someone else answers the same whoever exists or has access
    for mode in ("none", "read", "add"):
        known = err(
            handlers.share, conn, newcomer, hub=hub["address"], person="@ada", mode=mode
        )
        unknown = err(
            handlers.share,
            conn,
            newcomer,
            hub=hub["address"],
            person="@nobody",
            mode=mode,
        )
        assert known == unknown and known[0] == "Forbidden"

    # the owner sees everyone, with the count
    full = handlers.share(conn, system, hub=hub["address"])
    for name in (*names, "newcomer"):
        assert f"@{name} read" in full
    owner_line = line_for(content.start(conn, system), hub["address"][-8:])
    assert "shared with @" in owner_line and "@newcomer" in owner_line


def test_reader_of_a_personal_hub_does_not_see_the_other_readers(conn, make_account):
    alice, bob, carol = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "Diary", preset="personal")
    share(conn, alice, hub, bob)
    share(conn, alice, hub, carol)
    mk_note(conn, alice, "entry", hub=hub)
    seen = handlers.share(conn, bob, hub=hub)
    assert f"@{alice.handle} admin" in seen and f"@{bob.handle} read" in seen
    assert carol.handle not in seen
    for text in (content.start(conn, bob), content.profile(conn, bob)):
        line = line_for(text, "Diary")
        assert "people" not in line and "1 note" in line and "read" in line
        assert carol.handle not in text
    # the admin sees everyone and the count
    full = handlers.share(conn, alice, hub=hub)
    assert f"@{bob.handle} read" in full and f"@{carol.handle} read" in full
    assert f"shared with @{bob.handle}, @{carol.handle}," in line_for(
        content.start(conn, alice), "Diary"
    )
    # a reader cannot probe handles either
    assert err(
        handlers.share, conn, bob, hub=hub, person=f"@{carol.handle}", mode="none"
    ) == err(handlers.share, conn, bob, hub=hub, person="@ghost", mode="none")


def test_members_of_a_group_hub_who_can_add_see_everyone(conn, make_account):
    alice, bob, carol, dan = (make_account() for _ in range(4))
    hub = mk_hub(conn, alice, "Team", preset="group")
    share(conn, alice, hub, bob, "add")
    share(conn, alice, hub, carol, "add")
    share(conn, alice, hub, dan, "read")
    for viewer in (alice, bob, carol):
        full = handlers.share(conn, viewer, hub=hub)
        for other in (alice, bob, carol, dan):
            assert f"@{other.handle}" in full
        team = line_for(content.start(conn, viewer), "Team")
        if viewer is alice:
            assert f"shared with @{bob.handle}, @{carol.handle}, @{dan.handle}," in team
        else:
            assert "4 people have access" in team
        assert "only to people who can add" not in full
    # the read-only member sees only themself and the owner
    limited = handlers.share(conn, dan, hub=hub)
    assert f"@{dan.handle} read" in limited
    assert bob.handle not in limited and carol.handle not in limited
    assert "people" not in line_for(content.start(conn, dan), "Team")


def test_access_through_a_hub_above_follows_the_same_rule(conn, make_account):
    alice, bob, carol = (make_account() for _ in range(3))
    top = mk_hub(conn, alice, "Top")
    sub = mk_hub(conn, alice, "Sub", parent=top)
    share(conn, alice, top, bob, "add")
    share(conn, alice, top, carol, "read")
    dan = make_account()
    share(conn, alice, sub, dan, "read")  # a direct grant on the sub-hub
    assert f"@{dan.handle}" in handlers.share(conn, bob, hub=sub)  # add inherited
    assert f"@{dan.handle}" not in handlers.share(conn, carol, hub=sub)  # read only
    assert "people" not in line_for(content.start(conn, carol), "Sub")


def test_debug_listing_includes_a_note_only_through_its_authors_statement(
    conn, make_account
):
    system = accounts.ensure_system_account(conn)
    debug_hub = api.create_atom(
        conn,
        system,
        kind="hub",
        text="",
        title="Debug",
        tags=[bootstrap.slug_tag(bootstrap.DEBUG_HUB_SLUG)],
        payload_extra={"accept": "anyone"},
    )
    alice, bob = make_account(), make_account()
    pool = mk_hub(conn, alice, "pool")
    share(conn, alice, pool, bob, "read")
    # alice's debug note is unfiled (written before it was routed), bob can read it
    note = api.create_atom(
        conn, alice, kind="debug", text="alice's debug note", hub_node_id=None
    )
    handlers.link(conn, alice, ref=note["address"], to=pool)
    # bob (not the author) files it into the debug hub: organising only
    handlers.link(conn, bob, ref=note["address"], to=debug_hub["address"])
    assert [n.address for n in debug.list_debug_notes(conn)] == []
    # the author's own statement puts it on the operator's list
    handlers.link(conn, alice, ref=note["address"], to=debug_hub["address"])
    assert [n.address for n in debug.list_debug_notes(conn)] == [note["address"]]
