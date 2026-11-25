"""Word handles: the words of an address, the switch per connection, and how
many words a reader is shown (more only when a note that reader may read
shares the shorter form)."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from reticula_mcp import connection, handle_words
from reticula_mcp.accounts import Account
from reticula_mcp.content.handlers import profile
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.search import handles
from reticula_mcp.search.handlers import get, search
from reticula_mcp.search.render import short_ref
from tests.graph_support import mk_note

ADDRESS = "ret:pabcdefghijklmnopqrstuvwxyz/01a21305-2c41-7708-9880-0ce02035ca55"


# ------------------------------------------------------------------ the words


def test_the_number_is_the_last_28_bits_of_the_address() -> None:
    assert handle_words.number(ADDRESS) == 0x2035CA55 & (2**28 - 1)


@pytest.mark.parametrize("count", [2, 3, 4])
def test_words_and_back(count: int) -> None:
    for n in (0, 1, 2**14 + 5, 2**21 + 3, 2**28 - 1, handle_words.number(ADDRESS)):
        spoken = handle_words.to_words(n, count)
        assert len(spoken.split()) == count
        assert re.fullmatch(r"[a-z]+( [a-z]+)*", spoken)
        prefix, words = handle_words.to_prefix(spoken.upper().replace(" ", "-")) or (
            -1,
            0,
        )
        assert words == count
        low, high = handle_words.bounds(prefix, count)
        assert low <= n < high and high - low == 2 ** (28 - 7 * count)


def test_two_words_are_a_prefix_of_three_and_four() -> None:
    n = handle_words.number(ADDRESS)
    two, three, four = (handle_words.to_words(n, c) for c in (2, 3, 4))
    assert three.endswith(two) and four.endswith(three.split(" ", 1)[1])


def test_what_is_not_a_handle() -> None:
    for text in (
        "gentle",
        "4f9c21aa",
        "gentle beacon beacon beacon beacon",
        "kiln glaze",
    ):
        assert handle_words.to_prefix(text) is None


# ------------------------------------------------------------- in the replies


@pytest.fixture
def ana(make_account: Callable[..., Account]) -> Account:
    return make_account(handle="ana")


def on(conn: Conn, viewer: Account, key: str = "grant:g1") -> None:
    with connection.during({"connection": key}):
        assert "on for this connection" in profile(conn, viewer, word_handles="on")


def spoken(conn: Conn, viewer: Account, call: Callable[[], str], key: str = "grant:g1"):
    with connection.during({"connection": key}), handles.during(conn, viewer):
        return call()


def words_after(out: str, ref: str) -> str:
    """The words that follow ``ref`` at the start of a line."""
    found = re.search(rf"^(?:- )?{ref} ((?:[a-z]+ )+?)(?:note|hub)\b", out, re.M)
    return found.group(1).strip() if found else ""


def set_number(conn: Conn, address: str, n: int) -> str:
    """Give a note another address tail, so that two notes clash."""
    new = address[:-7] + f"{n:07x}"
    conn.execute("UPDATE node SET address = %s WHERE address = %s", (new, address))
    return new


def test_off_by_default_and_unchanged(conn: Conn, ana: Account) -> None:
    note = mk_note(conn, ana, "kiln firing notes", title="Kiln")
    plain = get(conn, ana, ref=note)
    assert spoken(conn, ana, lambda: get(conn, ana, ref=note)) == plain
    assert words_after(plain, short_ref(note)) == ""


def test_switched_on_the_id_gains_two_words_in_get_and_search(
    conn: Conn, ana: Account
) -> None:
    note = mk_note(conn, ana, "kiln firing notes", title="Kiln")
    on(conn, ana)
    two = handle_words.to_words(handle_words.number(note), 2)
    out = spoken(conn, ana, lambda: get(conn, ana, ref=note))
    assert out.splitlines()[0].startswith(f"{short_ref(note)} {two} note ")
    found = spoken(conn, ana, lambda: search(conn, ana, query="kiln"))
    assert words_after(found, short_ref(note)) == two
    # another connection of the same account has them off
    assert (
        words_after(
            spoken(conn, ana, lambda: get(conn, ana, ref=note), key="token:9"),
            short_ref(note),
        )
        == ""
    )
    with connection.during({"connection": "grant:g1"}):
        assert "off" in profile(conn, ana, word_handles="off")
    assert spoken(conn, ana, lambda: get(conn, ana, ref=note)) == get(
        conn, ana, ref=note
    )


def test_a_clash_among_readable_notes_adds_words(conn: Conn, ana: Account) -> None:
    a = mk_note(conn, ana, "first", title="First")
    b = mk_note(conn, ana, "second", title="Second")
    c = mk_note(conn, ana, "third", title="Third")
    base = handle_words.number(a)
    b = set_number(conn, b, base ^ 1)  # same top 21 bits: four words
    c = set_number(conn, c, base ^ (1 << 10))  # same top 14 bits: three words
    on(conn, ana)

    def words_of(ref: str) -> str:
        return words_after(
            spoken(conn, ana, lambda: get(conn, ana, ref=ref)), short_ref(ref)
        )

    words = {x: words_of(x) for x in (a, b, c)}
    assert len(words[a].split()) == 4 and len(words[b].split()) == 4
    assert len(words[c].split()) == 3
    assert len(set(words.values())) == 3


def test_a_note_the_reader_may_not_read_does_not_lengthen_the_words(
    conn: Conn, ana: Account, make_account: Callable[..., Account]
) -> None:
    bea = make_account(handle="bea")
    mine = mk_note(conn, ana, "mine", title="Mine")
    theirs = mk_note(conn, bea, "private to bea", title="Theirs", private=True)
    set_number(conn, theirs, handle_words.number(mine) ^ 1)
    on(conn, ana)
    out = spoken(conn, ana, lambda: get(conn, ana, ref=mine))
    assert len(words_after(out, short_ref(mine)).split()) == 2


def test_the_switch_needs_a_connection_and_on_or_off(conn: Conn, ana: Account) -> None:
    with pytest.raises(ReticulaError) as err:
        profile(conn, ana, word_handles="on")
    assert "per connection" in err.value.message
    with connection.during({"connection": "grant:g1"}), pytest.raises(ReticulaError):
        profile(conn, ana, word_handles="maybe")


# ------------------------------------------------------------- words as refs


def test_words_open_the_note_whether_or_not_they_are_shown(
    conn: Conn, ana: Account
) -> None:
    note = mk_note(conn, ana, "kiln firing notes", title="Kiln")
    n = handle_words.number(note)
    for count in (2, 3, 4):
        said = handle_words.to_words(n, count)
        assert get(conn, ana, ref=said) == get(conn, ana, ref=note)
        assert get(conn, ana, ref=said.upper().replace(" ", "-")) == get(
            conn, ana, ref=note
        )


def test_two_words_that_name_several_notes_list_them_with_more_words(
    conn: Conn, ana: Account
) -> None:
    a = mk_note(conn, ana, "first", title="First")
    b = set_number(conn, mk_note(conn, ana, "second", title="Second"), 0)
    base = handle_words.number(a)
    b = set_number(conn, b, base ^ (1 << 10))  # same two words, other three
    two = handle_words.to_words(base, 2)
    with pytest.raises(ReticulaError) as err:
        get(conn, ana, ref=two)
    assert err.value.code == "Conflict"
    for x in (a, b):
        three = handle_words.to_words(handle_words.number(x), 3)
        assert three in err.value.message and short_ref(x) in err.value.message
        assert get(conn, ana, ref=three) == get(conn, ana, ref=x)


def test_words_never_open_a_note_the_reader_may_not_read(
    conn: Conn, ana: Account, make_account: Callable[..., Account]
) -> None:
    bea = make_account(handle="bea")
    theirs = mk_note(conn, bea, "private to bea", title="Theirs", private=True)
    said = handle_words.to_words(handle_words.number(theirs), 3)
    with pytest.raises(ReticulaError) as err:
        get(conn, ana, ref=said)
    assert err.value.code == "NotFound" and "Theirs" not in err.value.message


def test_several_handles_in_one_get_go_between_commas(conn: Conn, ana: Account) -> None:
    a = mk_note(conn, ana, "first", title="First")
    b = mk_note(conn, ana, "second", title="Second")
    wa, wb = (handle_words.to_words(handle_words.number(x), 3) for x in (a, b))
    out = get(conn, ana, ref=f"{wa}, {wb}, {short_ref(a)} {short_ref(b)}")
    assert f"--- 1 of 4: {wa} ---" in out and f"--- 2 of 4: {wb} ---" in out
    assert f"--- 3 of 4: {short_ref(a)} ---" in out
    assert '"First"' in out and '"Second"' in out
