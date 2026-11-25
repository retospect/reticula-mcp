"""A relation name the account has not used before gets one line in the reply
of link: the names in use, and a near one if the new name looks like it."""

from __future__ import annotations

import pytest

from reticula_mcp.graph import api, handlers
from tests.graph_support import mk_note


def notes(conn, who, n):
    return [mk_note(conn, who, f"note {i}", title=f"N{i}") for i in range(n)]


def relate(conn, who, a, b, rel):
    return handlers.link(conn, who, ref=a, to=b, rel=rel)


def extra_line(out: str) -> str | None:
    lines = out.splitlines()
    return lines[1] if len(lines) > 1 else None


def test_the_names_in_use_are_counted_most_used_first(conn, make_account):
    z = make_account()
    a, b, c, d = notes(conn, z, 4)
    for dst in (b, c, d):
        relate(conn, z, a, dst, "mentions")
    relate(conn, z, b, c, "rebuts")
    relate(conn, z, b, d, "rebuts")
    relate(conn, z, c, d, "explains")
    out = relate(conn, z, d, a, "echoes")
    assert extra_line(out) == (
        'new relation name "echoes"; in use: mentions (3), rebuts (2), explains (1).'
    )


def test_a_near_name_is_suggested(conn, make_account):
    z = make_account()
    a, b, c = notes(conn, z, 3)
    for dst in (b, c):
        relate(conn, z, a, dst, "related-to")
    relate(conn, z, b, c, "explains")
    out = relate(conn, z, c, a, "relates-to")
    assert extra_line(out) == (
        'new relation name "relates-to"; in use: related-to (2), explains (1).'
        " Did you mean related-to?"
    )
    # the link is still made
    assert out.startswith("added relates-to ")
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM link WHERE rel = 'relates-to'"
        ).fetchone()["n"]
        == 1
    )


@pytest.mark.parametrize(
    ("used", "new"),
    [
        ("depends-on", "Depends_On"),
        ("depends-on", "dependson"),
        ("contradicts", "contradict"),
        ("mentions", "mention"),
        ("followed-by", "folowed-by"),
        ("precedes", "preceeds"),
    ],
)
def test_variants_are_recognised(conn, make_account, used, new):
    z = make_account()
    a, b, c = notes(conn, z, 3)
    relate(conn, z, a, b, used)
    out = relate(conn, z, b, c, new)
    assert out.endswith(f"Did you mean {used}?"), out


def test_a_recommended_name_is_suggested_before_a_name_in_use(conn, make_account):
    z = make_account()
    a, b, c = notes(conn, z, 3)
    relate(conn, z, a, b, "relates-to")  # a wrong name, already in use
    out = relate(conn, z, b, c, "relate-to")  # one edit from both
    assert out.endswith("Did you mean related-to?"), out


def test_a_different_name_gets_no_suggestion(conn, make_account):
    z = make_account()
    a, b, c = notes(conn, z, 3)
    relate(conn, z, a, b, "mentions")
    out = relate(conn, z, b, c, "contradicts")
    assert "Did you mean" not in out
    assert 'new relation name "contradicts"' in out


def test_short_names_only_match_as_variants(conn, make_account):
    assert api.close_relation_name("is", ["of", "at"]) is None
    assert api.close_relation_name("IS", ["is"]) == "is"


def test_a_name_used_before_gets_no_line(conn, make_account):
    z = make_account()
    a, b, c = notes(conn, z, 3)
    relate(conn, z, a, b, "mentions")
    assert extra_line(relate(conn, z, b, c, "mentions")) is None


def test_a_recommended_name_gets_no_line_on_first_use(conn, make_account):
    z = make_account()
    a, b = notes(conn, z, 2)
    for rel in api.KNOWN_RELS:
        assert extra_line(relate(conn, z, a, b, rel)) is None, rel


def test_the_first_name_ever_says_none_are_in_use(conn, make_account):
    z = make_account()
    a, b = notes(conn, z, 2)
    assert extra_line(relate(conn, z, a, b, "mentions")) == (
        'new relation name "mentions"; no other relation names in use yet.'
    )


def test_a_near_recommended_name_is_suggested_even_unused(conn, make_account):
    z = make_account()
    a, b = notes(conn, z, 2)
    out = relate(conn, z, a, b, "relates-to")
    assert out.endswith(
        'new relation name "relates-to"; no other relation names in use yet.'
        " Did you mean related-to?"
    )


def test_built_ins_are_not_counted_nor_announced(conn, make_account):
    z = make_account()
    a, b, c = notes(conn, z, 3)
    assert extra_line(relate(conn, z, b, a, "supersedes")) is None
    relate(conn, z, a, b, "mentions")
    assert [n for n, _ in api.relation_names_in_use(conn, z.person_id)] == ["mentions"]


def test_at_most_eight_are_listed(conn, make_account):
    z = make_account()
    a, *rest = notes(conn, z, 11)
    for i, dst in enumerate(rest):
        relate(conn, z, a, dst, f"kind{i:02d}x")
    out = relate(conn, z, rest[0], rest[1], "brandnew")
    line = extra_line(out) or ""
    assert line.count(" (1)") == 8 and line.endswith(", and 2 more.")


def test_other_accounts_names_are_not_shown(conn, make_account):
    z, y = make_account(), make_account()
    a, b = notes(conn, y, 2)
    relate(conn, y, a, b, "secretrel")
    c, d = notes(conn, z, 2)
    out = relate(conn, z, c, d, "mentions")
    assert "secretrel" not in out


def test_removing_or_repeating_a_link_adds_no_line(conn, make_account):
    z = make_account()
    a, b = notes(conn, z, 2)
    relate(conn, z, a, b, "mentions")
    again = relate(conn, z, a, b, "mentions")
    assert again.startswith("no change:") and extra_line(again) is None
    gone = handlers.link(conn, z, ref=a, to=b, rel="mentions", remove=True)
    assert extra_line(gone) is None
    fresh = handlers.link(conn, z, ref=a, to=b, rel="neverused", remove=True)
    assert extra_line(fresh) is None


def test_a_deleted_notes_links_are_not_counted(conn, make_account):
    z = make_account()
    a, b, c = notes(conn, z, 3)
    relate(conn, z, a, b, "mentions")
    handlers.delete(conn, z, ref=a)
    assert api.relation_names_in_use(conn, z.person_id) == []
    assert "no other relation names" in relate(conn, z, b, c, "echoes")
