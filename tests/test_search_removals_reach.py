"""A deletion or a take-out is told only to a reader who could read the hub
when it happened: who is given a share today learns nothing of what was taken
away before (search with since, and get of a deleted address). Every moment
here is that of its signed record, so the order of the calls is the order of
the events."""

from __future__ import annotations

import pytest

from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import handlers
from tests.graph_support import delete_now, mk_hub, mk_note, share
from tests.search_support import use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def short(address: str) -> str:
    return address[-8:]


def test_a_deletion_before_the_share_is_not_told(conn, make_account):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(conn, ada, "Board")
    before = mk_note(conn, ada, "deleted before ben came in", hub=hub)
    delete_now(conn, ada, before)
    share(conn, ada, hub, ben, "read")
    after = mk_note(conn, ada, "deleted after ben came in", hub=hub)
    delete_now(conn, ada, after)

    listing = handlers.search(conn, ben, query="", since="7d")
    assert f"- {short(after)} note deleted by @ada" in listing
    assert short(before) not in listing
    assert f"{short(after)} withdrawn" in handlers.get(conn, ben, ref=after)
    with pytest.raises(ReticulaError) as exc:
        handlers.get(conn, ben, ref=before)
    assert exc.value.code == "NotFound"
    # its author is told of both, as before
    mine = handlers.search(conn, ada, query="", since="7d")
    assert short(before) in mine and short(after) in mine
    assert "withdrawn" in handlers.get(conn, ada, ref=before)


def test_a_take_out_before_the_share_is_not_told(conn, make_account):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(conn, ada, "Board")
    before = mk_note(conn, ada, "taken out before ben came in", hub=hub)
    after = mk_note(conn, ada, "taken out after ben came in", hub=hub)
    graph.link(conn, ada, ref=before, to=hub, remove=True)
    share(conn, ada, hub, ben, "read")
    graph.link(conn, ada, ref=after, to=hub, remove=True)

    lines = handlers.search(conn, ben, query="", since="7d")
    assert f"{short(after)} note" in lines and "taken out of" in lines
    assert short(before) not in lines
