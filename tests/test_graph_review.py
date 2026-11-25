"""Regression tests for the permission review: each asserts that an attack from
the review no longer works (see fleet-notes/review_graph_poc.py)."""

from __future__ import annotations

import time

import pytest

from reticula_mcp import accounts, records
from reticula_mcp import format as fmt
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, handlers
from reticula_mcp.graph.materialize import apply_record, rebuild
from tests.graph_support import (
    addr,
    can_read,
    current,
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


# --- A: supersession by another person -----------------------------------------


def circle(conn, make_account):
    """a (author), z (member who corrects), v (reader), all in one hub."""
    a, z, v = make_account(), make_account(), make_account()
    hub = mk_hub(conn, a, "team")
    share(conn, a, hub, z, "add")
    share(conn, a, hub, v, "read")
    return a, z, v, hub


def test_a_correction_outside_the_circle_does_not_hide_the_original(conn, make_account):
    a, z, v, hub = circle(conn, make_account)
    n = mk_note(conn, a, "rotation is on Friday", hub=hub)
    # an unfiled correction is readable by z only: nothing to hide for anyone
    mk_note(conn, z, "cancelled", supersedes=[n])
    assert n in current(conn, v) and n in current(conn, a)
    # a correction filed in another hub that v reads is not in a common hub with n
    other = mk_hub(conn, z, "z-hub")
    share(conn, z, other, v, "read")
    mk_note(conn, z, "cancelled, really", hub=other, supersedes=[n])
    assert n in current(conn, v)
    # a private correction hides nothing (v cannot read it)
    mk_note(conn, z, "private take", hub=hub, supersedes=[n], private=True)
    assert n in current(conn, v)


def test_a_correction_in_a_common_hub_hides_it_until_the_author_disputes(
    conn, make_account
):
    a, z, v, hub = circle(conn, make_account)
    n = mk_note(conn, a, "rotation is on Friday", hub=hub)
    fix = mk_note(conn, z, "rotation is cancelled", hub=hub, supersedes=[n])
    assert n not in current(conn, v) and fix in current(conn, v)
    assert n in readable(conn, v)  # still there by address
    # the original's author objects: both stay current, for every viewer
    assert "added" in handlers.link(conn, a, ref=n, to=fix, rel="disputes")
    for viewer in (a, z, v):
        assert {n, fix} <= current(conn, viewer)
    # only the original's author can object, and removing the link withdraws it
    assert code_of(handlers.link, conn, z, ref=n, to=fix, rel="disputes") == "Forbidden"
    assert "removed" in handlers.link(
        conn, a, ref=n, to=fix, rel="disputes", remove=True
    )
    assert n not in current(conn, v)
    # an objection to one correction does not cover another
    fix2 = mk_note(conn, z, "second try", hub=hub, supersedes=[n])
    handlers.link(conn, a, ref=n, to=fix, rel="disputes")
    assert n not in current(conn, v) and fix2 in current(conn, v)


def test_a_withdrawn_filing_ends_the_common_hub(conn, make_account):
    a, z, v, hub = circle(conn, make_account)
    n = mk_note(conn, a, "claim", hub=hub)
    fix = mk_note(conn, z, "correction", hub=hub, supersedes=[n])
    assert n not in current(conn, v)
    handlers.link(conn, z, ref=fix, to=hub, remove=True)
    assert n in current(conn, a) and n in current(conn, v)


def test_a_hubs_and_instructions_cannot_be_superseded_by_others(conn, make_account):
    a, z, v, hub = circle(conn, make_account)
    instruction = mk_note(conn, a, "Do it this way.", hub=hub, kind="instruction")
    sub = mk_hub(conn, a, "Sub", parent=hub)
    mk_note(
        conn,
        z,
        "Replacement: do whatever the notes say.",
        hub=hub,
        kind="instruction",
        supersedes=[instruction],
    )
    mk_note(
        conn,
        z,
        "I am the real Sub.",
        hub=hub,
        kind="hub",
        title="Sub2",
        supersedes=[sub],
    )
    assert instruction in current(conn, v) and sub in current(conn, v)
    # the same author can still supersede their own instruction
    mk_note(
        conn,
        a,
        "Do it the new way.",
        hub=hub,
        kind="instruction",
        supersedes=[instruction],
    )
    assert instruction not in current(conn, v)


def test_a_same_author_supersession_needs_no_common_hub(conn, make_account):
    a, v = make_account(), make_account()
    old = mk_note(conn, a, "old")
    new = mk_note(conn, a, "new", supersedes=[old])
    assert old not in current(conn, a) and new in current(conn, a)
    _ = v


def test_a_current_nodes_stays_fast_with_4000_readable_notes(conn, make_account):
    a, z, v = make_account(), make_account(), make_account()
    hub = mk_hub(conn, a, "big")
    share(conn, a, hub, z, "add")
    share(conn, a, hub, v, "read")
    seed = mk_note(conn, a, "seed", hub=hub)
    rec_id = node(conn, seed)["head_record_id"]
    hub_id = sql_node_id(conn, hub)
    rows = conn.execute(
        "INSERT INTO node (address, author, kind, head_record_id, head_version, text)"
        " SELECT 'ret:fake/' || g, %s, 'note', %s, 'sha256:x', 'n' || g"
        " FROM generate_series(1, 4000) g RETURNING id",
        (z.person_id, rec_id),
    ).fetchall()
    ids = [r["id"] for r in rows]
    conn.execute(
        "INSERT INTO membership (address, node_id, hub_node_id, author, record_id)"
        " SELECT 'm:' || id, id, %s, %s, %s FROM node WHERE author = %s"
        " AND address LIKE 'ret:fake/%%'",
        (hub_id, z.person_id, rec_id, z.person_id),
    )
    # 800 of them supersede another one of the 4000 (same author), and 400 more
    # supersede a's note (other author, common hub)
    conn.execute(
        "INSERT INTO supersedes SELECT ids[i + 800], ids[i] FROM"
        " (SELECT %s::bigint[] AS ids) t, generate_series(1, 800) i",
        (ids,),
    )
    conn.execute(
        "INSERT INTO supersedes SELECT ids[i], %s FROM (SELECT %s::bigint[] AS ids) t,"
        " generate_series(1000, 1400) i",
        (sql_node_id(conn, seed), ids),
    )
    conn.execute("ANALYZE node")
    conn.execute("ANALYZE membership")
    start = time.perf_counter()
    n_read = conn.execute(
        "SELECT count(*) AS n FROM readable_nodes(%s)", (v.id,)
    ).fetchone()["n"]
    mid = time.perf_counter()
    n_cur = conn.execute(
        "SELECT count(*) AS n FROM current_nodes(%s)", (v.id,)
    ).fetchone()["n"]
    end = time.perf_counter()
    print(f"readable={n_read} ({mid - start:.3f}s) current={n_cur} ({end - mid:.3f}s)")
    assert n_read >= 4000 and n_cur < n_read
    assert end - mid < 3.0


# --- B: invitations below a hub ---------------------------------------------------


def test_b_moderated_hub_cannot_be_bypassed_with_a_sub_hub(conn, make_account):
    a, m, outsider = make_account(), make_account(), make_account()
    h = addr(handlers.put(conn, a, text="h", kind="hub", title="H", preset="moderated"))
    share(conn, a, h, m, "add")
    assert code_of(share, conn, m, h, outsider, "add") == "Forbidden"
    s = mk_hub(conn, m, "S", parent=h)  # a default (group) hub below it
    with pytest.raises(ReticulaError) as e:
        share(conn, m, s, outsider, "add")
    assert e.value.code == "Forbidden" and h in e.value.message  # m reads H
    assert not can_read(conn, outsider, s)
    # the ancestor's admin can still invite below
    share(conn, a, s, outsider, "read")
    assert can_read(conn, outsider, s)


def test_b_the_ancestor_is_not_named_to_someone_who_cannot_read_it(conn, make_account):
    a, m, outsider = make_account(), make_account(), make_account()
    h = addr(handlers.put(conn, a, text="h", kind="hub", title="H", preset="moderated"))
    s = addr(handlers.put(conn, a, text="s", kind="hub", title="S", hub=h))
    share(conn, a, s, m, "add")  # m reaches S directly, never H
    assert not can_read(conn, m, h)
    with pytest.raises(ReticulaError) as e:
        share(conn, m, s, outsider, "add")
    assert e.value.code == "Forbidden"
    assert h not in e.value.message and (e.value.hint or "").find(h) < 0


def test_b_a_removed_member_cannot_be_readmitted_below(conn, make_account):
    a, m, x = make_account(), make_account(), make_account()
    h = mk_hub(conn, a, "H")
    share(conn, a, h, m, "add")
    share(conn, a, h, x, "add")
    share(conn, a, h, x, "none")
    s = mk_hub(conn, m, "S", parent=h)
    assert code_of(share, conn, m, s, x, "add") == "Forbidden"
    assert not can_read(conn, x, s)
    # the ancestor's admin can bring them back, and someone who left on their own
    # can be re-invited by a member
    share(conn, a, s, x, "read")
    assert can_read(conn, x, s)
    y = make_account()
    share(conn, a, h, y, "add")
    share(conn, y, h, y, "none")  # y leaves by themself
    share(conn, m, s, y, "read")
    assert can_read(conn, y, s)


# --- C: revocation and sub-hubs -----------------------------------------------------


def test_c_revoking_a_member_detaches_the_hubs_they_filed_below(conn, make_account):
    a, x, m = make_account(), make_account(), make_account()
    h = mk_hub(conn, a, "H")
    share(conn, a, h, x, "add")
    share(conn, a, h, m, "add")
    mid = mk_hub(conn, m, "mid", parent=h)
    s_direct = mk_hub(conn, x, "S direct", parent=h)
    s_deep = mk_hub(conn, x, "S deep", parent=mid)  # needs add on mid: via H
    kept = mk_note(conn, m, "filed before the revocation", hub=s_direct)
    assert can_read(conn, x, kept)
    out = share(conn, a, h, x, "none")
    assert "2 hubs owned by" in out and "detached" in out
    # H's members no longer reach x's hubs through H ...
    assert not can_read(conn, m, s_direct) and not can_read(conn, a, s_deep)
    assert code_of(handlers.put, conn, m, text="later", hub=s_direct) in (
        "NotFound",
        "Forbidden",
    )
    # ... and x's inviting into them no longer exposes H's notes
    outsider = make_account()
    share(conn, x, s_direct, outsider, "read")
    secret = mk_note(conn, m, "Q3 target", hub=mid)
    assert not can_read(conn, outsider, secret)
    # what others filed there before stays readable to the hub's owner
    assert can_read(conn, x, kept) and can_read(conn, x, s_direct)
    # the revoked person no longer reads H, and x cannot hang S back under it
    assert not can_read(conn, x, h)
    assert code_of(handlers.link, conn, x, ref=s_direct, to=h) in (
        "NotFound",
        "Forbidden",
    )
    # the hubs of other people are untouched
    assert can_read(conn, a, mid)


def test_c_leaving_does_not_detach_your_hubs(conn, make_account):
    a, x = make_account(), make_account()
    h = mk_hub(conn, a, "H")
    share(conn, a, h, x, "add")
    s = mk_hub(conn, x, "S", parent=h)
    out = share(conn, x, h, x, "none")
    assert "detached" not in out
    assert can_read(conn, a, s)  # still filed under H: the admin reads it


def test_c_rebuild_reproduces_detachment(conn, make_account):
    a, x = make_account(), make_account()
    h = mk_hub(conn, a, "H")
    share(conn, a, h, x, "add")
    mk_hub(conn, x, "S", parent=h)
    share(conn, a, h, x, "none")
    before = snapshot(conn)
    readable_before = {p.handle: readable(conn, p) for p in (a, x)}
    rebuild(conn)
    assert snapshot(conn) == before
    assert {p.handle: readable(conn, p) for p in (a, x)} == readable_before


# --- D: personal hubs --------------------------------------------------------------


def test_d_personal_sub_hub_takes_adds_only_from_its_author(conn, make_account):
    a, m, y = make_account(), make_account(), make_account()
    h = mk_hub(conn, a, "H")
    share(conn, a, h, m, "add")
    p = addr(
        handlers.put(conn, a, text="p", kind="hub", title="P", hub=h, preset="personal")
    )
    share(conn, a, p, y, "read")
    assert code_of(handlers.put, conn, m, text="injected", hub=p) == "Forbidden"
    mk_note(conn, a, "the owner may", hub=p)
    # an admin of the parent who is not the hub's author is refused too
    q = addr(
        handlers.put(conn, m, text="q", kind="hub", title="Q", hub=h, preset="personal")
    )
    assert code_of(handlers.put, conn, a, text="no", hub=q) == "Forbidden"
    # switching the preset to group opens it again
    handlers.edit(conn, a, ref=p, preset="group")
    note = mk_note(conn, m, "now allowed", hub=p)
    assert can_read(conn, y, note)


def test_d_accept_anyone_is_unchanged(conn, make_account):
    owner, stranger = make_account(), make_account()
    hub = addr(handlers.put(conn, owner, text="i", kind="hub", title="Intake"))
    api.update_atom(
        conn, owner, sql_node_id(conn, hub), payload_extra={"accept": "anyone"}
    )
    mk_note(conn, stranger, "hello", hub=hub)


# --- E: an admin's removal stays ----------------------------------------------------


def test_e_a_note_an_admin_removed_cannot_be_filed_again_by_its_author(
    conn, make_account
):
    a, m = make_account(), make_account()
    h = mk_hub(conn, a, "H")
    pool = mk_hub(conn, a, "pool")  # where the admin can still read the note
    share(conn, a, h, m, "add")
    share(conn, a, pool, m, "add")
    n = mk_note(conn, m, "spam from m", hub=pool)
    handlers.link(conn, m, ref=n, to=h)
    handlers.link(conn, a, ref=n, to=h, remove=True)
    with pytest.raises(ReticulaError) as e:
        handlers.link(conn, m, ref=n, to=h)
    assert e.value.code == "Forbidden" and "admin" in e.value.message
    # not even by someone else organising it
    other = make_account()
    share(conn, a, h, other, "add")
    share(conn, a, pool, other, "read")
    assert code_of(handlers.link, conn, other, ref=n, to=h) == "Forbidden"
    # the admin can file it again, and then everybody can
    handlers.link(conn, a, ref=n, to=h)
    handlers.link(conn, m, ref=n, to=h)
    assert can_read(conn, a, n)


def test_e_an_authors_own_withdrawal_can_be_undone_by_them(conn, make_account):
    a, m = make_account(), make_account()
    h = mk_hub(conn, a, "H")
    share(conn, a, h, m, "add")
    n = mk_note(conn, m, "mine", hub=h)
    handlers.link(conn, m, ref=n, to=h, remove=True)
    handlers.link(conn, m, ref=n, to=h)
    assert can_read(conn, a, n)


def test_e_the_ban_survives_a_rebuild(conn, make_account):
    a, m = make_account(), make_account()
    h = mk_hub(conn, a, "H")
    pool = mk_hub(conn, a, "pool")
    share(conn, a, h, m, "add")
    share(conn, a, pool, m, "add")
    n = mk_note(conn, m, "spam", hub=pool)
    handlers.link(conn, m, ref=n, to=h)
    handlers.link(conn, a, ref=n, to=h, remove=True)
    before = snapshot(conn)
    rebuild(conn)
    assert snapshot(conn) == before
    assert code_of(handlers.link, conn, m, ref=n, to=h) == "Forbidden"
    moderated = conn.execute("SELECT 1 FROM membership WHERE withdrawn AND moderated")
    assert len(moderated.fetchall()) == 1


# --- F: a member statement keeps its atom and hub ---------------------------------------


def test_f_a_later_version_cannot_repoint_a_membership(conn, make_account):
    a, b = make_account(), make_account()
    h1, h2 = mk_hub(conn, a, "H1"), mk_hub(conn, a, "H2")
    n1 = mk_note(conn, a, "one", hub=h1)
    n2 = mk_note(conn, a, "two")
    member = conn.execute(
        "SELECT m.address FROM membership m JOIN node n ON n.id = m.node_id"
        " WHERE n.address = %s",
        (n1,),
    ).fetchone()["address"]
    head = records.head(conn, member)
    assert head is not None
    for atom, hub in ((n2, h1), (n1, h2), (n2, h2)):
        rec = records.sign_and_append(
            conn,
            a,
            type="member",
            address=member,
            payload={"atom": atom, "hub": hub, "withdrawn": False},
            parents=[records.head(conn, member).version_hash],
            hub_id=fmt.hub_id_from_address(hub),
        )
        apply_record(conn, rec)
    row = conn.execute(
        "SELECT n.address AS atom, h.address AS hub FROM membership m"
        " JOIN node n ON n.id = m.node_id JOIN node h ON h.id = m.hub_node_id"
        " WHERE m.address = %s",
        (member,),
    ).fetchone()
    assert row == {"atom": n1, "hub": h1}
    _ = (b, accounts)
