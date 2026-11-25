"""What a viewer is told about the people who read a hub through a hub above it,
the count on a member's line, and the access word on the line of somebody
else's hub. A display rule; no permission changes.

Written from the rules, not from the code: a failing assertion here is a finding.
"""

from __future__ import annotations

import re

import pytest

from reticula_mcp.content import handlers as content
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import access, handlers
from reticula_mcp.search import handlers as search
from reticula_mcp.search import read
from tests.graph_support import can_read, mk_hub, mk_note, share, sql_node_id
from tests.search_support import use_hash_embedder

THROUGH = "(through a hub above this one)"
OTHERS = "Other people read it through a hub above this one."
EVERYONE = "Everyone with access to a hub above this one can read it too."


@pytest.fixture(autouse=True)
def hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


# ------------------------------------------------------------------- helpers


def line_for(text: str, title: str) -> str:
    """The line of the hub with this title (lines read `- @owner "Title" ...`)."""
    return next(ln for ln in text.splitlines() if f'"{title}"' in ln)


def both(conn, viewer, title: str) -> list[str]:
    """The hub's line in `start` and in `profile`."""
    return [
        line_for(content.start(conn, viewer), title),
        line_for(content.profile(conn, viewer), title),
    ]


def after_owner(line: str) -> str:
    """What follows `owner  ` on the line of the viewer's own hub."""
    return line.split("owner  ", 1)[1]


def access_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.startswith("access:")]
    assert len(lines) == 1, out
    return lines[0]


def reader_line(owner, me) -> str:
    return (
        f"access: @{owner.handle} owner, @{me.handle} read (you); {read.READERS_NOTE}"
    )


def through_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if THROUGH in ln]


def through_handles(text: str) -> list[str]:
    return re.findall(rf"@(\S+) (?:add|read) {re.escape(THROUGH)}", text)


def share_text(conn, viewer, hub: str) -> str:
    return handlers.share(conn, viewer, hub=hub)


def assert_reader_view(conn, viewer, owner, hub: str, title: str, hidden) -> None:
    """The viewer reads the hub and gets what a reader always gets."""
    listing = share_text(conn, viewer, hub)
    assert f"@{owner.handle} admin" in listing
    assert f"@{viewer.handle} read" in listing
    assert "other readers are shown only to people who can add to this hub" in listing
    assert "hub above" not in listing
    shown = search.get(conn, viewer, ref=hub)
    assert access_line(shown) == reader_line(owner, viewer)
    for text in (listing, shown, *both(conn, viewer, title)):
        for person in hidden:
            assert f"@{person.handle}" not in text
    for ln in both(conn, viewer, title):
        assert "have access" not in ln


def assert_no_leak(conn, viewers, hub: str, above: list[str]) -> None:
    """Every handle `share(hub)` shows a viewer as read through a hub above also
    occurs in that viewer's `share` of some hub above."""
    for viewer in viewers:
        try:
            text = share_text(conn, viewer, hub)
        except ReticulaError:
            continue
        seen = ""
        for a in above:
            try:
                seen += share_text(conn, viewer, a)
            except ReticulaError:
                pass
        for handle in through_handles(text):
            assert f"@{handle}" in seen, (viewer.handle, handle)


WORD = re.compile(
    r"id [0-9a-f]{8}  (?P<word>.+?)"
    r"(?=  (?:only you|you, the owner)| +\d+ notes?\b|$)"
)


def word_of(line: str) -> str:
    found = WORD.search(line)
    assert found, line
    return found.group("word")


# ------------------------------------------------------------------ case 1


def build_owner_of_the_hub_above(conn, make_account):
    ada, bob, cleo = make_account(), make_account(), make_account()
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, bob, "add")
    share(conn, ada, top, cleo, "read")
    bobs = mk_hub(conn, bob, "Bobs", parent=top)
    return ada, bob, cleo, top, bobs


def test_owner_of_the_hub_above_gets_the_names_for_a_hub_filed_under_it(
    conn, make_account
):
    ada, bob, cleo, top, bobs = build_owner_of_the_hub_above(conn, make_account)
    listing = share_text(conn, ada, bobs)
    assert f"@{ada.handle} add {THROUGH}" in listing
    assert f"@{cleo.handle} read {THROUGH}" in listing
    assert f"@{bob.handle} add {THROUGH}" not in listing
    assert f"@{bob.handle} read {THROUGH}" not in listing
    assert OTHERS not in listing and EVERYONE not in listing


def test_owner_of_a_hub_filed_under_a_hub_he_can_add_to_gets_the_same_names(
    conn, make_account
):
    ada, bob, cleo, top, bobs = build_owner_of_the_hub_above(conn, make_account)
    listing = share_text(conn, bob, bobs)
    assert f"@{ada.handle} add {THROUGH}" in listing
    assert f"@{cleo.handle} read {THROUGH}" in listing
    assert through_handles(listing) == [ada.handle, cleo.handle]
    assert OTHERS not in listing and EVERYONE not in listing


def test_start_and_profile_tell_the_owner_who_reads_through_the_hub_above(
    conn, make_account
):
    ada, bob, cleo, top, bobs = build_owner_of_the_hub_above(conn, make_account)
    for ln in both(conn, bob, "Bobs"):
        assert after_owner(ln).startswith(
            f"shared with @{ada.handle}, @{cleo.handle} through a hub above it,"
        )


def test_get_names_who_reads_through_the_hub_above_to_both_who_may_know(
    conn, make_account
):
    ada, bob, cleo, top, bobs = build_owner_of_the_hub_above(conn, make_account)
    for viewer in (ada, bob):
        line = access_line(search.get(conn, viewer, ref=bobs))
        assert f"@{ada.handle} add (through a hub above)" in line
        assert f"@{cleo.handle} read (through a hub above)" in line
        assert line.index(f"@{ada.handle} add") < line.index(f"@{cleo.handle} read")
        assert f"@{bob.handle} add (through" not in line
        assert "other people through a hub above" not in line
        assert "everyone with access to a hub above" not in line


def test_the_reader_of_the_hub_above_gets_a_readers_view_of_the_hub_under_it(
    conn, make_account
):
    ada, bob, cleo, top, bobs = build_owner_of_the_hub_above(conn, make_account)
    assert_reader_view(conn, cleo, bob, bobs, "Bobs", [ada])


def test_no_name_leaks_in_the_hub_filed_under_the_hub_above(conn, make_account):
    ada, bob, cleo, top, bobs = build_owner_of_the_hub_above(conn, make_account)
    assert_no_leak(conn, (ada, bob, cleo), bobs, [top])


def test_readers_above_of_a_hub_filed_under_the_hub_above(conn, make_account):
    ada, bob, cleo, top, bobs = build_owner_of_the_hub_above(conn, make_account)
    bobs_id = sql_node_id(conn, bobs)
    rows, others = access.readers_above(conn, ada.person_id, bobs_id)
    assert [(r["handle"], r["mode"]) for r in rows] == [
        (ada.handle, "add"),
        (cleo.handle, "read"),
    ]
    assert rows[0]["person"] == ada.person_id
    assert others is False
    # Cleo reads the hub and may know nobody above it
    rows, others = access.readers_above(conn, cleo.person_id, bobs_id)
    assert rows == [] and others is True


# ------------------------------------------------------------------ case 2


def build_member_with_add_above(conn, make_account):
    ada, ben, cleo, dan = (make_account() for _ in range(4))
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, ben, "add")
    share(conn, ada, top, cleo, "read")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    share(conn, ada, sub, dan, "read")
    return ada, ben, cleo, dan, top, sub


def test_member_who_adds_above_sees_a_plain_grant_and_those_through_the_hub_above(
    conn, make_account
):
    ada, ben, cleo, dan, top, sub = build_member_with_add_above(conn, make_account)
    listing = share_text(conn, ben, sub)
    dan_line = line_for_handle(listing, dan)
    assert dan_line.strip().startswith(f"@{dan.handle} read")
    assert "hub above" not in dan_line
    assert f"@{ben.handle} add {THROUGH}" in listing
    assert f"@{cleo.handle} read {THROUGH}" in listing
    assert dan.handle not in "".join(through_lines(listing))
    assert through_handles(listing) == [ben.handle, cleo.handle]


def line_for_handle(text: str, person) -> str:
    return next(ln for ln in text.splitlines() if f"@{person.handle}" in ln)


def test_member_who_adds_above_sees_the_same_in_get(conn, make_account):
    ada, ben, cleo, dan, top, sub = build_member_with_add_above(conn, make_account)
    line = access_line(search.get(conn, ben, ref=sub))
    assert f"@{dan.handle} read" in line
    assert f"@{ben.handle} add (through a hub above)" in line
    assert f"@{cleo.handle} read (through a hub above)" in line
    assert f"@{dan.handle} read (through" not in line


def test_owner_with_a_grant_on_the_hub_and_people_above_is_told_both(
    conn, make_account
):
    ada, ben, cleo, dan, top, sub = build_member_with_add_above(conn, make_account)
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with @{dan.handle}, and with @{ben.handle}, @{cleo.handle}"
            " through a hub above it,"
        )


def test_reader_of_the_hub_above_gets_a_readers_view_of_the_hub_under_it(
    conn, make_account
):
    ada, ben, cleo, dan, top, sub = build_member_with_add_above(conn, make_account)
    assert_reader_view(conn, cleo, ada, sub, "Sub", [ben, dan])


def test_no_name_leaks_in_a_hub_with_a_member_and_a_reader_above(conn, make_account):
    ada, ben, cleo, dan, top, sub = build_member_with_add_above(conn, make_account)
    assert_no_leak(conn, (ada, ben, cleo, dan), sub, [top])


def test_readers_above_for_a_member_and_for_a_reader(conn, make_account):
    ada, ben, cleo, dan, top, sub = build_member_with_add_above(conn, make_account)
    sub_id = sql_node_id(conn, sub)
    rows, others = access.readers_above(conn, ben.person_id, sub_id)
    assert [(r["handle"], r["mode"]) for r in rows] == [
        (ben.handle, "add"),
        (cleo.handle, "read"),
    ]
    assert others is False
    rows, others = access.readers_above(conn, cleo.person_id, sub_id)
    assert rows == [] and others is True


# ------------------------------------------------------------------ case 3


def build_owner_who_only_reads_above(conn, make_account):
    ada, xavier, eve = make_account(), make_account(), make_account()
    top = mk_hub(conn, xavier, "Top")
    share(conn, xavier, top, ada, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    note = mk_note(conn, ada, "a note in sub", hub=sub)
    share(conn, xavier, top, eve, "add")
    share(conn, xavier, top, ada, "read")
    return ada, xavier, eve, top, sub, note


def test_downgraded_owner_of_the_sub_hub_still_carries_access_to_the_others(
    conn, make_account
):
    ada, xavier, eve, top, sub, note = build_owner_who_only_reads_above(
        conn, make_account
    )
    assert can_read(conn, eve, note)
    assert can_read(conn, xavier, note)


def test_owner_who_only_reads_above_is_told_everyone_has_a_hub_above(
    conn, make_account
):
    ada, xavier, eve, top, sub, note = build_owner_who_only_reads_above(
        conn, make_account
    )
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            "shared with everyone who has a hub above it,"
        )
        assert f"@{eve.handle}" not in ln
        assert f"@{xavier.handle}" not in after_owner(ln)
        assert "nobody" not in ln


def test_owner_who_only_reads_above_gets_no_names_in_the_access_list(
    conn, make_account
):
    ada, xavier, eve, top, sub, note = build_owner_who_only_reads_above(
        conn, make_account
    )
    listing = share_text(conn, ada, sub)
    assert EVERYONE in listing
    assert through_lines(listing) == []
    assert f"@{eve.handle}" not in listing and f"@{xavier.handle}" not in listing
    shown = search.get(conn, ada, ref=sub)
    line = access_line(shown)
    assert "everyone with access to a hub above this one" in line
    assert f"@{eve.handle}" not in shown
    assert f"@{xavier.handle}" not in line
    assert "through a hub above)" not in line


def test_owner_who_only_reads_above_sees_nobody_named_in_start_and_profile(
    conn, make_account
):
    ada, xavier, eve, top, sub, note = build_owner_who_only_reads_above(
        conn, make_account
    )
    for text in (content.start(conn, ada), content.profile(conn, ada)):
        assert f"@{eve.handle}" not in text
        assert f"@{eve.handle}" not in line_for(text, "Sub")


def test_readers_above_for_an_owner_who_only_reads_above(conn, make_account):
    ada, xavier, eve, top, sub, note = build_owner_who_only_reads_above(
        conn, make_account
    )
    rows, others = access.readers_above(conn, ada.person_id, sql_node_id(conn, sub))
    assert rows == [] and others is True


def test_owner_who_only_reads_above_with_a_direct_grant_is_told_so(conn, make_account):
    ada, xavier, eve, top, sub, note = build_owner_who_only_reads_above(
        conn, make_account
    )
    dan = make_account()
    share(conn, ada, sub, dan, "read")
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with @{dan.handle} and everyone who has a hub above it,"
        )
    assert_no_leak(conn, (ada, xavier, eve, dan), sub, [top])


# ------------------------------------------------------------------ case 4


def test_a_revoked_person_above_is_not_named_and_the_hub_is_unshared_again(
    conn, make_account
):
    ada, ben = make_account(), make_account()
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, ben, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with @{ben.handle} through a hub above it,"
        )
    share(conn, ada, top, ben, "none")
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith("shared with nobody,")
    assert "hub above" not in share_text(conn, ada, sub)
    assert "hub above" not in search.get(conn, ada, ref=sub)


def test_a_revoked_person_above_is_not_named_beside_a_remaining_one(conn, make_account):
    ada, ben, cleo = (make_account() for _ in range(3))
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, ben, "add")
    share(conn, ada, top, cleo, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    share(conn, ada, top, ben, "none")
    assert through_handles(share_text(conn, ada, sub)) == [cleo.handle]
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with @{cleo.handle} through a hub above it,"
        )


def test_revoking_a_grant_on_the_hub_leaves_the_person_named_through_the_hub_above(
    conn, make_account
):
    ada, cleo = make_account(), make_account()
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, cleo, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    share(conn, ada, sub, cleo, "read")
    before = share_text(conn, ada, sub)
    assert f"@{cleo.handle} read" in before
    assert f"@{cleo.handle} add {THROUGH}" not in before
    share(conn, ada, sub, cleo, "none")
    after = share_text(conn, ada, sub)
    assert f"@{cleo.handle} add {THROUGH}" in after
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with @{cleo.handle} through a hub above it,"
        )
    line = access_line(search.get(conn, ada, ref=sub))
    assert f"@{cleo.handle} add (through a hub above)" in line


# ------------------------------------------------------------------ case 5


def build_personal_hub_above(conn, make_account):
    xavier, ben, cleo = make_account(), make_account(), make_account()
    journal = mk_hub(conn, xavier, "Journal", preset="personal")
    share(conn, xavier, journal, ben, "add")
    share(conn, xavier, journal, cleo, "add")
    trips = mk_hub(conn, xavier, "Trips", parent=journal)
    return xavier, ben, cleo, journal, trips


def test_the_owner_of_a_hub_under_a_personal_hub_is_told_who_reads_it_there(
    conn, make_account
):
    xavier, ben, cleo, journal, trips = build_personal_hub_above(conn, make_account)
    for ln in both(conn, xavier, "Trips"):
        assert after_owner(ln).startswith(
            f"shared with @{ben.handle}, @{cleo.handle} through a hub above it,"
        )
    listing = share_text(conn, xavier, trips)
    assert f"@{ben.handle} read {THROUGH}" in listing
    assert f"@{cleo.handle} read {THROUGH}" in listing
    assert " add (through" not in listing
    line = access_line(search.get(conn, xavier, ref=trips))
    assert f"@{ben.handle} read (through a hub above)" in line


def test_a_reader_of_the_personal_hub_above_gets_a_readers_view_under_it(
    conn, make_account
):
    xavier, ben, cleo, journal, trips = build_personal_hub_above(conn, make_account)
    assert_reader_view(conn, ben, xavier, trips, "Trips", [cleo])


def test_no_name_leaks_in_a_hub_under_a_personal_hub(conn, make_account):
    xavier, ben, cleo, journal, trips = build_personal_hub_above(conn, make_account)
    assert_no_leak(conn, (xavier, ben, cleo), trips, [journal])


# ------------------------------------------------------------------ case 6


def test_a_hub_above_that_becomes_personal_stops_naming_its_people(conn, make_account):
    ada, xavier, eve = make_account(), make_account(), make_account()
    top = mk_hub(conn, xavier, "Top")
    share(conn, xavier, top, ada, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    share(conn, xavier, top, eve, "add")
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with @{xavier.handle}, @{eve.handle} through a hub above it,"
        )
    assert f"@{eve.handle} add {THROUGH}" in share_text(conn, ada, sub)

    handlers.edit(conn, xavier, ref=top, preset="personal")

    for ln in both(conn, ada, "Sub"):
        assert f"@{eve.handle}" not in ln
        assert after_owner(ln).startswith(
            "shared with everyone who has a hub above it,"
        )
    listing = share_text(conn, ada, sub)
    assert f"@{eve.handle}" not in listing
    assert through_lines(listing) == []
    assert EVERYONE in listing
    assert f"@{eve.handle}" not in search.get(conn, ada, ref=sub)
    assert_no_leak(conn, (ada, xavier, eve), sub, [top])


# ------------------------------------------------------------------ case 7


def build_two_levels(conn, make_account):
    ada, xavier, ben, zoe = (make_account() for _ in range(4))
    b = mk_hub(conn, xavier, "Bee")
    share(conn, xavier, b, ada, "add")
    a = mk_hub(conn, ada, "Ay", parent=b)
    h = mk_hub(conn, ada, "Aitch", parent=a)
    share(conn, ada, a, ben, "add")
    share(conn, xavier, b, zoe, "add")
    return ada, xavier, ben, zoe, a, b, h


def test_a_member_of_the_middle_hub_is_told_others_read_through_the_top_one(
    conn, make_account
):
    ada, xavier, ben, zoe, a, b, h = build_two_levels(conn, make_account)
    listing = share_text(conn, ben, h)
    assert f"@{ben.handle} add {THROUGH}" in listing
    assert OTHERS in listing
    assert f"@{zoe.handle}" not in listing and f"@{xavier.handle}" not in listing
    assert through_handles(listing) == [ben.handle]
    line = access_line(search.get(conn, ben, ref=h))
    assert f"@{ben.handle} add (through a hub above)" in line
    assert "other people through a hub above" in line
    assert f"@{zoe.handle}" not in line and f"@{xavier.handle}" not in line


def test_the_owner_of_both_levels_is_named_everyone(conn, make_account):
    ada, xavier, ben, zoe, a, b, h = build_two_levels(conn, make_account)
    listing = share_text(conn, ada, h)
    assert through_handles(listing) == [xavier.handle, zoe.handle, ben.handle]
    assert OTHERS not in listing and EVERYONE not in listing
    for ln in both(conn, ada, "Aitch"):
        assert after_owner(ln).startswith(
            f"shared with @{xavier.handle}, @{zoe.handle}, @{ben.handle}"
            " through a hub above it,"
        )


def test_a_member_of_the_middle_hub_gets_no_count_on_the_line_of_the_hub(
    conn, make_account
):
    ada, xavier, ben, zoe, a, b, h = build_two_levels(conn, make_account)
    for ln in both(conn, ben, "Aitch"):
        assert "have access" not in ln


def test_no_name_leaks_in_a_hub_two_levels_down(conn, make_account):
    ada, xavier, ben, zoe, a, b, h = build_two_levels(conn, make_account)
    assert_no_leak(conn, (ada, xavier, ben, zoe), h, [a, b])


def test_readers_above_two_levels_down(conn, make_account):
    ada, xavier, ben, zoe, a, b, h = build_two_levels(conn, make_account)
    h_id = sql_node_id(conn, h)
    rows, others = access.readers_above(conn, ben.person_id, h_id)
    assert [r["handle"] for r in rows] == [ben.handle]
    assert others is True
    rows, others = access.readers_above(conn, ada.person_id, h_id)
    assert [r["handle"] for r in rows] == [xavier.handle, zoe.handle, ben.handle]
    assert others is False
    # Zoe adds to the top hub, so she adds to the middle one and H too
    rows, others = access.readers_above(conn, zoe.person_id, h_id)
    assert [r["handle"] for r in rows] == [xavier.handle, zoe.handle, ben.handle]
    assert others is False


def test_named_people_and_others_make_the_line_say_and_others(conn, make_account):
    ada, xavier, ben, zoe, a, b, h = build_two_levels(conn, make_account)
    # Xavier lowers Ada to read on the top hub: she no longer may see its list
    share(conn, xavier, b, ada, "read")
    for ln in both(conn, ada, "Aitch"):
        assert after_owner(ln).startswith(
            f"shared with @{ben.handle} and others through a hub above it,"
        )
    listing = share_text(conn, ada, h)
    assert through_handles(listing) == [ben.handle]
    assert OTHERS in listing
    assert f"@{zoe.handle}" not in listing and f"@{xavier.handle}" not in listing
    line = access_line(search.get(conn, ada, ref=h))
    assert f"@{ben.handle} add (through a hub above)" in line
    assert "other people through a hub above" in line
    assert_no_leak(conn, (ada, xavier, ben, zoe), h, [a, b])


# ------------------------------------------------------------------ case 8


def test_a_hub_above_whose_people_are_all_on_the_hub_does_not_count_as_above(
    conn, make_account
):
    ada, ben = make_account(), make_account()
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, ben, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    share(conn, ada, sub, ben, "add")
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(f"shared with @{ben.handle},")
        assert "hub above" not in ln
    assert "hub above" not in share_text(conn, ada, sub)
    assert "hub above" not in search.get(conn, ada, ref=sub)
    rows, others = access.readers_above(conn, ada.person_id, sql_node_id(conn, sub))
    assert rows == [] and others is False
    assert_no_leak(conn, (ada, ben), sub, [top])


def test_a_hub_under_an_unshared_hub_of_the_same_owner_is_shared_with_nobody(
    conn, make_account
):
    ada = make_account()
    top = mk_hub(conn, ada, "Top")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith("shared with nobody,")
    assert "hub above" not in share_text(conn, ada, sub)
    assert "hub above" not in search.get(conn, ada, ref=sub)


# ------------------------------------------------------------ more owner lines


def test_more_than_three_named_people_are_cut_to_three_and_a_count(conn, make_account):
    ada = make_account()
    people = [make_account() for _ in range(5)]
    top = mk_hub(conn, ada, "Top")
    for p in people:
        share(conn, ada, top, p, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    first = ", ".join(f"@{p.handle}" for p in people[:3])
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with {first} and 2 more through a hub above it,"
        )
    assert through_handles(share_text(conn, ada, sub)) == [p.handle for p in people]


def test_a_named_person_is_named_once_when_on_two_hubs_above(conn, make_account):
    ada, xavier, ben = make_account(), make_account(), make_account()
    outer = mk_hub(conn, xavier, "Outer")
    share(conn, xavier, outer, ada, "add")
    share(conn, xavier, outer, ben, "add")
    inner = mk_hub(conn, ada, "Inner", parent=outer)
    share(conn, ada, inner, ben, "add")
    sub = mk_hub(conn, ada, "Sub", parent=inner)
    assert through_handles(share_text(conn, ada, sub)) == [xavier.handle, ben.handle]
    for ln in both(conn, ada, "Sub"):
        assert after_owner(ln).startswith(
            f"shared with @{xavier.handle}, @{ben.handle} through a hub above it,"
        )


def test_people_are_named_the_older_hub_first_and_in_the_order_granted(
    conn, make_account
):
    ada, xavier = make_account(), make_account()
    p1, p2, p3, p4 = (make_account() for _ in range(4))
    outer = mk_hub(conn, xavier, "Outer")
    share(conn, xavier, outer, ada, "add")
    inner = mk_hub(conn, ada, "Inner", parent=outer)
    sub = mk_hub(conn, ada, "Sub", parent=inner)
    # grants are made on the newer hub first, and p4 before p3 on the older one
    share(conn, ada, inner, p1, "add")
    share(conn, ada, inner, p2, "read")
    share(conn, xavier, outer, p4, "add")
    share(conn, xavier, outer, p3, "read")
    assert through_handles(share_text(conn, ada, sub)) == [
        xavier.handle,
        p4.handle,
        p3.handle,
        p1.handle,
        p2.handle,
    ]


# ------------------------------------------------------------------ case 9


def test_no_handle_is_shown_through_a_hub_above_that_the_viewer_cannot_read_there(
    conn, make_account
):
    ada, xavier, ben, zoe, a, b, h = build_two_levels(conn, make_account)
    cleo = make_account()
    share(conn, ada, a, cleo, "read")
    sibling = mk_hub(conn, ada, "Sibling", parent=a)
    for hub in (h, sibling):
        assert_no_leak(conn, (ada, xavier, ben, zoe, cleo), hub, [a, b])


# ------------------------------------------------------------------ rule 2


def test_the_count_counts_readers_and_adders_alike(conn, make_account):
    ada, ben, cleo, dan = (make_account() for _ in range(4))
    team = mk_hub(conn, ada, "Team")
    share(conn, ada, team, ben, "add")
    share(conn, ada, team, cleo, "read")
    share(conn, ada, team, dan, "add")
    for ln in both(conn, ben, "Team"):
        assert "you, the owner and 2 more have access," in ln


def test_a_revoked_person_is_not_counted(conn, make_account):
    ada, ben, cleo, dan, eve = (make_account() for _ in range(5))
    team = mk_hub(conn, ada, "Team")
    for p in (ben, cleo, dan, eve):
        share(conn, ada, team, p, "add")
    share(conn, ada, team, eve, "none")
    for ln in both(conn, ben, "Team"):
        assert "you, the owner and 2 more have access," in ln


def test_one_more_person_is_counted_as_one_more(conn, make_account):
    ada, ben, cleo = (make_account() for _ in range(3))
    team = mk_hub(conn, ada, "Team")
    share(conn, ada, team, ben, "add")
    share(conn, ada, team, cleo, "read")
    for ln in both(conn, ben, "Team"):
        assert "you, the owner and 1 more have access," in ln


def test_a_member_alone_beside_a_revoked_person_is_told_only_you_and_the_owner(
    conn, make_account
):
    ada, ben, eve = (make_account() for _ in range(3))
    team = mk_hub(conn, ada, "Team")
    share(conn, ada, team, ben, "add")
    share(conn, ada, team, eve, "read")
    share(conn, ada, team, eve, "none")
    for ln in both(conn, ben, "Team"):
        assert "only you and the owner have access," in ln


def test_a_reader_never_gets_a_count(conn, make_account):
    ada, ben, cleo, dan = (make_account() for _ in range(4))
    team = mk_hub(conn, ada, "Team")
    share(conn, ada, team, ben, "add")
    share(conn, ada, team, cleo, "read")
    share(conn, ada, team, dan, "read")
    for ln in both(conn, cleo, "Team"):
        assert "have access" not in ln


def test_a_member_with_a_grant_on_the_sub_hub_and_nobody_above_gets_a_count(
    conn, make_account
):
    ada, ben = make_account(), make_account()
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, ben, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    share(conn, ada, sub, ben, "add")
    for ln in both(conn, ben, "Sub"):
        assert "only you and the owner have access," in ln


def test_a_member_with_add_through_the_hub_above_gets_no_count(conn, make_account):
    ada, ben, dan = (make_account() for _ in range(3))
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, ben, "add")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    for ln in both(conn, ben, "Sub"):
        assert "have access" not in ln
    share(conn, ada, sub, dan, "read")
    for ln in both(conn, ben, "Sub"):
        assert "have access" not in ln


def test_a_member_who_adds_to_the_sub_hub_gets_no_count_when_others_read_above(
    conn, make_account
):
    ada, ben, cleo = (make_account() for _ in range(3))
    top = mk_hub(conn, ada, "Top")
    share(conn, ada, top, cleo, "read")
    sub = mk_hub(conn, ada, "Sub", parent=top)
    share(conn, ada, sub, ben, "add")
    for ln in both(conn, ben, "Sub"):
        assert "have access" not in ln


# ------------------------------------------------------------------ rule 3


def scenario_add_on_the_hub(conn, v, o):
    h = mk_hub(conn, o, "Hx")
    share(conn, o, h, v, "add")
    return h


def scenario_read_on_the_hub(conn, v, o):
    h = mk_hub(conn, o, "Hx")
    share(conn, o, h, v, "read")
    return h


def scenario_add_on_a_hub_above(conn, v, o):
    a = mk_hub(conn, o, "Ax")
    share(conn, o, a, v, "add")
    return mk_hub(conn, o, "Hx", parent=a)


def scenario_owner_of_a_hub_above(conn, v, o):
    a = mk_hub(conn, v, "Ax")
    share(conn, v, a, o, "add")
    return mk_hub(conn, o, "Hx", parent=a)


def scenario_read_on_the_hub_and_add_above(conn, v, o):
    a = mk_hub(conn, o, "Ax")
    share(conn, o, a, v, "add")
    h = mk_hub(conn, o, "Hx", parent=a)
    share(conn, o, h, v, "read")
    return h


def scenario_read_on_a_hub_above(conn, v, o):
    a = mk_hub(conn, o, "Ax")
    share(conn, o, a, v, "read")
    return mk_hub(conn, o, "Hx", parent=a)


def scenario_personal_hub_and_add_above(conn, v, o):
    a = mk_hub(conn, o, "Ax")
    share(conn, o, a, v, "add")
    return mk_hub(conn, o, "Hx", parent=a, preset="personal")


def scenario_personal_hub_and_owner_of_a_hub_above(conn, v, o):
    a = mk_hub(conn, v, "Ax")
    share(conn, v, a, o, "add")
    return mk_hub(conn, o, "Hx", parent=a, preset="personal")


def scenario_read_on_the_hub_and_read_above(conn, v, o):
    a = mk_hub(conn, o, "Ax")
    share(conn, o, a, v, "read")
    h = mk_hub(conn, o, "Hx", parent=a)
    share(conn, o, h, v, "read")
    return h


SCENARIOS = [
    (scenario_add_on_the_hub, "add"),
    (scenario_read_on_the_hub, "read"),
    (scenario_add_on_a_hub_above, "add (from a parent hub)"),
    (scenario_owner_of_a_hub_above, "add (from a parent hub)"),
    (scenario_read_on_the_hub_and_add_above, "add (from a parent hub)"),
    (scenario_read_on_a_hub_above, "read (from a parent hub)"),
    (scenario_personal_hub_and_add_above, "read (from a parent hub)"),
    (scenario_personal_hub_and_owner_of_a_hub_above, "read (from a parent hub)"),
    (scenario_read_on_the_hub_and_read_above, "read"),
]
IDS = [fn.__name__.removeprefix("scenario_") for fn, _ in SCENARIOS]


@pytest.mark.parametrize(("build", "word"), SCENARIOS, ids=IDS)
def test_the_word_on_the_line_of_somebody_elses_hub(conn, make_account, build, word):
    v, o = make_account(), make_account()
    h = build(conn, v, o)
    mk_note(conn, o, "a note", hub=h)
    for ln in both(conn, v, "Hx"):
        assert word_of(ln) == word, ln


@pytest.mark.parametrize(("build", "word"), SCENARIOS, ids=IDS)
def test_the_hub_listing_carries_the_same_add_or_read(conn, make_account, build, word):
    v, o = make_account(), make_account()
    build(conn, v, o)
    rows = search.search(conn, v, query="", kind="hub")
    ln = line_for(rows, "Hx")
    first = word.split()[0]
    other = "read" if first == "add" else "add"
    assert re.search(rf"\b{first}\b", ln), ln
    assert not re.search(rf"\b{other}\b", ln), ln


@pytest.mark.parametrize(("build", "word"), SCENARIOS, ids=IDS)
def test_a_note_can_be_put_in_the_hub_exactly_when_the_word_says_add(
    conn, make_account, build, word
):
    v, o = make_account(), make_account()
    h = build(conn, v, o)
    if word.startswith("add"):
        handlers.put(conn, v, text="x", hub=h)
    else:
        with pytest.raises(ReticulaError):
            handlers.put(conn, v, text="x", hub=h)


# ------------------------------------------------------------------ rule 1b


def remark(owner, cleo) -> str:
    return (
        f"@{cleo.handle} read (granted by @{owner.handle});"
        " add through a hub above this one"
    )


def build_read_grant_with_add_above(conn, make_account, cleo_above="add"):
    ada, ben, cleo = (make_account() for _ in range(3))
    project = mk_hub(conn, ada, "Project")
    share(conn, ada, project, ben, "add")
    firmware = mk_hub(conn, ben, "Firmware", parent=project)
    share(conn, ben, firmware, cleo, "read")
    share(conn, ada, project, cleo, cleo_above)
    return ada, ben, cleo, project, firmware


def test_a_read_grant_with_add_above_carries_a_remark_in_the_access_list(
    conn, make_account
):
    ada, ben, cleo, project, firmware = build_read_grant_with_add_above(
        conn, make_account
    )
    for viewer in (ada, ben, cleo):
        listing = share_text(conn, viewer, firmware)
        assert remark(ben, cleo) in listing.splitlines(), viewer.handle
        assert f"@{cleo.handle} add {THROUGH}" not in listing
        assert through_handles(listing) == [ada.handle]


def test_a_read_grant_with_add_above_carries_a_remark_in_get(conn, make_account):
    ada, ben, cleo, project, firmware = build_read_grant_with_add_above(
        conn, make_account
    )
    for viewer in (ada, ben, cleo):
        line = access_line(search.get(conn, viewer, ref=firmware))
        assert f"@{cleo.handle} read (add through a hub above)" in line
        assert f"@{cleo.handle} add (through" not in line
        assert f"@{ada.handle} add (through a hub above)" in line


def test_a_read_grant_with_add_above_is_not_among_the_readers_above(conn, make_account):
    ada, ben, cleo, project, firmware = build_read_grant_with_add_above(
        conn, make_account
    )
    for viewer in (ada, ben, cleo):
        rows, others = access.readers_above(
            conn, viewer.person_id, sql_node_id(conn, firmware)
        )
        assert cleo.person_id not in [r["person"] for r in rows]
        assert [r["handle"] for r in rows] == [ada.handle]
        assert others is False
    adders = access.adders_from_above(conn, ada.person_id, sql_node_id(conn, firmware))
    assert adders == {cleo.person_id}


def test_the_line_of_a_hub_with_a_read_grant_and_add_above_says_add_from_above(
    conn, make_account
):
    ada, ben, cleo, project, firmware = build_read_grant_with_add_above(
        conn, make_account
    )
    for ln in both(conn, cleo, "Firmware"):
        assert word_of(ln) == "add (from a parent hub)"
    handlers.put(conn, cleo, text="x", hub=firmware)


def test_the_owner_is_told_both_the_grant_and_the_person_through_the_hub_above(
    conn, make_account
):
    ada, ben, cleo, project, firmware = build_read_grant_with_add_above(
        conn, make_account
    )
    for ln in both(conn, ben, "Firmware"):
        assert after_owner(ln).startswith(
            f"shared with @{cleo.handle}, and with @{ada.handle}"
            " through a hub above it,"
        )


def test_a_read_grant_with_only_read_above_has_no_remark(conn, make_account):
    ada, ben, cleo, project, firmware = build_read_grant_with_add_above(
        conn, make_account, cleo_above="read"
    )
    for viewer in (ada, ben):
        listing = share_text(conn, viewer, firmware)
        assert f"@{cleo.handle} read (granted by @{ben.handle})" in listing
        assert "add through a hub above" not in listing
        line = access_line(search.get(conn, viewer, ref=firmware))
        assert f"@{cleo.handle} read" in line
        assert "add through a hub above" not in line
    for ln in both(conn, cleo, "Firmware"):
        assert word_of(ln) == "read"


def test_a_read_grant_with_add_above_on_a_personal_hub_has_no_remark(
    conn, make_account
):
    ada, ben, cleo = (make_account() for _ in range(3))
    project = mk_hub(conn, ada, "Project")
    share(conn, ada, project, ben, "add")
    firmware = mk_hub(conn, ben, "Firmware", parent=project, preset="personal")
    share(conn, ben, firmware, cleo, "read")
    share(conn, ada, project, cleo, "add")
    listing = share_text(conn, ben, firmware)
    assert f"@{cleo.handle} read (granted by @{ben.handle})" in listing
    assert "add through a hub above" not in listing
    line = access_line(search.get(conn, ben, ref=firmware))
    assert f"@{cleo.handle} read" in line
    assert "add through a hub above" not in line
    with pytest.raises(ReticulaError):
        handlers.put(conn, cleo, text="x", hub=firmware)


def test_the_remark_is_not_shown_to_an_owner_who_may_not_see_the_hub_above(
    conn, make_account
):
    ada, xavier, dan = (make_account() for _ in range(3))
    top = mk_hub(conn, xavier, "Top")
    share(conn, xavier, top, ada, "add")
    corner = mk_hub(conn, ada, "Corner", parent=top)
    share(conn, xavier, top, dan, "add")
    share(conn, ada, corner, dan, "read")
    remark_dan = (
        f"@{dan.handle} read (granted by @{ada.handle});"
        " add through a hub above this one"
    )
    assert remark_dan in share_text(conn, ada, corner).splitlines()
    line = access_line(search.get(conn, ada, ref=corner))
    assert f"@{dan.handle} read (add through a hub above)" in line

    share(conn, xavier, top, ada, "read")

    listing = share_text(conn, ada, corner)
    assert f"@{dan.handle} read (granted by @{ada.handle})" in listing
    assert "add through a hub above" not in listing
    line = access_line(search.get(conn, ada, ref=corner))
    assert f"@{dan.handle} read" in line
    assert "add through a hub above" not in line
    assert_no_leak(conn, (ada, xavier, dan), corner, [top])
