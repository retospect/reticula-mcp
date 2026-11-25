"""``get`` on a hub of more than 26 notes: lettered groups, and ``part``.

Every note the viewer may read is reached through exactly one letter path;
nothing the viewer may not read is reached, counted or lends a word to a label.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import groups
from reticula_mcp.search import read as read_module
from reticula_mcp.search.handlers import get
from reticula_mcp.search.render import short_ref
from tests.graph_support import mk_hub, mk_note, share, sql_node_id

SUBJECTS = {
    "kiln": ["kiln", "glaze", "firing", "stoneware", "cone"],
    "bird": ["kestrel", "falcon", "nest", "feather", "perch"],
    "code": ["python", "parser", "compiler", "bytecode", "lexer"],
    "boat": ["sail", "mast", "hull", "rudder", "keel"],
    "bean": ["coffee", "roast", "espresso", "grinder", "crema"],
}
GROUP_LINE = re.compile(r"^([A-Z]+)  (.+)  \((\d+)\)$")


def fill(conn: Conn, author: Account, hub: str, n: int, *, start: int = 0) -> list[str]:
    """``n`` notes about the five subjects, in turn."""
    names = sorted(SUBJECTS)
    out = []
    for i in range(start, start + n):
        words = SUBJECTS[names[i % 5]]
        text = f"{words[i % 5]} and {words[(i + 1) % 5]}, {words[(i + 2) % 5]} today"
        out.append(mk_note(conn, author, text, hub=hub, title=f"{words[0]} {i}"))
    return out


def group_lines(out: str) -> dict[str, tuple[str, int]]:
    found = {}
    for line in out.splitlines():
        m = GROUP_LINE.match(line)
        if m:
            found[m.group(1)] = (m.group(2), int(m.group(3)))
    return found


def listed(out: str) -> set[str]:
    """Short refs of the member lines of an answer: those after its
    ``members (newest first, ...)`` line."""
    lines = out.splitlines()
    at = next((i for i, ln in enumerate(lines) if ln.startswith("members (")), None)
    if at is None:
        return set()
    return {
        ln.split()[1] for ln in lines[at + 1 :] if re.match(r"^- [0-9a-f-]{8} ", ln)
    }


def walk(conn: Conn, viewer: Account, hub: str, part: str = "") -> list[str]:
    """Every short ref reached through the letters, one entry per path."""
    out = get(conn, viewer, ref=hub, part=part) if part else get(conn, viewer, ref=hub)
    letters = group_lines(out)
    if not letters:
        return sorted(listed(out))
    reached: list[str] = []
    for letter in letters:
        reached += walk(conn, viewer, hub, letter)
    return reached


@pytest.fixture
def ana(make_account: Callable[..., Account]) -> Account:
    return make_account(handle="ana")


def test_a_hub_of_26_notes_is_unchanged(
    conn: Conn, ana: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = mk_hub(conn, ana, "Small")
    fill(conn, ana, hub, 26)
    out = get(conn, ana, ref=hub)
    assert "groups by subject" not in out
    monkeypatch.setattr(read_module, "_grouped", lambda conn, viewer, top: None)
    assert get(conn, ana, ref=hub) == out


def test_more_than_26_notes_show_groups_and_every_note_has_one_path(
    conn: Conn, ana: Account
) -> None:
    hub = mk_hub(conn, ana, "Big")
    notes = fill(conn, ana, hub, 40)
    out = get(conn, ana, ref=hub)
    assert "40 notes in 5 groups by subject" in out
    lines = group_lines(out)
    assert sorted(lines) == ["A", "B", "C", "D", "E"]
    assert sorted(n for _, n in lines.values()) == [8] * 5
    assert f'next: get(ref={short_ref(hub)!r}, part="A") lists a group' in out
    newest = f'the newest notes first: search(query="", hub={short_ref(hub)!r})'
    assert newest in out
    assert newest not in get(conn, ana, ref=hub, part="A")
    assert "members (newest first" not in out
    reached = walk(conn, ana, hub)
    assert sorted(reached) == sorted(short_ref(n) for n in notes)
    # each group holds one subject, and its label is words of that subject
    for letter, (label, _) in lines.items():
        words = label.split(", ")
        assert len(words) == 3
        subject = {s for s, ws in SUBJECTS.items() if set(words) <= set(ws)}
        assert len(subject) == 1, (letter, label)


def test_the_same_notes_give_the_same_letters(conn: Conn, ana: Account) -> None:
    hub = mk_hub(conn, ana, "Big")
    fill(conn, ana, hub, 40)
    assert get(conn, ana, ref=hub) == get(conn, ana, ref=hub)
    assert get(conn, ana, ref=hub, part="C") == get(conn, ana, ref=hub, part="c")


def test_a_group_repeats_its_line_and_lists_its_notes(conn: Conn, ana: Account) -> None:
    hub = mk_hub(conn, ana, "Big")
    fill(conn, ana, hub, 40)
    label, size = group_lines(get(conn, ana, ref=hub))["B"]
    out = get(conn, ana, ref=hub, part="B")
    assert (
        f"group B of 5: {label} ({size} notes; keep a note's id, not its letters)"
        in out
    )
    assert f"members (newest first, {size}):" in out
    assert len(listed(out)) == size


def test_a_letter_that_does_not_exist_names_those_that_do(
    conn: Conn, ana: Account
) -> None:
    hub = mk_hub(conn, ana, "Big")
    fill(conn, ana, hub, 40)
    with pytest.raises(ReticulaError) as err:
        get(conn, ana, ref=hub, part="F")
    assert err.value.code == "BadInput"
    assert "no group F: this hub has the groups A to E" in err.value.message
    with pytest.raises(ReticulaError) as err:
        get(conn, ana, ref=hub, part="AB")
    assert "group A is not split" in err.value.message
    with pytest.raises(ReticulaError) as err:
        get(conn, ana, ref=hub, part="1")
    assert err.value.code == "BadInput"


def test_part_on_a_small_hub_or_a_note_is_refused(conn: Conn, ana: Account) -> None:
    hub = mk_hub(conn, ana, "Small")
    notes = fill(conn, ana, hub, 3)
    for ref in (hub, notes[0]):
        with pytest.raises(ReticulaError) as err:
            get(conn, ana, ref=ref, part="A")
        assert err.value.code == "BadInput"
    big = mk_hub(conn, ana, "Big")
    fill(conn, ana, big, 30)
    with pytest.raises(ReticulaError):
        get(conn, ana, ref=big, part="A", history=True)


def test_what_the_viewer_may_not_read_is_not_reached_counted_or_labelled(
    conn: Conn, ana: Account, make_account: Callable[..., Account]
) -> None:
    """bea's hub, shared with ana; in it a sub-hub of cid's that ana cannot
    read, a private note of bea's, and a deleted one. The words of those never
    reach a label and they are not counted."""
    bea, cid = make_account(handle="bea"), make_account(handle="cid")
    hub = mk_hub(conn, bea, "Shared")
    share(conn, bea, hub, ana, "read")
    share(conn, bea, hub, cid, "add")
    seen = fill(conn, bea, hub, 30)
    # filed under the shared hub, but private: only cid reads it
    hidden_sub = mk_hub(conn, cid, "Hidden", parent=hub)
    for i in range(12):
        mk_note(conn, cid, f"zyzzyva secret {i} quokka", hub=hidden_sub)
    graph.edit(conn, cid, ref=hidden_sub, private=True)
    for i in range(5):
        mk_note(conn, bea, f"zyzzyva private {i} quokka", hub=hub, private=True)
    out = get(conn, ana, ref=hub)
    assert "30 notes in 4 groups by subject" in out
    assert "zyzzyva" not in out and "quokka" not in out
    reached = walk(conn, ana, hub)
    assert sorted(reached) == sorted(short_ref(n) for n in seen)
    for letter in group_lines(out):
        part = get(conn, ana, ref=hub, part=letter)
        assert "zyzzyva" not in part and "quokka" not in part
    # cid reads the private sub-hub: other notes, other groups
    assert "42 notes in 6 groups by subject" in get(conn, cid, ref=hub)


def test_notes_in_a_readable_sub_hub_are_grouped_and_the_sub_hub_line_stays(
    conn: Conn, ana: Account
) -> None:
    hub = mk_hub(conn, ana, "Top")
    sub = mk_hub(conn, ana, "Sub", parent=hub)
    top_notes = fill(conn, ana, hub, 20)
    sub_notes = fill(conn, ana, sub, 15, start=20)
    out = get(conn, ana, ref=hub)
    assert "sub-hubs:" in out and '"Sub"' in out
    assert "35 notes in 5 groups by subject" in out
    reached = walk(conn, ana, hub)
    assert sorted(set(reached)) == sorted(short_ref(n) for n in top_notes + sub_notes)
    assert len(reached) == len(set(reached))


def test_stored_vectors_are_used_when_nearly_every_note_has_one(
    conn: Conn, ana: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a real model the vectors decide: here they say the opposite of
    the words, two subjects per vector, and the groups follow the vectors."""
    hub = mk_hub(conn, ana, "Vectors")
    notes = fill(conn, ana, hub, 40)
    monkeypatch.setattr(groups, "_model", lambda: "test-model")
    for i, address in enumerate(notes):
        vec = [0.0] * 8
        vec[i % 4] = 1.0  # four directions, not five subjects
        conn.execute(
            "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
            " SELECT n.id, 'test-model', 8, %s::vector, n.head_version"
            " FROM node n WHERE n.id = %s",
            (str(vec), sql_node_id(conn, address)),
        )
    out = get(conn, ana, ref=hub)
    assert "40 notes in 5 groups by subject" in out
    # 40 notes in four directions of ten, five groups of eight: four groups
    # of one direction each, and the two left of every direction in the fifth.
    # The words would have made five groups of one subject each.
    direction = {short_ref(a): i % 4 for i, a in enumerate(notes)}
    pure = 0
    for letter in group_lines(out):
        members = listed(get(conn, ana, ref=hub, part=letter))
        pure += len({direction[m] for m in members}) == 1
    assert pure == 4
