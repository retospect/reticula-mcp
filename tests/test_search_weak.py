"""When the search by meaning has nothing at the model's cut, the answer adds
the notes nearest in meaning, under a line of their own that says they may be
unrelated (find.WEAK_SHOWN, handlers.WEAK; debug note baada5c5): the cut keeps
nonsense out and also real paraphrases a little below it. They are shown
whenever the vector leg is empty, also beside a keyword match on something
else, and never with the hash embedder, which compares words."""

from __future__ import annotations

import pytest

from reticula_mcp.search import find, handlers
from tests.graph_support import mk_hub, mk_note, share
from tests.search_support import (
    SlowEmbedder,
    spy_on_sql,
    use_embedder,
    use_hash_embedder,
)


def ref(address: str) -> str:
    return address[-8:]


@pytest.fixture
def model(monkeypatch):
    """A loaded model that is not cheap; its vectors are the hash embedder's,
    so a query with no word of a note is far from it."""
    m = SlowEmbedder(delay=0.0)
    m.load()
    # a cut no vector reaches: the vector leg is empty unless a test lowers it
    m.max_distance = 0.0001
    use_embedder(monkeypatch, m)
    return m


def parts(out: str) -> tuple[list[str], list[str]]:
    rows: list[str] = []
    weak: list[str] = []
    into = rows
    for ln in out.splitlines():
        if ln == handlers.WEAK:
            into = weak
        elif ln.startswith("- "):
            into.append(ln)
    return rows, weak


def test_nothing_at_the_cut_shows_the_nearest_under_their_own_line(
    conn, make_account, model
):
    ada = make_account(handle="ada")
    for i in range(5):
        mk_note(conn, ada, f"kiln firing log {i}", title=f"Firing {i}")
    out = handlers.search(conn, ada, query="glowing coastal tower")
    rows, weak = parts(out)
    assert out.startswith('search "glowing coastal tower": no results.')
    assert rows == [] and len(weak) == find.WEAK_SHOWN
    assert out.index(handlers.WEAK) < out.index(weak[0])
    assert "get(ref) reads one of those above" in out


def test_a_match_by_meaning_shows_no_weak_rows(conn, make_account, model):
    model.max_distance = 2.0  # every vector is a match
    ada = make_account(handle="ada")
    for i in range(8):  # more than the limit: some matches are not shown
        mk_note(conn, ada, f"kiln firing log {i}", title=f"Firing {i}")
    out = handlers.search(conn, ada, query="kiln firing", limit=3)
    assert handlers.WEAK not in out
    assert find.hybrid_search(conn, ada, "kiln firing", limit=3).weak == []


def test_a_keyword_hit_does_not_hide_them(conn, make_account, model, monkeypatch):
    """The vector leg is empty (a cut nothing reaches), a keyword matches one
    note: that note is a result, and the weak rows are the others."""
    ada = make_account(handle="ada")
    hit = mk_note(conn, ada, "the gateway reboots", title="Gateway")
    for i in range(4):
        mk_note(conn, ada, f"kiln firing log {i}", title=f"Firing {i}")
    out = handlers.search(conn, ada, query="gateway")
    rows, weak = parts(out)
    assert [r[2:10] for r in rows] == [ref(hit)]
    assert 0 < len(weak) <= find.WEAK_SHOWN
    assert all(ref(hit) not in w for w in weak)  # never shown twice


def test_the_hash_embedder_shows_none(conn, make_account, monkeypatch):
    use_hash_embedder(monkeypatch)
    ada = make_account(handle="ada")
    mk_note(conn, ada, "kiln firing log", title="Firing")
    out = handlers.search(conn, ada, query="glowing coastal tower")
    assert handlers.WEAK not in out and ": no results." in out


def test_only_readable_notes_and_others_without_their_text(conn, make_account, model):
    ada, ben, cleo = (make_account(handle=h) for h in ("ada", "ben", "cleo"))
    hub = mk_hub(conn, ben, "Shared")
    share(conn, ben, hub, ada, "read")
    mine = mk_note(conn, ada, "my kiln note", title="Mine")
    his = mk_note(conn, ben, "his kiln note with words", title="His", hub=hub)
    hidden = mk_note(conn, cleo, "cleo's private kiln note", title="Hidden")
    out = handlers.search(conn, ada, query="glowing coastal tower")
    _rows, weak = parts(out)
    shown = [w[2:10] for w in weak]
    assert ref(hidden) not in out
    assert shown[0] == ref(mine)  # her own first
    assert set(shown[1:]) == {ref(his), ref(hub)}  # what she may read of ben's
    assert "my kiln note" in out and "his kiln note with words" not in out


def test_they_cost_no_statement(conn, make_account, model, monkeypatch):
    """The nearest notes come from the statement of the vector leg itself:
    the same search with and without weak rows runs the same statements."""
    ada = make_account(handle="ada")
    for i in range(4):
        mk_note(conn, ada, f"kiln firing log {i}", title=f"Firing {i}")
    find.hybrid_search(conn, ada, "glowing coastal tower")  # vectors, plans warm
    sql = spy_on_sql(monkeypatch)
    monkeypatch.setattr(find, "WEAK_SHOWN", 0)
    assert find.hybrid_search(conn, ada, "glowing coastal tower").weak == []
    without = list(sql.statements)
    sql.reset()
    monkeypatch.setattr(find, "WEAK_SHOWN", 3)
    assert find.hybrid_search(conn, ada, "glowing coastal tower").weak
    assert len(sql.statements) == len(without)
