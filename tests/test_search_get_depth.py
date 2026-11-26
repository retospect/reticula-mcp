"""`get(ref=<note>, depth=2)` adds the notes one link step further: the typed
links of the notes it links to and is linked from, both directions (debug
note 3c95310b: depth 2 printed the same as depth 1)."""

from __future__ import annotations

import pytest

from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import handlers, read
from tests.graph_support import mk_hub, mk_note, share
from tests.search_support import use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def ref(address: str) -> str:
    return address[-8:]


def steps(out: str) -> list[str]:
    """The lines under 'two link steps away:'."""
    lines = out.splitlines()
    if "two link steps away:" not in lines:
        return []
    at = lines.index("two link steps away:") + 1
    taken = []
    for ln in lines[at:]:
        if not ln.startswith("- "):
            break
        taken.append(ln)
    return taken


@pytest.fixture
def chain(conn, make_account):
    ada = make_account()
    a = mk_note(conn, ada, "Why the kiln cracks pots.", title="A cracks")
    b = mk_note(conn, ada, "Cooling too fast cracks glaze.", title="B cooling")
    c = mk_note(conn, ada, "Porcelain may cool fast.", title="C porcelain")
    graph.link(conn, ada, ref=a, to=b, rel="explains")
    graph.link(conn, ada, ref=b, to=c, rel="exception-to")
    return ada, a, b, c


def test_depth_two_shows_the_note_one_step_further(conn, chain):
    ada, a, b, c = chain
    one = handlers.get(conn, ada, ref=a)
    assert f"link explains -> {ref(b)} note" in one
    assert steps(one) == [] and ref(c) not in one
    two = handlers.get(conn, ada, ref=a, depth=2)
    assert steps(two) == [
        f'- {ref(b)} link exception-to -> {ref(c)} note "C porcelain"'
    ]
    # the same from the other end, the arrows turned
    back = handlers.get(conn, ada, ref=c, depth=2)
    assert f"link exception-to <- {ref(b)} note" in back
    assert steps(back) == [f'- {ref(b)} link explains <- {ref(a)} note "A cracks"']
    # the middle note has no second step: both ends are on its own link lines
    assert steps(handlers.get(conn, ada, ref=b, depth=2)) == []


def test_a_note_reached_twice_is_shown_once(conn, chain):
    ada, a, b, c = chain
    e = mk_note(conn, ada, "Slow cooling schedule.", title="E schedule")
    graph.link(conn, ada, ref=a, to=e, rel="see-also")
    graph.link(conn, ada, ref=e, to=c, rel="see-also")
    graph.link(conn, ada, ref=c, to=a, rel="see-also")  # back to the note itself
    got = steps(handlers.get(conn, ada, ref=a, depth=2))
    # c is on a link line of a now (c -> a), so it is no second step at all
    assert all(ref(c) not in ln.split(" link ")[1] for ln in got), got
    graph.link(conn, ada, ref=c, to=a, rel="see-also", remove=True)
    got = steps(handlers.get(conn, ada, ref=a, depth=2))
    assert sum(ref(c) in ln for ln in got) == 1, got
    assert all(ref(a) not in ln for ln in got), got


def test_a_note_the_reader_may_not_read_is_not_shown(conn, make_account):
    ada, ben = make_account(), make_account()
    hub = mk_hub(conn, ada, "Studio")
    share(conn, ada, hub, ben, "add")
    a = mk_note(conn, ada, "Why the kiln cracks pots.", title="A cracks", hub=hub)
    b = mk_note(conn, ada, "Cooling too fast cracks glaze.", title="B cooling", hub=hub)
    graph.link(conn, ada, ref=a, to=b, rel="explains")
    secret = mk_note(conn, ben, "My private doubt.", title="Doubt", private=True)
    graph.link(conn, ben, ref=secret, to=b, rel="disputes")
    loose = mk_note(conn, ada, "Not in the hub.", title="Loose")
    graph.link(conn, ada, ref=b, to=loose, rel="see-also")
    # ada may not read ben's private note, ben not ada's note outside the hub
    for viewer, hidden in ((ada, secret), (ben, loose)):
        got = handlers.get(conn, viewer, ref=a, depth=2)
        assert ref(hidden) not in got, got
    assert steps(handlers.get(conn, ada, ref=a, depth=2)) == [
        f'- {ref(b)} link see-also -> {ref(loose)} note "Loose"'
    ]
    assert steps(handlers.get(conn, ben, ref=a, depth=2)) == [
        f'- {ref(b)} link disputes <- {ref(secret)} note "Doubt"'
    ]


def test_the_second_step_is_capped_and_says_where_the_rest_is(conn, chain):
    ada, a, b, _c = chain
    for i in range(read.LINKS_SHOWN + 4):
        far = mk_note(conn, ada, f"Firing {i}.", title=f"Firing {i}")
        graph.link(conn, ada, ref=b, to=far, rel="see-also")
    got = handlers.get(conn, ada, ref=a, depth=2)
    assert len(steps(got)) == read.LINKS_SHOWN
    assert f"and 5 more: get(ref={ref(b)!r})" in got


def test_a_depth_beyond_two_says_so(conn, chain):
    ada, a, _b, c = chain
    got = handlers.get(conn, ada, ref=a, depth=5)
    assert ref(c) in got
    assert "depth 5 is more than one call reads: this is depth 2" in got


def test_the_count_beyond_the_cap_is_of_readable_notes_only(
    conn, make_account, monkeypatch
):
    """The cap on the link rows read, and the 'or more' it gives, count only
    notes the reader may read: unreadable links neither fill the cap nor
    make the reader believe there are more."""
    monkeypatch.setattr(read, "SECOND_STEP_READ", 5)
    monkeypatch.setattr(read, "LINKS_SHOWN", 1)
    ada, cleo = make_account(), make_account()
    hub = mk_hub(conn, ada, "Studio")
    share(conn, ada, hub, cleo, "add")
    a = mk_note(conn, ada, "Why the kiln cracks pots.", title="A cracks")
    b = mk_note(conn, ada, "Cooling too fast cracks glaze.", title="B cooling", hub=hub)
    graph.link(conn, ada, ref=a, to=b, rel="explains")
    for i in range(6):  # first by id, and not for ada to read
        secret = mk_note(
            conn, cleo, f"Private {i}.", title=f"Private {i}", private=True
        )
        graph.link(conn, cleo, ref=secret, to=b, rel="see-also")
    readable = [
        mk_note(conn, ada, f"Firing {i}.", title=f"Firing {i}") for i in range(3)
    ]
    for far in readable:
        graph.link(conn, ada, ref=b, to=far, rel="see-also")
    got = handlers.get(conn, ada, ref=a, depth=2)
    assert len(steps(got)) == 1
    assert f"and 2 more: get(ref={ref(b)!r})" in got
    assert "or more" not in got and "Private" not in got
