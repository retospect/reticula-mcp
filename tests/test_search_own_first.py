"""In the exact-title leg of search, the viewer's own notes rank before other
people's: a title is often reused, and the note one wrote is the one meant.
Within each, the order stays as before (newest first)."""

from __future__ import annotations

import pytest

from reticula_mcp.search import find
from tests.graph_support import mk_hub, mk_note, node, share
from tests.search_support import use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def order(conn, viewer, query: str) -> list[int]:
    return [h.node_id for h in find.hybrid_search(conn, viewer, query).hits]


def test_own_title_match_ranks_before_a_newer_one_of_someone_else(conn, make_account):
    ada, ben = make_account(), make_account()
    hub = mk_hub(conn, ben, "Shared")
    share(conn, ben, hub, ada, "add")
    mine = mk_note(conn, ada, "my figures", title="Budget", hub=hub)
    theirs = mk_note(conn, ben, "ben's figures", title="Budget", hub=hub)
    newer_mine = mk_note(conn, ada, "my later figures", title="budget ", hub=hub)
    ids = order(conn, ada, "Budget")
    own = [node(conn, newer_mine)["id"], node(conn, mine)["id"]]
    assert ids[:3] == [*own, node(conn, theirs)["id"]]
    # for ben, his own comes first
    assert order(conn, ben, "Budget")[0] == node(conn, theirs)["id"]


def test_a_title_match_of_someone_else_still_ranks_before_an_own_tag_match(
    conn, make_account
):
    ada, ben = make_account(), make_account()
    hub = mk_hub(conn, ben, "Shared")
    share(conn, ben, hub, ada, "add")
    tagged = mk_note(conn, ada, "tagged", title="Other", tags=["budget"], hub=hub)
    titled = mk_note(conn, ben, "titled", title="Budget", hub=hub)
    ids = order(conn, ada, "budget")
    assert ids.index(node(conn, titled)["id"]) < ids.index(node(conn, tagged)["id"])
