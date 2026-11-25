"""A slow or missing embedding model never gets in the way.

The first search after a start answers from keywords while the model loads in
the background; a write neither loads nor waits for the model; the nodes that
were written meanwhile are embedded by the server's background worker with
nobody doing anything. A stand-in model (``SlowEmbedder``) makes these run
everywhere; the tests at the end repeat the essentials with the real model
when the ``fastembed`` extra is installed.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from reticula_mcp import accounts
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Database
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import embedder as embedder_module
from reticula_mcp.search import handlers, hooks, index, worker
from reticula_mcp.search.embedder import HashEmbedder
from tests.conftest import PASSWORD
from tests.helpers import make_atom
from tests.search_support import (
    SlowEmbedder,
    use_embedder,
    use_hash_embedder,
    use_real_model,
    wait_until,
)

LOADING = handlers.KEYWORDS_ONLY["loading"]
UNAVAILABLE = handlers.KEYWORDS_ONLY["off"]
_ADDRESS = re.compile(r"ret:\S+")


def address(out: str) -> str:
    found = _ADDRESS.search(out)
    assert found, out
    return found.group(0)


def vectors(conn, model: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM embedding WHERE model = %s", (model,)
    ).fetchone()["n"]


# ------------------------------------------------------------------- the search


def test_search_answers_from_keywords_while_the_model_loads(
    conn, make_account, monkeypatch
):
    slow = SlowEmbedder(delay=1.0)
    use_embedder(monkeypatch, slow)
    alice = make_account(handle="alice")
    note = make_atom(conn, alice, "The aardvark burrow survey is due Friday.")
    make_atom(conn, alice, "Grocery list: bread, milk, eggs.")

    started = time.monotonic()
    out = handlers.search(conn, alice, query="aardvark")
    took = time.monotonic() - started

    assert note["address"][-8:] in out  # the keyword legs found it
    assert LOADING in out  # and the caller is told what kind of answer this is
    assert took < 0.5, f"the search waited for the model ({took:.2f}s)"
    assert slow.embed_calls == 0 and vectors(conn, slow.model) == 0
    # a miss says so too: the caller should not conclude that nothing exists
    assert LOADING in handlers.search(conn, alice, query="zeppelin")

    assert wait_until(lambda: slow.ready, 5)  # the search started the load
    assert slow.loads == 1
    again = handlers.search(conn, alice, query="aardvark")
    assert note["address"][-8:] in again
    assert "keyword matches only" not in again
    assert vectors(conn, slow.model) == 2  # caught up by the search itself


def test_a_model_that_failed_to_load_is_reported_and_retried(
    conn, make_account, monkeypatch
):
    slow = SlowEmbedder(delay=0.0, failures=1)
    use_embedder(monkeypatch, slow)
    alice = make_account(handle="alice")
    note = make_atom(conn, alice, "Resilient marmot colony notes.", title="Marmot")

    assert LOADING in handlers.search(conn, alice, query="marmot")
    assert wait_until(lambda: slow.load_error is not None, 5)
    out = handlers.search(conn, alice, query="marmot")  # asks for another load
    assert note["address"][-8:] in out and UNAVAILABLE in out
    assert wait_until(lambda: slow.ready, 5)
    assert "keyword matches only" not in handlers.search(conn, alice, query="marmot")


def test_search_with_no_embedder_at_all_still_answers(conn, make_account, monkeypatch):
    def broken(kind, model_name):
        raise RuntimeError("the embedder cannot be built")

    monkeypatch.setattr(embedder_module, "_build", broken)
    alice = make_account(handle="alice")
    note = make_atom(conn, alice, "Unflappable wombat ledger.", title="Wombat")
    out = handlers.search(conn, alice, query="wombat")
    assert note["address"][-8:] in out and UNAVAILABLE in out


def test_a_search_with_a_costly_model_catches_up_only_a_little(
    conn, make_account, monkeypatch
):
    slow = SlowEmbedder(delay=0.0, embed_delay=0.05)
    slow.load()
    use_embedder(monkeypatch, slow)
    alice = make_account(handle="alice")
    for i in range(40):
        make_atom(conn, alice, f"heron sighting number {i}", title=f"heron {i}")

    handlers.search(conn, alice, query="heron")
    first = vectors(conn, slow.model)
    assert 0 < first <= index.CATCH_UP_COSTLY  # bounded by count

    monkeypatch.setattr(index, "CATCH_UP_SECONDS", 0.01)
    handlers.search(conn, alice, query="heron")
    # bounded by time: one batch, then the deadline stops it
    assert vectors(conn, slow.model) == first + index.BATCH_COSTLY


# -------------------------------------------------------------------- the write


def test_a_write_neither_loads_nor_waits_for_the_model(conn, make_account, monkeypatch):
    slow = SlowEmbedder(delay=30.0)
    use_embedder(monkeypatch, slow)
    alice = make_account(handle="alice")

    started = time.monotonic()
    note = address(graph.put(conn, alice, text="tapir field notes", title="Tapir"))
    graph.edit(conn, alice, ref=note, text="tapir field notes, revised")
    took = time.monotonic() - started

    assert took < 2.0, f"the write waited for the model ({took:.2f}s)"
    assert slow.loads == 0 and slow.embed_calls == 0  # not even asked to load
    assert vectors(conn, slow.model) == 0
    assert "revised" in handlers.get(conn, alice, ref=note)  # the write happened


def test_a_write_survives_an_embedder_that_cannot_be_built(
    conn, make_account, monkeypatch
):
    def broken(kind, model_name):
        raise RuntimeError("the embedder cannot be built")

    monkeypatch.setattr(embedder_module, "_build", broken)
    alice = make_account(handle="alice")
    note = address(graph.put(conn, alice, text="okapi field notes", title="Okapi"))
    assert "Okapi" in handlers.get(conn, alice, ref=note)


def test_deleting_a_note_drops_its_vectors_of_every_model(
    conn, make_account, monkeypatch
):
    use_hash_embedder(monkeypatch)
    alice = make_account(handle="alice")
    note = address(graph.put(conn, alice, text="ibis field notes", title="Ibis"))
    node_id = conn.execute(
        "SELECT id FROM node WHERE address = %s", (note,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
        " VALUES (%s, 'other-3', 3, '[1,2,3]', 'x')",
        (node_id,),
    )
    graph.delete(conn, alice, ref=note)
    left = conn.execute(
        "SELECT count(*) AS n FROM embedding WHERE node_id = %s", (node_id,)
    ).fetchone()["n"]
    assert left == 0


# ------------------------------------------------------------------- the worker


@pytest.fixture
def server(fresh_db_url, monkeypatch):
    """What ``server.build_app`` gives the start hook: settings and an open pool
    on a database with real commits. Stops the worker afterwards."""
    monkeypatch.setattr(worker, "NUDGE_DELAYS", (0.05, 0.1, 0.2, 0.4, 0.8))
    db = Database(fresh_db_url)
    db.open()
    try:
        yield Settings(database_url=fresh_db_url, embedder="hash"), db
    finally:
        hooks.on_server_stop()
        db.close()


def test_the_worker_loads_the_model_and_embeds_what_was_missed(server, monkeypatch):
    settings, db = server
    slow = SlowEmbedder(delay=0.4)
    use_embedder(monkeypatch, slow)
    with connect(db.url) as c:
        alice = accounts.create_account(c, "alice@example.com", PASSWORD, "alice")
        notes = [
            address(graph.put(c, alice, text=f"lemur note {i}", title=f"Lemur {i}"))
            for i in range(5)
        ]
        c.commit()
        assert vectors(c, slow.model) == 0  # written while there was no model
        total = c.execute(
            "SELECT count(*) AS n FROM node WHERE tombstoned_at IS NULL"
        ).fetchone()["n"]

    started = time.monotonic()
    hooks.on_server_start(settings, db)
    assert time.monotonic() - started < 0.2  # the start does not wait for the model
    assert not slow.ready

    with connect(db.url, autocommit=True) as c:
        # everything written before the start is embedded, nobody asked
        assert wait_until(lambda: vectors(c, slow.model) == total, 10)
        assert slow.ready and slow.loads == 1
        assert index.has_vector_index(c, slow)

        # a write made while the server runs: the hook nudges the worker
        with connect(db.url) as w:
            new = address(graph.put(w, alice, text="lemur note, late", title="Late"))
            graph.edit(w, alice, ref=notes[0], text="lemur note 0, second version")
            w.commit()
        fresh = (
            "SELECT count(*) AS n FROM node n JOIN embedding e ON e.node_id = n.id"
            " AND e.model = %s AND e.version_hash = n.head_version"
            " WHERE n.address = ANY(%s)"
        )
        assert wait_until(
            lambda: (
                c.execute(fresh, (slow.model, [new, notes[0]])).fetchone()["n"] == 2
            ),
            worker.IDLE_SECONDS / 10,  # far sooner than the idle poll
        )
        assert vectors(c, slow.model) == total + 1

    running = worker._worker
    assert running is not None and running.alive()
    hooks.on_server_stop()
    assert not running.alive() and running.state == "stopped"


def test_the_worker_keeps_trying_a_model_that_will_not_load(
    server, monkeypatch, caplog
):
    settings, db = server
    monkeypatch.setattr(worker, "LOAD_RETRY_FIRST", 0.05)
    slow = SlowEmbedder(delay=0.0, failures=2)
    use_embedder(monkeypatch, slow)
    with connect(db.url) as c:
        alice = accounts.create_account(c, "alice@example.com", PASSWORD, "alice")
        graph.put(c, alice, text="gecko note", title="Gecko")
        c.commit()
    caplog.set_level(logging.WARNING, logger="reticula_mcp.search")

    hooks.on_server_start(settings, db)
    with connect(db.url, autocommit=True) as c:
        assert wait_until(lambda: vectors(c, slow.model) > 0, 10)
    assert slow.loads == 3
    failures = [r for r in caplog.records if "did not load" in r.getMessage()]
    assert len(failures) == 2


def test_no_worker_is_started_for_the_hash_embedder(server, monkeypatch):
    settings, db = server
    use_hash_embedder(monkeypatch)
    hooks.on_server_start(settings, db)
    assert worker._worker is None


def test_only_one_process_catches_up_at_a_time(server, monkeypatch):
    settings, db = server
    slow = SlowEmbedder(delay=0.0)
    slow.load()
    with connect(db.url) as c:
        alice = accounts.create_account(c, "alice@example.com", PASSWORD, "alice")
        make_atom(c, alice, "newt note", title="Newt")
        c.commit()
    one = worker.Worker(db, slow)
    with connect(db.url) as other:  # another server process is catching up
        other.execute("SELECT pg_advisory_xact_lock(%s)", (worker.LOCK_KEY,))
        assert one._pass() == 0
        other.rollback()
    assert one._pass() > 0


def test_the_start_hook_never_raises(server, monkeypatch):
    settings, db = server

    def broken(kind, model_name):
        raise RuntimeError("the embedder cannot be built")

    monkeypatch.setattr(embedder_module, "_build", broken)
    hooks.on_server_start(settings, db)  # logged, not raised
    assert worker._worker is None
    hooks.on_server_stop()


# ------------------------------------------------------------ where models live


def test_model_files_go_where_the_operator_says_or_under_the_home_cache(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "chosen"))
    assert embedder_module.model_cache_dir() == str(tmp_path / "chosen")

    monkeypatch.delenv("FASTEMBED_CACHE_PATH")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    expected = tmp_path / "cache" / "reticula" / "fastembed"
    assert embedder_module.model_cache_dir() == str(expected)
    assert expected.is_dir()  # not the system temp directory

    # a place that cannot be written: fastembed's own default is used
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    assert embedder_module.model_cache_dir() is None


def test_the_hash_embedder_is_always_ready():
    assert embedder_module.is_ready(HashEmbedder())
    embedder_module.warm(HashEmbedder())  # nothing to do, nothing raised


# --------------------------------------------------------------- the real model


def test_real_model_first_search_does_not_wait_and_later_ones_use_it(
    conn, make_account, monkeypatch
):
    real = use_real_model(monkeypatch)  # a new instance: nothing loaded
    assert not embedder_module.is_ready(real)
    alice = make_account(handle="alice")
    right = make_atom(conn, alice, "How to bake sourdough bread with a starter.")
    make_atom(conn, alice, "Quarterly tax filing deadlines for small businesses.")
    make_atom(conn, alice, "Notes on the migration of monarch butterflies.")

    first = handlers.search(conn, alice, query="sourdough")
    assert right["address"][-8:] in first and LOADING in first

    if not wait_until(lambda: embedder_module.is_ready(real), 120):
        pytest.skip(
            f"real embedding model unavailable: {embedder_module.load_error(real)}"
        )
    out = handlers.search(conn, alice, query="making loaves of leavened wheat dough")
    rows = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert rows and right["address"][-8:] in rows[0]
    assert "keyword matches only" not in out
    assert vectors(conn, real.model) == 3


def test_real_model_write_does_not_load_it(conn, make_account, monkeypatch):
    real = use_real_model(monkeypatch)
    alice = make_account(handle="alice")
    started = time.monotonic()
    graph.put(conn, alice, text="quokka field notes", title="Quokka")
    assert time.monotonic() - started < 2.0
    assert not embedder_module.is_ready(real)
    assert vectors(conn, real.model) == 0
