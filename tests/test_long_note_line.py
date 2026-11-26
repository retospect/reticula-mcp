"""A note longer than search by meaning reads gets one warning line at the end
of the reply of put and edit; nothing is refused and nothing is cut."""

from __future__ import annotations

from reticula_mcp.graph import handlers
from reticula_mcp.search import index
from tests.graph_support import addr, mk_hub, mk_note, node


def reads() -> int:
    return index.model_reads_chars()


def test_a_long_note_gets_the_line_at_the_end_of_put(conn, make_account):
    z = make_account()
    text = "word " * 1300  # 6,500 characters
    out = handlers.put(conn, z, text=text)
    assert out.splitlines()[-1] == (
        f"this note is {len(text):,} characters; search by meaning reads only"
        f" about the first {reads():,}. Split it into notes of one idea each and"
        " link them (derived-from or related-to)."
    )
    assert node(conn, addr(out))["text"] == text  # stored whole


def test_the_title_line_counts_as_the_model_reads_it(conn, make_account):
    z = make_account()
    title = "Meeting summary"
    text = "x" * (reads() - len(title) - 1)  # title + newline + text == the limit
    assert "search by meaning" not in handlers.put(conn, z, text=text, title=title)
    out = handlers.put(conn, z, text=text + "y", title=title)
    assert f"is {reads() + 1:,} characters" in out


def test_a_short_note_gets_no_line(conn, make_account):
    z = make_account()
    assert "search by meaning" not in handlers.put(conn, z, text="x" * reads())


def test_the_line_comes_after_next_and_after_filing(conn, make_account):
    z = make_account()
    hub = mk_hub(conn, z, "Minutes")
    out = handlers.put(conn, z, text="y" * (reads() + 10), hub=hub)
    lines = out.splitlines()
    assert lines[0].startswith("saved note ")
    assert lines[1].startswith("next: ")
    assert lines[-1].startswith(f"this note is {reads() + 10:,} characters")


def test_edit_warns_about_the_new_text(conn, make_account):
    z = make_account()
    note = mk_note(conn, z, "short")
    out = handlers.edit(conn, z, ref=note, text="z" * (reads() + 1))
    assert out.splitlines()[-1].startswith(f"this note is {reads() + 1:,} characters")
    back = handlers.edit(conn, z, ref=note, text="short again")
    assert "search by meaning" not in back


def test_a_title_edit_of_a_long_note_warns_again(conn, make_account):
    z = make_account()
    note = mk_note(conn, z, "q" * (reads() + 50))
    out = handlers.edit(conn, z, ref=note, title="New title")
    assert "search by meaning reads only" in out


def test_an_unchanged_edit_gets_no_line(conn, make_account):
    z = make_account()
    long = "q" * (reads() + 50)
    note = mk_note(conn, z, long)
    out = handlers.edit(conn, z, ref=note, text=long)
    assert out.startswith("unchanged") and "search by meaning" not in out


def test_the_number_comes_from_the_index(conn, make_account, monkeypatch):
    monkeypatch.setattr(index, "model_reads_chars", lambda: 100)
    z = make_account()
    out = handlers.put(conn, z, text="a" * 150)
    assert "this note is 150 characters" in out and "first 100." in out


def test_a_long_hub_text_names_the_kind(conn, make_account):
    z = make_account()
    out = handlers.put(conn, z, kind="hub", title="H", text="h" * (reads() + 5))
    assert "this hub is " in out.splitlines()[-1]
