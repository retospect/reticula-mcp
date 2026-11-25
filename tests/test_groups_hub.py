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
    out = get_part(conn, viewer, hub, part) if part else get(conn, viewer, ref=hub)
    letters = group_lines(out)
    if not letters:
        return sorted(listed(out))
    reached: list[str] = []
    for letter in letters:
        reached += walk(conn, viewer, hub, letter)
    return reached


def get_part(conn: Conn, viewer: Account, hub: str, letters: str, **kw: object) -> str:
    """A lettered group of a hub: its id, a colon and the letters."""
    return get(conn, viewer, ref=f"{hub}:{letters}", **kw)


@pytest.fixture
def ana(make_account: Callable[..., Account]) -> Account:
    return make_account(handle="ana")


def test_a_hub_of_26_notes_is_unchanged(
    conn: Conn, ana: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = mk_hub(conn, ana, "Small")
    fill(conn, ana, hub, 26)
    out = get(conn, ana, ref=hub)
    assert " groups by " not in out
    monkeypatch.setattr(read_module, "_grouped", lambda conn, viewer, top: None)
    assert get(conn, ana, ref=hub) == out


def test_more_than_26_notes_show_groups_and_every_note_has_one_path(
    conn: Conn, ana: Account
) -> None:
    hub = mk_hub(conn, ana, "Big")
    notes = fill(conn, ana, hub, 40)
    out = get(conn, ana, ref=hub)
    assert "40 notes in 5 groups by their words," in out
    lines = group_lines(out)
    assert sorted(lines) == ["A", "B", "C", "D", "E"]
    assert sorted(n for _, n in lines.values()) == [8] * 5
    assert f'next: get(ref="{short_ref(hub)}:A") lists a group' in out
    newest = f'the newest notes first: search(query="", hub="{short_ref(hub)}")'
    assert newest in out
    assert newest not in get_part(conn, ana, hub, "A")
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
    assert get_part(conn, ana, hub, "C") == get_part(conn, ana, hub, "c")


def test_a_group_repeats_its_line_and_lists_its_notes(conn: Conn, ana: Account) -> None:
    hub = mk_hub(conn, ana, "Big")
    fill(conn, ana, hub, 40)
    label, size = group_lines(get(conn, ana, ref=hub))["B"]
    out = get_part(conn, ana, hub, "B")
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
        get_part(conn, ana, hub, "F")
    assert err.value.code == "BadInput"
    assert "no group F: this hub has the groups A to E" in err.value.message
    with pytest.raises(ReticulaError) as err:
        get_part(conn, ana, hub, "AB")
    assert "group A is not split" in err.value.message
    # what follows the colon is a group only when it is one to four letters;
    # anything else is a malformed ref
    with pytest.raises(ReticulaError) as err:
        get_part(conn, ana, hub, "1")
    assert err.value.code == "BadInput"


def test_the_letters_ride_on_the_ref_and_an_address_is_not_split() -> None:
    assert groups.split_letters("9ab6128c:AB") == ("9ab6128c", "AB")
    assert groups.split_letters(" 9ab6128c:c ") == ("9ab6128c", "c")
    address = "ret:pabcdefghijklmnopqrstuvwxyz/01a21305-2c41-7708-9880-0ce02035ca55"
    assert groups.split_letters(address) == (address, None)
    assert groups.split_letters(f"{address}:A") == (address, "A")
    assert groups.split_letters("9ab6128c:ABCDE") == ("9ab6128c:ABCDE", None)
    assert groups.split_letters("9ab6128c+20") == ("9ab6128c+20", None)


def test_several_hubs_and_groups_in_one_get(conn: Conn, ana: Account) -> None:
    hub = mk_hub(conn, ana, "Big")
    fill(conn, ana, hub, 40)
    out = get(conn, ana, ref=f"{short_ref(hub)}:A {short_ref(hub)}:B")
    assert "--- 1 of 2:" in out and "group A of 5:" in out and "group B of 5:" in out


def test_part_on_a_small_hub_or_a_note_is_refused(conn: Conn, ana: Account) -> None:
    hub = mk_hub(conn, ana, "Small")
    notes = fill(conn, ana, hub, 3)
    for ref in (hub, notes[0]):
        with pytest.raises(ReticulaError) as err:
            get_part(conn, ana, ref, "A")
        assert err.value.code == "BadInput"
    big = mk_hub(conn, ana, "Big")
    fill(conn, ana, big, 30)
    with pytest.raises(ReticulaError):
        get_part(conn, ana, big, "A", history=True)


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
    assert "30 notes in 4 groups by their words," in out
    assert "zyzzyva" not in out and "quokka" not in out
    reached = walk(conn, ana, hub)
    assert sorted(reached) == sorted(short_ref(n) for n in seen)
    for letter in group_lines(out):
        part = get_part(conn, ana, hub, letter)
        assert "zyzzyva" not in part and "quokka" not in part
    # cid reads the private sub-hub: other notes, other groups
    assert "42 notes in 6 groups by their words," in get(conn, cid, ref=hub)


def test_notes_in_a_readable_sub_hub_are_grouped_and_the_sub_hub_line_stays(
    conn: Conn, ana: Account
) -> None:
    hub = mk_hub(conn, ana, "Top")
    sub = mk_hub(conn, ana, "Sub", parent=hub)
    top_notes = fill(conn, ana, hub, 20)
    sub_notes = fill(conn, ana, sub, 15, start=20)
    out = get(conn, ana, ref=hub)
    assert "sub-hubs:" in out and '"Sub"' in out
    assert "35 notes in 5 groups by their words," in out
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
    assert "40 notes in 5 groups by meaning," in out
    # 40 notes in four directions of ten, five groups of eight: four groups
    # of one direction each, and the two left of every direction in the fifth.
    # The words would have made five groups of one subject each.
    direction = {short_ref(a): i % 4 for i, a in enumerate(notes)}
    pure = 0
    for letter in group_lines(out):
        members = listed(get_part(conn, ana, hub, letter))
        pure += len({direction[m] for m in members}) == 1
    assert pure == 4


def add_vector(conn: Conn, address: str, i: int) -> None:
    vec = [0.0] * 8
    vec[i % 4] = 1.0
    conn.execute(
        "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
        " SELECT n.id, 'test-model', 8, %s::vector, n.head_version"
        " FROM node n WHERE n.id = %s",
        (str(vec), sql_node_id(conn, address)),
    )


def test_two_listings_agree_and_every_letter_opens_its_group(
    conn: Conn, ana: Account
) -> None:
    """What RETICULA4 found on 01b2cff, as a test: list twice, then open each
    letter of the first listing; its line must be the listing's line."""
    hub = mk_hub(conn, ana, "Conference")
    fill(conn, ana, hub, 60)
    first, second = get(conn, ana, ref=hub), get(conn, ana, ref=hub)
    assert first == second
    lines = group_lines(first)
    assert len(lines) == 8
    for letter, (label, size) in lines.items():
        part = get_part(conn, ana, hub, letter)
        assert f"group {letter} of 8: {label} ({size} notes;" in part


def test_vectors_arriving_do_not_move_the_groups_until_all_are_there(
    conn: Conn, ana: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a real model, notes get their vectors one by one from the indexer.
    Until the last one is there the groups go by the words, the same at every
    call, and the first line says how many are still to come."""
    monkeypatch.setattr(groups, "_model", lambda: "test-model")
    hub = mk_hub(conn, ana, "Arriving")
    notes = fill(conn, ana, hub, 40)
    seen = []
    for done in (0, 1, 20, 39):
        conn.execute("DELETE FROM embedding WHERE model = 'test-model'")
        for i, address in enumerate(notes[:done]):
            add_vector(conn, address, i)
        out = get(conn, ana, ref=hub)
        assert f"groups by their words ({40 - done} not indexed yet;" in out
        seen.append(group_lines(out))
        for letter, (label, size) in group_lines(out).items():
            part = get_part(conn, ana, hub, letter)
            assert f"group {letter} of 5: {label} ({size} notes;" in part
    assert all(s == seen[0] for s in seen)
    add_vector(conn, notes[39], 39)
    out = get(conn, ana, ref=hub)
    assert "40 notes in 5 groups by meaning," in out
    assert get(conn, ana, ref=hub) == out
