"""`get` names the hub a note is filed in by its id, and offers to replace
somebody else's note in that hub. An assistant that had to correct a note in
another person's hub guessed the hub's reference before (docs/trials.md,
round thirteen)."""

from __future__ import annotations

import pytest

from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import handlers
from tests.graph_support import mk_hub, mk_note, share
from tests.search_support import use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def ref(address: str) -> str:
    return address[-8:]


def test_the_path_of_a_note_ends_with_the_id_of_its_hub(conn, make_account):
    ben = make_account()
    top = mk_hub(conn, ben, "Lab")
    sub = mk_hub(conn, ben, "Lab logistics", parent=top)
    note = mk_note(conn, ben, "The freezer is defrosted on Fridays.", hub=sub)
    got = handlers.get(conn, ben, ref=note)
    assert f'in: "Lab" / "Lab logistics" id {ref(sub)}' in got
    # the path of a hub itself names no id: the hub is the one read
    assert f"id {ref(top)}" not in handlers.get(conn, ben, ref=sub)


def test_somebody_elses_note_in_a_hub_is_offered_a_replacement_there(
    conn, make_account
):
    ben, ada = make_account(), make_account()
    hub = mk_hub(conn, ben, "Lab logistics")
    share(conn, ben, hub, ada, "add")
    note = mk_note(conn, ben, "The freezer is defrosted on Mondays.", hub=hub)
    got = handlers.get(conn, ada, ref=note)
    hint = (
        f"next: put(text=<the correction>, supersedes={ref(note)!r},"
        f" hub={ref(hub)!r}) replaces it in the same hub"
    )
    assert hint in got
    lines = got.splitlines()
    link = next(i for i, ln in enumerate(lines) if ln.startswith("next: link("))
    assert lines.index(hint) == link - 1
    # its author edits it instead
    assert "supersedes=" not in handlers.get(conn, ben, ref=note)


def test_no_replacement_is_offered_without_a_hub_the_reader_may_read(
    conn, make_account
):
    """A note in no hub is read by its author only, who is told to edit it.
    Of two hubs the reader names only the one they may read."""
    ben, ada = make_account(), make_account()
    loose = mk_note(conn, ben, "A note in no hub.")
    shared = mk_hub(conn, ben, "Shared")
    closed = mk_hub(conn, ben, "Closed")
    share(conn, ben, shared, ada, "read")
    both = mk_note(conn, ben, "A note in two hubs.", hub=closed)
    graph.link(conn, ben, ref=both, to=shared, rel="part-of")
    assert "supersedes=" not in handlers.get(conn, ben, ref=loose)
    got = handlers.get(conn, ada, ref=both)
    assert f"hub={ref(shared)!r}" in got
    assert ref(closed) not in got
