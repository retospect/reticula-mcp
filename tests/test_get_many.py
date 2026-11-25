"""``get`` with several refs in ``ref``: each answer as one ``get`` gives it,
one after the other; a ref that fails says so in its place."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.search.handlers import MAX_REFS, get, split_refs
from reticula_mcp.search.read import read_ref
from reticula_mcp.search.render import short_ref
from tests.graph_support import mk_hub, mk_note


@pytest.fixture
def ana(make_account: Callable[..., Account]) -> Account:
    return make_account(handle="ana")


def test_one_ref_answers_as_before(conn: Conn, ana: Account) -> None:
    note = mk_note(conn, ana, "kiln note", title="Kiln")
    out = get(conn, ana, ref=note)
    assert out == read_ref(conn, ana, note, history=False, depth=1)
    assert "--- 1 of" not in out


@pytest.mark.parametrize("sep", [" ", ",", ", ", "\n", " , "])
def test_several_refs_answer_one_after_the_other(
    conn: Conn, ana: Account, sep: str
) -> None:
    a = mk_note(conn, ana, "first note", title="First")
    b = mk_note(conn, ana, "second note", title="Second")
    hub = mk_hub(conn, ana, "Hub")
    refs = [short_ref(a), b, short_ref(hub)]
    out = get(conn, ana, ref=sep.join(refs))
    expected = "\n".join(
        f"--- {n} of 3: {ref} ---\n{get(conn, ana, ref=ref)}"
        for n, ref in enumerate(refs, 1)
    )
    assert out == expected


def test_a_ref_that_fails_says_so_and_the_others_answer(
    conn: Conn, ana: Account, make_account: Callable[..., Account]
) -> None:
    bea = make_account(handle="bea")
    mine = mk_note(conn, ana, "mine", title="Mine")
    theirs = mk_note(conn, bea, "private to bea", title="Theirs", private=True)
    out = get(conn, ana, ref=f"{mine} {theirs} ffffffffffff")
    blocks = out.split("\n--- ")
    assert '"Mine"' in blocks[0]
    assert blocks[1].startswith(f"2 of 3: {theirs} ---\n[error:NotFound]")
    assert "private to bea" not in out and "Theirs" not in out
    assert blocks[2].startswith("3 of 3: ffffffffffff ---\n[error:")


def test_history_and_depth_apply_to_each(conn: Conn, ana: Account) -> None:
    a = mk_note(conn, ana, "first", title="First")
    b = mk_note(conn, ana, "second", title="Second")
    out = get(conn, ana, ref=f"{a},{b}", history=True)
    for ref in (a, b):
        assert get(conn, ana, ref=ref, history=True) in out


def test_a_ref_twice_is_read_once(conn: Conn, ana: Account) -> None:
    a = mk_note(conn, ana, "first", title="First")
    b = mk_note(conn, ana, "second", title="Second")
    out = get(conn, ana, ref=f"{a} {b} {a}")
    assert out.count("--- ") == 2 and "--- 2 of 2:" in out


def test_more_than_ten_refs_or_part_with_several_is_refused(
    conn: Conn, ana: Account
) -> None:
    notes = [
        mk_note(conn, ana, f"note {i}", title=f"N{i}") for i in range(MAX_REFS + 1)
    ]
    with pytest.raises(ReticulaError) as err:
        get(conn, ana, ref=" ".join(notes))
    assert err.value.code == "BadInput" and "at most 10 refs" in err.value.message
    assert get(conn, ana, ref=" ".join(notes[:MAX_REFS])).count("--- ") == MAX_REFS
    with pytest.raises(ReticulaError) as err:
        get(conn, ana, ref=f"{notes[0]} {notes[1]}", part="A")
    assert err.value.code == "BadInput"


def test_split_refs() -> None:
    assert split_refs("a1b2c3, d4e5f6 ,\n0a1b2c3d4e5f") == [
        "a1b2c3",
        "d4e5f6",
        "0a1b2c3d4e5f",
    ]
    assert split_refs("ret:pabc/0190-aa+20") == ["ret:pabc/0190-aa+20"]
    assert split_refs("  ") == []
