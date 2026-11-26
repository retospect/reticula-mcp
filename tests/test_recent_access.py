"""What ``search(query="", since=...)`` shows a viewer: nothing they cannot read.

The rule under test: a note the viewer cannot read does not appear, is not
counted in the total, is not counted in "by other people", and does not change
the number of the ``... N more`` line. Each test builds a scene in which the
viewer (``ana``) legitimately sees several recent events by ``bea``, and one
thing she must not see, asks for one line at a time (so that the ``more`` line
exists), and then for all of them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import NamedTuple

import pytest

from reticula_mcp import accounts
from reticula_mcp.accounts import Account
from reticula_mcp.content import hooks as content_hooks
from reticula_mcp.db.pool import Conn
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import render
from reticula_mcp.search.handlers import search
from tests.graph_support import delete_now, mk_hub, mk_note, share
from tests.test_recent_support import (
    Clock,
    by_others,
    line_of,
    listed,
    more,
    short,
    shown_and_total,
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def people(
    conn: Conn, make_account: Callable[..., Account]
) -> tuple[Account, Account, Account]:
    """``ana`` (the viewer), ``bea`` (writes) and ``cyd`` (has no share at all),
    on an instance whose shipped instructions are seeded: they are readable by
    everybody and must never show up in a listing of recent events."""
    accounts.ensure_system_account(conn)
    content_hooks.on_migrated(conn)
    return (
        make_account(handle="ana"),
        make_account(handle="bea"),
        make_account(handle="cyd"),
    )


def recent(conn: Conn, viewer: Account, limit: int = 50) -> str:
    return search(conn, viewer, query="", since="7d", limit=limit)


def expect(
    conn: Conn,
    viewer: Account,
    expected: Sequence[str],
    hidden: Sequence[str],
    *,
    others: int,
    words: Sequence[str] = (),
) -> str:
    """Check the listing of ``viewer`` against ``expected`` (addresses) with one
    line per call and with all lines; ``hidden`` (addresses) and ``words`` (text
    of what must stay hidden) appear nowhere. Returns the full listing."""
    total = len(expected)
    assert total >= 2, "the scene must leave the viewer at least two events"
    one = recent(conn, viewer, limit=1)
    full = recent(conn, viewer)
    for out in (one, full):
        for address in hidden:
            assert short(address) not in out, (address, out)
        for word in words:
            assert word not in out, (word, out)
    assert shown_and_total(one) == (1, total), one
    assert more(one) == total - 1, one
    assert shown_and_total(full) == (total, total), full
    assert more(full) == 0, full
    assert sorted(listed(full)) == sorted(short(a) for a in expected), full
    assert by_others(full) == others, full
    return full


# ------------------------------------------------------------------ scenes


class Board(NamedTuple):
    hub: str
    first: str
    second: str
    private: str


def private_in_shared(conn: Conn, ana: Account, bea: Account) -> Board:
    """bea's hub, shared with ana (read) first; then two notes, and a private
    one, written in it."""
    hub = mk_hub(conn, bea, "Shared board")
    share(conn, bea, hub, ana, "read")
    first = mk_note(conn, bea, "VISIBLEALPHA first note", hub=hub)
    second = mk_note(conn, bea, "VISIBLEBETA second note", hub=hub)
    private = mk_note(conn, bea, "SECRETGAMMA private note", hub=hub, private=True)
    return Board(hub, first, second, private)


class Late(NamedTuple):
    hub: str
    old_one: str
    old_two: str
    new: str
    moments: dict[int, str]  # days ago -> the date the listing would print


def shared_later(
    conn: Conn, clock: Clock, ana: Account, bea: Account, *, via_other_hub: bool
) -> Late:
    """Two notes written in bea's hub 5 days ago (one edited 4 days ago), the
    hub shared with ana 2 days ago, a third note written 1 day ago. With
    ``via_other_hub`` ana already read the first old note through another hub
    that bea shared with her 30 days ago, where bea wrote it."""
    other = ""
    if via_other_hub:
        with clock.ago(days=31):
            other = mk_hub(conn, bea, "Older hub")
        with clock.ago(days=30):
            share(conn, bea, other, ana, "read")
    dates: dict[int, str] = {}
    with clock.ago(days=6) as t6:
        hub = mk_hub(conn, bea, "Late hub")
    with clock.ago(days=5) as t5:
        if via_other_hub:
            old_one = mk_note(conn, bea, "LATEONE old note", hub=other)
            graph.link(conn, bea, ref=old_one, to=hub)
        else:
            old_one = mk_note(conn, bea, "LATEONE old note", hub=hub)
        old_two = mk_note(conn, bea, "LATETWO old note", hub=hub)
    with clock.ago(days=4) as t4:
        graph.edit(conn, bea, ref=old_two, text="LATETWO old note, corrected")
    with clock.ago(days=2):
        share(conn, bea, hub, ana, "read")
    with clock.ago(days=1):
        new = mk_note(conn, bea, "LATETHREE new note", hub=hub)
    for days, moment in ((6, t6), (5, t5), (4, t4)):
        dates[days] = render.stamp(moment)[:10]
    return Late(hub, old_one, old_two, new, dates)


# ------------------------------------------------------------------- 1 to 7


def test_a_private_note_in_a_shared_hub_is_not_listed_or_counted(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """bea's private note in a hub she shared appears nowhere and is in no count."""
    ana, bea, _ = people(conn, make_account)
    board = private_in_shared(conn, ana, bea)
    full = expect(
        conn,
        ana,
        [board.hub, board.first, board.second],
        [board.private],
        others=3,
        words=["SECRETGAMMA"],
    )
    # the hub line says how much ana can read in it, not how much it holds
    assert "read · 2 notes" in line_of(full, board.hub)
    assert "3 notes" not in full


def test_a_hub_above_a_shared_sub_hub_is_not_listed(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """When only the sub-hub is shared, the parent hub and its notes stay hidden."""
    ana, bea, _ = people(conn, make_account)
    parent = mk_hub(conn, bea, "Hidden parent hub")
    sub = mk_hub(conn, bea, "Open sub hub", parent=parent)
    share(conn, bea, sub, ana, "read")
    above = mk_note(conn, bea, "HIDDENUP note in the parent", hub=parent)
    one = mk_note(conn, bea, "VISIBLEONE in the sub-hub", hub=sub)
    two = mk_note(conn, bea, "VISIBLETWO in the sub-hub", hub=sub)
    full = expect(
        conn,
        ana,
        [sub, one, two],
        [parent, above],
        others=3,
        words=["Hidden parent hub", "HIDDENUP"],
    )
    assert "read · 2 notes" in line_of(full, sub)


def test_a_share_taken_away_removes_the_hub_and_its_notes(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """After bea ends the share, ana sees neither the hub nor what was in it."""
    ana, bea, _ = people(conn, make_account)
    gone = mk_hub(conn, bea, "Withdrawn hub")
    share(conn, bea, gone, ana, "read")
    g1 = mk_note(conn, bea, "WITHDRAWNONE note", hub=gone)
    g2 = mk_note(conn, bea, "WITHDRAWNTWO note", hub=gone)
    before = recent(conn, ana)
    assert {short(gone), short(g1), short(g2)} <= set(listed(before)), before
    share(conn, bea, gone, ana, "none")
    kept = mk_hub(conn, bea, "Kept hub")
    share(conn, bea, kept, ana, "read")
    k1 = mk_note(conn, bea, "KEPTONE note", hub=kept)
    k2 = mk_note(conn, bea, "KEPTTWO note", hub=kept)
    expect(
        conn,
        ana,
        [kept, k1, k2],
        [gone, g1, g2],
        others=3,
        words=["Withdrawn hub", "WITHDRAWN"],
    )


def test_a_note_deleted_after_it_was_listed_is_gone_and_not_counted(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """A deleted note's own line goes; in its place a bare line says it was
    deleted, with nothing of what it said, and the total stays the same."""
    ana, bea, _ = people(conn, make_account)
    hub = mk_hub(conn, bea, "Board")
    share(conn, bea, hub, ana, "read")
    a = mk_note(conn, bea, "KEEPONE note", hub=hub)
    b = mk_note(conn, bea, "KEEPTWO note", hub=hub)
    c = mk_note(conn, bea, "DELETEDTEXT note", hub=hub)
    before = recent(conn, ana)
    assert short(c) in listed(before), before
    assert shown_and_total(before) == (4, 4), before
    delete_now(conn, bea, c)
    after = recent(conn, ana)
    assert shown_and_total(after) == (4, 4), after
    assert f"- {short(c)} note deleted by @bea · now" in after.splitlines()
    assert "DELETEDTEXT" not in after
    assert "read · 2 notes" in line_of(after, hub)


def test_a_hub_set_to_private_after_sharing_disappears_with_its_notes(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """Once bea makes the shared hub private, ana sees nothing of it."""
    ana, bea, _ = people(conn, make_account)
    closed = mk_hub(conn, bea, "Closed again hub")
    share(conn, bea, closed, ana, "read")
    c1 = mk_note(conn, bea, "CLOSEDONE note", hub=closed)
    c2 = mk_note(conn, bea, "CLOSEDTWO note", hub=closed)
    before = recent(conn, ana)
    assert {short(closed), short(c1), short(c2)} <= set(listed(before)), before
    graph.edit(conn, bea, ref=closed, private=True)
    open_hub = mk_hub(conn, bea, "Open hub")
    share(conn, bea, open_hub, ana, "read")
    o1 = mk_note(conn, bea, "OPENONE note", hub=open_hub)
    o2 = mk_note(conn, bea, "OPENTWO note", hub=open_hub)
    expect(
        conn,
        ana,
        [open_hub, o1, o2],
        [closed, c1, c2],
        others=3,
        words=["Closed again hub", "CLOSED"],
    )


def test_a_note_taken_out_of_the_shared_hub_is_gone(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """A note its author took out of the shared hub (and put in a hub ana cannot
    read) is no longer listed as a note: a bare line says it was taken out,
    with nothing of what it said and nothing of the hub it went to."""
    ana, bea, _ = people(conn, make_account)
    hub = mk_hub(conn, bea, "Board")
    share(conn, bea, hub, ana, "read")
    a = mk_note(conn, bea, "STAYSONE note", hub=hub)
    b = mk_note(conn, bea, "STAYSTWO note", hub=hub)
    moved = mk_note(conn, bea, "REMOVEDTEXT note", hub=hub)
    elsewhere = mk_hub(conn, bea, "Elsewhere hub")
    graph.link(conn, bea, ref=moved, to=elsewhere)
    before = recent(conn, ana)
    assert short(moved) in listed(before), before
    graph.link(conn, bea, ref=moved, to=hub, remove=True)
    after = recent(conn, ana, limit=50)
    lines = after.splitlines()
    assert f'- {short(moved)} note taken out of hub "Board" by @bea · now' in lines
    assert [ln for ln in lines if short(moved) in ln] == [
        f'- {short(moved)} note taken out of hub "Board" by @bea · now'
    ]
    assert short(elsewhere) not in after
    for word in ("REMOVEDTEXT", "Elsewhere hub"):
        assert word not in after
    assert {short(hub), short(a), short(b)} <= set(listed(after))


def test_corrects_never_names_a_note_the_viewer_cannot_read(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """A correction of a private note shows no ``corrects`` and no trace of it."""
    ana, bea, _ = people(conn, make_account)
    hub = mk_hub(conn, bea, "Board")
    share(conn, bea, hub, ana, "read")
    other = mk_note(conn, bea, "OTHERNOTE readable", hub=hub)
    old = mk_note(
        conn,
        bea,
        "OLDSECRET text",
        hub=hub,
        private=True,
        title="Old private title",
    )
    new = mk_note(conn, bea, "NEWREADABLE text", hub=hub, supersedes=[old])
    full = expect(
        conn,
        ana,
        [hub, other, new],
        [old],
        others=3,
        words=["Old private title", "OLDSECRET"],
    )
    assert "corrects" not in full
    assert "corrects" not in line_of(full, new)


# ------------------------------------------------------------------- 8 and 9


def test_a_hub_shared_later_hides_what_it_held_before_and_its_dates(
    conn: Conn, make_account: Callable[..., Account], clock: Clock
) -> None:
    """The hub is one line; the notes written and edited before the share are
    not listed and none of their dates is printed."""
    ana, bea, _ = people(conn, make_account)
    scene = shared_later(conn, clock, ana, bea, via_other_hub=False)
    full = expect(
        conn,
        ana,
        [scene.hub, scene.new],
        [scene.old_one, scene.old_two],
        others=2,
        words=[scene.moments[5], scene.moments[4], "LATEONE", "LATETWO"],
    )
    hub_line = line_of(full, scene.hub)
    assert "shared with you by @bea" in hub_line
    assert "read · 3 notes" in hub_line
    assert full.count("shared with you") == 1
    assert "written" in line_of(full, scene.new)


def test_a_note_ana_could_read_all_along_is_listed_when_the_hub_is_shared_later(
    conn: Conn, make_account: Callable[..., Account], clock: Clock
) -> None:
    """The note ana could already read through an older share is listed; the
    other old note, which she could not, is not."""
    ana, bea, _ = people(conn, make_account)
    scene = shared_later(conn, clock, ana, bea, via_other_hub=True)
    full = expect(
        conn,
        ana,
        [scene.hub, scene.old_one, scene.new],
        [scene.old_two],
        others=3,
        words=[scene.moments[4], "LATETWO"],
    )
    assert "written" in line_of(full, scene.old_one)
    assert "shared with you by @bea" in line_of(full, scene.hub)
    assert "read · 3 notes" in line_of(full, scene.hub)
    others = [
        ln for ln in full.splitlines() if not ln.startswith(f"- {short(scene.old_one)}")
    ]
    assert not any(scene.moments[5] in ln for ln in others), full


def test_a_change_of_access_is_not_a_new_share(
    conn: Conn, make_account: Callable[..., Account], clock: Clock
) -> None:
    """read changed to add two days ago: the hub is not listed as shared again,
    and what was written since the first share is listed note by note."""
    ana, bea, _ = people(conn, make_account)
    with clock.ago(days=31):
        hub = mk_hub(conn, bea, "Long shared hub")
    with clock.ago(days=30):
        share(conn, bea, hub, ana, "read")
    with clock.ago(days=5):
        one = mk_note(conn, bea, "SINCEONE note", hub=hub)
    with clock.ago(days=4):
        two = mk_note(conn, bea, "SINCETWO note", hub=hub)
    with clock.ago(days=2):
        share(conn, bea, hub, ana, "add")
    full = expect(conn, ana, [one, two], [], others=2)
    assert "shared with you" not in full
    assert short(hub) not in listed(full)


def test_a_share_given_again_after_it_was_taken_away_starts_anew(
    conn: Conn, make_account: Callable[..., Account], clock: Clock
) -> None:
    """Taken away four days ago and given again two days ago: the hub is one
    line, shared two days ago, and the note written before the break is not
    listed and its date is not printed."""
    ana, bea, _ = people(conn, make_account)
    with clock.ago(days=31):
        hub = mk_hub(conn, bea, "Again hub")
    with clock.ago(days=30):
        share(conn, bea, hub, ana, "read")
    with clock.ago(days=5) as t5:
        old = mk_note(conn, bea, "BEFOREBREAK note", hub=hub)
    with clock.ago(days=4):
        share(conn, bea, hub, ana, "none")
    with clock.ago(days=2) as t2:
        share(conn, bea, hub, ana, "add")
    with clock.ago(days=1):
        new = mk_note(conn, bea, "AFTERBREAK note", hub=hub)
    full = expect(
        conn,
        ana,
        [hub, new],
        [old],
        others=2,
        words=["BEFOREBREAK", render.stamp(t5)[:10]],
    )
    hub_line = line_of(full, hub)
    assert f"shared with you by @bea {render.stamp(t2)}" in hub_line
    assert "add · 2 notes" in hub_line


# ------------------------------------------------------------------------- 10


def test_a_hub_filed_later_under_a_shared_hub_is_one_line(
    conn: Conn, make_account: Callable[..., Account], clock: Clock
) -> None:
    """bea files her own hub under a hub ana reads: the hub is listed as filed,
    and what it held before is not listed one by one."""
    ana, bea, _ = people(conn, make_account)
    with clock.ago(days=31):
        parent = mk_hub(conn, bea, "Parent hub")
    with clock.ago(days=30):
        share(conn, bea, parent, ana, "read")
    with clock.ago(days=6):
        loose = mk_hub(conn, bea, "Loose hub")
    with clock.ago(days=5):
        s1 = mk_note(conn, bea, "LOOSEONE note", hub=loose)
        s2 = mk_note(conn, bea, "LOOSETWO note", hub=loose)
    with clock.ago(days=2):
        graph.link(conn, bea, ref=loose, to=parent)
    fresh = mk_note(conn, bea, "FRESHINPARENT note", hub=parent)
    full = expect(
        conn,
        ana,
        [loose, fresh],
        [s1, s2],
        others=2,
        words=["LOOSEONE", "LOOSETWO"],
    )
    assert "filed in this hub" in line_of(full, loose)
    assert listed(full) == [short(fresh), short(loose)], full


# ------------------------------------------------------------------------- 11


def test_a_person_without_any_share_sees_nothing_of_the_scenes(
    conn: Conn, make_account: Callable[..., Account], clock: Clock
) -> None:
    """cyd, who has no share, gets the empty answer for scenes ana reads."""
    ana, bea, cyd = people(conn, make_account)
    board = private_in_shared(conn, ana, bea)
    late = shared_later(conn, clock, ana, bea, via_other_hub=False)
    out = recent(conn, cyd)
    assert out.startswith("new since "), out
    assert "nothing that you can read" in out, out
    assert listed(out) == []
    for address in (*board, late.hub, late.old_one, late.old_two, late.new):
        assert short(address) not in out
    for word in (
        "Shared board",
        "VISIBLEALPHA",
        "SECRETGAMMA",
        "Late hub",
        "LATEONE",
        "LATETHREE",
        "@bea",
    ):
        assert word not in out, (word, out)
    # ana sees the scenes, so the empty answer is cyd's alone
    assert short(board.hub) in listed(recent(conn, ana))


# ------------------------------------------------------------------------- 12


def test_own_notes_are_listed_for_the_author_only(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """ana's private note, her note in her own unshared hub and her unfiled note
    are listed for her, marked (you), and not for bea."""
    ana, bea, _ = people(conn, make_account)
    mine = mk_hub(conn, ana, "Ana's own hub")
    inside = mk_note(conn, ana, "OWNINSIDE note", hub=mine)
    private = mk_note(conn, ana, "OWNPRIVATE note", private=True)
    loose = mk_note(conn, ana, "OWNLOOSE note")
    out = recent(conn, ana)
    assert sorted(listed(out)) == sorted(
        short(a) for a in (mine, inside, private, loose)
    )
    assert shown_and_total(out) == (4, 4), out
    assert by_others(out) == 0, out
    for address in (inside, private, loose):
        line = line_of(out, address)
        assert line.endswith(" (you)"), line
        assert "@ana" not in line, line
    assert "(you)" in line_of(out, mine)
    assert "written" in line_of(out, private)
    # bea reads none of it
    theirs = recent(conn, bea)
    assert "nothing that you can read" in theirs, theirs
    assert listed(theirs) == []
    for word in ("OWNINSIDE", "OWNPRIVATE", "OWNLOOSE", "Ana's own hub"):
        assert word not in theirs


def test_a_private_note_in_a_hub_shared_with_bea_is_ana_only(
    conn: Conn, make_account: Callable[..., Account]
) -> None:
    """ana's private note in a hub she shared is listed for ana, not for bea,
    and bea's counts and totals do not include it."""
    ana, bea, _ = people(conn, make_account)
    hub = mk_hub(conn, ana, "Ana's shared hub")
    share(conn, ana, hub, bea, "read")
    public = mk_note(conn, ana, "PUBLICNOTE for bea", hub=hub)
    private = mk_note(conn, ana, "PRIVATENOTE only ana", hub=hub, private=True)
    mine = recent(conn, ana)
    assert sorted(listed(mine)) == sorted(short(a) for a in (hub, public, private))
    assert line_of(mine, private).endswith(" (you)")
    full = expect(conn, bea, [hub, public], [private], others=2, words=["PRIVATENOTE"])
    assert "shared with you by @ana" in line_of(full, hub)
    assert "read · 1 note" in line_of(full, hub)
    assert "1 notes" not in full
