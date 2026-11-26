"""A note is embedded by the write itself when the model is loaded, so that it
is findable by meaning as soon as ``put`` or ``edit`` returns (debug note
baada5c5). What a write could not embed goes into ``embedding_backlog``
(migration 0302); the worker is the repair, and a search embeds a few of
those its viewer may read. Once the worker has caught up for the model in use,
a search no longer looks for notes without a vector among all notes."""

from __future__ import annotations

import re

import pytest

from reticula_mcp import accounts
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Database
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import find, hooks, index, worker
from tests.conftest import PASSWORD
from tests.helpers import make_atom
from tests.search_support import (
    SlowEmbedder,
    file_under,
    grant,
    hub_of,
    spy_on_sql,
    use_embedder,
)

_ADDRESS = re.compile(r"ret:p[a-z2-7]{26}/[0-9a-f-]{36}")
FULL_SCAN = "NOT EXISTS (SELECT 1 FROM embedding e WHERE e.node_id = n.id"


def address(out: str) -> str:
    found = _ADDRESS.search(out)
    assert found, out
    return found.group(0)


def node(conn, ref: str) -> dict:
    return conn.execute(
        "SELECT id, head_version FROM node WHERE address = %s", (ref,)
    ).fetchone()


def vector_version(conn, node_id: int, model: str) -> str | None:
    row = conn.execute(
        "SELECT version_hash FROM embedding WHERE node_id = %s AND model = %s",
        (node_id, model),
    ).fetchone()
    return row["version_hash"] if row else None


def pending(conn) -> set[int]:
    rows = conn.execute("SELECT node_id FROM embedding_backlog").fetchall()
    return {r["node_id"] for r in rows}


class BrokenEmbedder(SlowEmbedder):
    """Loaded, and every embed fails."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        raise RuntimeError("the model fell over")


class RefusedEmbedder(SlowEmbedder):
    """Loaded; its vectors are refused by the database (NaN), so the error
    comes from a statement inside the write's transaction."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[float("nan")] * self.dim for _ in texts]


def test_a_vector_the_database_refuses_does_not_poison_the_write(
    conn, make_account, monkeypatch
):
    refused = RefusedEmbedder(delay=0.0)
    refused.load()
    use_embedder(monkeypatch, refused)
    ada = make_account()
    note = address(graph.put(conn, ada, text="ibis field notes", title="Ibis"))
    assert refused.embed_calls == 1
    row = node(conn, note)  # the transaction goes on
    assert vector_version(conn, row["id"], refused.model) is None
    assert row["id"] in pending(conn)
    # and can still commit: a savepoint released without an error means the
    # transaction is not aborted (the session's connection is never committed)
    with conn.transaction():
        conn.execute("SELECT 1")
    assert conn.info.transaction_status.name == "INTRANS"


@pytest.fixture
def loaded(monkeypatch):
    model = SlowEmbedder(delay=0.0)
    model.load()
    use_embedder(monkeypatch, model)
    return model


def test_put_and_edit_store_the_vector_before_they_return(conn, make_account, loaded):
    ada = make_account()
    note = address(graph.put(conn, ada, text="tapir field notes", title="Tapir"))
    row = node(conn, note)
    assert vector_version(conn, row["id"], loaded.model) == row["head_version"]
    assert row["id"] not in pending(conn)
    graph.edit(conn, ada, ref=note, text="tapir field notes, revised")
    row2 = node(conn, note)
    assert row2["head_version"] != row["head_version"]
    assert vector_version(conn, row2["id"], loaded.model) == row2["head_version"]
    assert row2["id"] not in pending(conn)
    assert loaded.loads == 1  # loaded once by the fixture, never by a write


def test_a_failing_model_rolls_back_only_the_vector(conn, make_account, monkeypatch):
    broken = BrokenEmbedder(delay=0.0)
    broken.load()
    use_embedder(monkeypatch, broken)
    ada = make_account()
    note = address(graph.put(conn, ada, text="okapi field notes", title="Okapi"))
    row = node(conn, note)  # the note is written
    assert broken.embed_calls == 1
    assert vector_version(conn, row["id"], broken.model) is None
    assert row["id"] in pending(conn)  # and left for the worker
    graph.edit(conn, ada, ref=note, text="okapi field notes, revised")
    assert node(conn, note)["head_version"] != row["head_version"]
    assert row["id"] in pending(conn)


def test_a_model_still_loading_leaves_the_note_in_the_backlog(
    conn, make_account, monkeypatch
):
    slow = SlowEmbedder(delay=30.0)
    use_embedder(monkeypatch, slow)
    ada = make_account()
    note = address(graph.put(conn, ada, text="quokka notes", title="Quokka"))
    assert slow.loads == 0 and slow.embed_calls == 0  # no wait, no load
    assert node(conn, note)["id"] in pending(conn)


def test_every_way_of_writing_a_node_reaches_the_backlog(conn, make_account, loaded):
    ada = make_account()
    raw = make_atom(conn, ada, "a row written without the hook", title="Raw")
    assert raw["id"] in pending(conn)  # the insert trigger
    index.embed_nodes(conn, loaded, [raw["id"]])
    assert raw["id"] not in pending(conn)
    conn.execute(
        "UPDATE node SET head_version = 'v-new' || head_version WHERE id = %s",
        (raw["id"],),
    )
    assert raw["id"] in pending(conn)  # a new head
    index.embed_nodes(conn, loaded, [raw["id"]])
    # its vector went (0303: a vector of the model in use and caught up)
    index.mark_complete(conn, loaded)
    conn.execute("DELETE FROM embedding WHERE node_id = %s", (raw["id"],))
    assert raw["id"] in pending(conn)
    # a vector of another model going (--drop-other-models) changes nothing
    index.embed_nodes(conn, loaded, [raw["id"]])
    other = SlowEmbedder(delay=0.0, model="other")
    index.embed_nodes(conn, other, [raw["id"]])
    conn.execute(
        "DELETE FROM embedding WHERE node_id = %s AND model = %s",
        (raw["id"], other.model),
    )
    assert raw["id"] not in pending(conn)
    note = address(graph.put(conn, ada, text="to be deleted", title="Gone"))
    gone = node(conn, note)["id"]
    graph.delete(conn, ada, ref=note)
    assert gone not in pending(conn)  # a deleted note needs no vector


def test_a_search_reads_only_the_backlog_once_the_worker_caught_up(
    conn, make_account, monkeypatch, loaded
):
    ada, ben = make_account(), make_account()
    hub = hub_of(conn, ada, "Kiln")
    grant(conn, hub, ben, "read")
    mine = [
        make_atom(conn, ada, f"kiln firing {i}", title=f"Firing {i}") for i in range(3)
    ]
    for m in mine:
        file_under(conn, m, hub)
    private = make_atom(conn, ada, "kiln diary, private", title="Diary")
    index.mark_complete(conn, loaded)
    sql = spy_on_sql(monkeypatch)
    find.hybrid_search(conn, ben, "kiln firing")
    assert not any(FULL_SCAN in s for s in sql.statements)
    # ben's search embedded what ben may read, nothing else
    left = pending(conn)
    assert not {m["id"] for m in mine} & left
    assert private["id"] in left
    # bounded: a few per search, the rest is the worker's
    more = [make_atom(conn, ada, f"glaze {i}", title=f"Glaze {i}") for i in range(30)]
    for m in more:
        file_under(conn, m, hub)
    find.hybrid_search(conn, ben, "glaze")
    done = {m["id"] for m in more} - pending(conn)
    assert len(done) == index.CATCH_UP_COSTLY


def test_after_a_switch_a_search_looks_among_all_notes_again(
    conn, make_account, monkeypatch, loaded
):
    ada = make_account()
    make_atom(conn, ada, "kiln firing", title="Firing")
    other = SlowEmbedder(delay=0.0, model="other")
    other.load()
    index.mark_complete(conn, other)  # the worker caught up for another model
    assert not index.is_complete(conn, loaded)
    sql = spy_on_sql(monkeypatch)
    find.hybrid_search(conn, ada, "kiln")
    assert any(FULL_SCAN in s for s in sql.statements)


@pytest.fixture
def server(fresh_db_url, monkeypatch):
    monkeypatch.setattr(worker, "NUDGE_DELAYS", (0.05, 0.1, 0.2, 0.4, 0.8))
    db = Database(fresh_db_url)
    db.open()
    try:
        yield db
    finally:
        hooks.on_server_stop()
        db.close()


def test_the_worker_records_the_model_when_nothing_is_left(server):
    db = server
    slow = SlowEmbedder(delay=0.0)
    slow.load()
    with connect(db.url) as c:
        alice = accounts.create_account(c, "alice@example.com", PASSWORD, "alice")
        make_atom(c, alice, "newt note", title="Newt")
        c.commit()
    one = worker.Worker(db, slow)
    assert one._pass() > 0
    with connect(db.url) as c:
        assert not index.is_complete(c, slow)
    assert one._pass() == 0
    with connect(db.url) as c:
        assert index.is_complete(c, slow)
        assert pending(c) == set()


def test_what_the_model_reads_is_said_in_characters():
    """For the warning of put and edit about a note longer than that."""
    reads = index.model_reads_chars()
    assert reads == 2200
    assert reads <= index.TEXT_LIMIT
