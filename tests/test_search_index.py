"""Embedding index: rebuild changes no shared data, lazy catch-up, write hook, CLI."""

from __future__ import annotations

import pytest

from reticula_mcp import accounts
from reticula_mcp.cli import main
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.search import handlers, hooks, index, render
from reticula_mcp.search.embedder import HashEmbedder, get_embedder
from tests.helpers import make_atom
from tests.search_support import (
    file_under,
    hub_of,
    tombstone,
    use_hash_embedder,
)

SHARED_TABLES = (
    "record",
    "node",
    "membership",
    "hub_grant",
    "supersedes",
    "link",
    "account",
)


@pytest.fixture(autouse=True)
def world(monkeypatch):
    use_hash_embedder(monkeypatch)


def checksums(conn) -> dict[str, str]:
    out = {}
    for table in SHARED_TABLES:
        row = conn.execute(
            f"SELECT count(*) AS n, md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) AS h"
            f" FROM {table} t"
        ).fetchone()
        out[table] = f"{row['n']}:{row['h']}"
    return out


def vec_count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM embedding").fetchone()["n"]


def test_dropping_and_rebuilding_the_index_changes_no_shared_data(conn, make_account):
    alice = make_account(handle="alice")
    hub = hub_of(conn, alice, "Garden")
    for i, word in enumerate(["tulip", "orchid", "fern"]):
        n = make_atom(conn, alice, f"notes on the {word}", title=word)
        file_under(conn, n, hub)
    before = handlers.search(conn, alice, query="orchid")
    assert vec_count(conn) == 4  # lazily embedded by the search above
    shared = checksums(conn)

    written = index.reindex(conn, get_embedder(Settings.from_env()), drop=True)
    assert written == 4 and vec_count(conn) == 4
    index.reindex(conn, get_embedder(Settings.from_env()), drop=True)
    conn.execute("DELETE FROM embedding")  # and a raw wipe
    assert vec_count(conn) == 0

    assert checksums(conn) == shared
    assert handlers.search(conn, alice, query="orchid") == before  # works again
    assert vec_count(conn) == 4
    assert checksums(conn) == shared


def test_catch_up_is_lazy_bounded_and_restamps_stale_rows(conn, make_account):
    alice = make_account(handle="alice")
    emb = HashEmbedder()
    notes = [make_atom(conn, alice, f"note {i}", title=f"t{i}") for i in range(5)]
    assert vec_count(conn) == 0
    assert index.catch_up(conn, emb, alice.id, limit=2) == 2  # bounded
    assert vec_count(conn) == 2
    assert index.catch_up(conn, emb, alice.id) == 3  # the rest
    assert index.catch_up(conn, emb, alice.id) == 0  # nothing left
    # a new head version makes the stored vector stale
    conn.execute(
        "UPDATE node SET head_version = 'sha256:new' WHERE id = %s", (notes[0]["id"],)
    )
    assert index.catch_up(conn, emb, alice.id) == 1
    row = conn.execute(
        "SELECT version_hash FROM embedding WHERE node_id = %s", (notes[0]["id"],)
    ).fetchone()
    assert row["version_hash"] == "sha256:new"


def test_catch_up_only_embeds_what_the_viewer_may_read(conn, make_account):
    alice, bob = make_account(), make_account()
    make_atom(conn, alice, "alice only")
    mine = make_atom(conn, bob, "bob only")
    assert index.catch_up(conn, HashEmbedder(), bob.id) == 1
    assert conn.execute("SELECT node_id FROM embedding").fetchall() == [
        {"node_id": mine["id"]}
    ]


def test_write_hook_embeds_inline_and_forgets_tombstoned(conn, make_account):
    alice = make_account()
    n = make_atom(conn, alice, "hook me", title="Hook")
    assert vec_count(conn) == 0
    hooks.on_node_changed(conn, n["id"])
    assert vec_count(conn) == 1
    tombstone(conn, n)
    hooks.on_node_changed(conn, n["id"])
    assert vec_count(conn) == 0


def test_write_hook_never_fails_the_write(conn, make_account, monkeypatch):
    alice = make_account()
    n = make_atom(conn, alice, "hook me")
    hooks.on_node_changed(conn, 987654321)  # unknown node: nothing happens

    class Broken:
        model, dim, cheap = "broken", 3, True

        def embed(self, texts):
            raise RuntimeError("model fell over")

    monkeypatch.setattr(hooks, "get_embedder", lambda settings: Broken())
    hooks.on_node_changed(conn, n["id"])  # swallowed; the transaction is still usable
    assert conn.execute("SELECT 1 AS x").fetchone() == {"x": 1}
    assert vec_count(conn) == 0


def test_search_survives_a_broken_embedder(conn, make_account, monkeypatch):
    alice = make_account()
    n = make_atom(conn, alice, "resilient marmot", title="Marmot")

    class Broken:
        model, dim, cheap = "broken", 3, True

        def embed(self, texts):
            raise RuntimeError("model fell over")

    from reticula_mcp.search import find

    monkeypatch.setattr(find, "get_embedder", lambda settings: Broken())
    out = handlers.search(conn, alice, query="marmot")
    assert render.short_ref(n["address"]) in out


def test_vectors_of_other_models_never_meet_the_distance_operator(conn, make_account):
    alice = make_account()
    n = make_atom(conn, alice, "lonely lemur")
    # a vector of another model and another dimension sits in the table
    conn.execute(
        "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
        " VALUES (%s, 'other-3', 3, '[1,2,3]', 'x')",
        (n["id"],),
    )
    assert render.short_ref(n["address"]) in handlers.search(conn, alice, query="lemur")


def test_reindex_cli_rebuilds_without_touching_shared_tables(
    fresh_db_url, monkeypatch, capsys
):
    monkeypatch.setenv("RETICULA_DATABASE_URL", fresh_db_url)
    with connect(fresh_db_url) as c:
        alice = accounts.create_account(c, "cli@example.com", "pw-pw-pw-pw")
        make_atom(c, alice, "cli note about otters", title="Otters")
        make_atom(c, alice, "cli note about badgers", title="Badgers")
        c.commit()
        shared = checksums(c)
    assert main(["reindex", "--drop"]) == 0
    assert "embedded 2 nodes with hash-256" in capsys.readouterr().out
    assert main(["reindex"]) == 0
    with connect(fresh_db_url) as c:
        assert vec_count(c) == 2
        assert checksums(c) == shared
