"""Switching the embedding model on a database that already has data is safe.

Rows of the model that is not current are ignored and left alone, a search and
``reindex`` fill the current model without anyone asking, and the vector index
of the current model (and no other) serves the indexed form of the vector leg.
Each claim is shown with two cheap stand-in models and, where the fastembed
extra is installed, with hash -> real model -> hash.
"""

from __future__ import annotations

from typing import Any

import pytest
from pgvector import Vector

from reticula_mcp import accounts
from reticula_mcp.cli import main
from reticula_mcp.db import connect
from reticula_mcp.search import find, handlers, hooks, index, render
from reticula_mcp.search.embedder import Embedder, HashEmbedder
from tests.helpers import make_atom
from tests.search_support import load_real_model, tombstone, use_embedder

HASH = HashEmbedder()  # model "hash-256"
ALT = HashEmbedder(dim=384, model="alt")  # model "alt-384"
ALT_SAME_DIM = HashEmbedder(dim=256, model="alt")  # model "alt-256"
SEMANTIC_SQL = find.semantic_sql  # the real one, before any spy

KILN = "firing the ceramic kiln at high temperature for the glaze"
GARDEN = "planting tulip bulbs in the autumn garden"
TAXES = "filing the annual tax return before the deadline"
HIKE = "mountain hiking trail through the alpine meadow"
QUERIES = {
    "kiln": "ceramic kiln glaze firing",
    "garden": "tulip bulbs autumn garden",
    "taxes": "annual tax return deadline",
    "hike": "alpine mountain hiking trail",
}


# ------------------------------------------------------------------- helpers


def notes(conn, owner) -> dict[str, dict[str, Any]]:
    return {
        "kiln": make_atom(conn, owner, KILN, title="Kiln"),
        "garden": make_atom(conn, owner, GARDEN, title="Garden"),
        "taxes": make_atom(conn, owner, TAXES, title="Taxes"),
        "hike": make_atom(conn, owner, HIKE, title="Hike"),
    }


def stored(conn, model: str) -> list[dict[str, Any]]:
    """Every row of one model, as it is on disk."""
    return conn.execute(
        "SELECT node_id, dim, version_hash, vec::text AS vec FROM embedding"
        " WHERE model = %s ORDER BY node_id",
        (model,),
    ).fetchall()


def node_ids(rows: list[dict[str, Any]]) -> set[int]:
    return {r["node_id"] for r in rows}


def live_ids(conn) -> set[int]:
    return {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM node WHERE tombstoned_at IS NULL"
        ).fetchall()
    }


def vector_indexes(conn) -> set[str]:
    return {
        r["indexname"]
        for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'embedding'"
        ).fetchall()
        if r["indexname"].startswith(index.INDEX_PREFIX)
    }


def semantic_ids(found: find.Found) -> set[int]:
    return {h.node_id for h in found.hits if "semantic" in h.legs}


def search_finds(conn, viewer, note: dict[str, Any], query: str) -> None:
    """The vector leg took part, found the note, and the rendered search shows
    it."""
    found = find.hybrid_search(conn, viewer, query)
    assert found.semantic == find.SEMANTIC_ON
    assert note["id"] in semantic_ids(found)
    assert render.short_ref(note["address"]) in handlers.search(
        conn, viewer, query=query
    )


@pytest.fixture
def indexed_calls(monkeypatch) -> list[tuple[str, int, bool]]:
    """What the vector leg asked of ``semantic_sql``: (model, dim, indexed)."""
    calls: list[tuple[str, int, bool]] = []

    def spy(embedder, dim, where, *, indexed):
        calls.append((embedder.model, dim, indexed))
        return SEMANTIC_SQL(embedder, dim, where, indexed=indexed)

    monkeypatch.setattr(find, "semantic_sql", spy)
    return calls


def count_rows(conn) -> None:
    """Pin the table's row estimate to a busy instance's (rolled back with the
    test). The search reads ``reltuples`` first (``find._candidates``) and
    autovacuum on the throwaway database leaves 0 there, which counts as "no
    candidates" even with ``EXACT_MAX = 0``; the planner needs a stable figure
    too to pick the vector index on tables this small."""
    conn.execute(
        "UPDATE pg_class SET reltuples = 1000, relpages = 100"
        " WHERE oid = 'embedding'::regclass"
    )


def vector_leg(conn, embedder: Embedder, viewer, query: str) -> list[int]:
    """The ids the vector leg returns for ``embedder``, with no catch-up."""
    where, params = find.scope(
        conn, viewer, hub_node_id=None, kind=None, include_superseded=False
    )
    return [i for i, _ in find._semantic(conn, embedder, query, where, params, 50)]


def planned_indexes(conn, embedder: Embedder, viewer, query: str) -> set[str]:
    """Names of the indexes the indexed form of the vector leg would scan, with
    sequential scans off (the tables here are far too small for the planner to
    want an index)."""
    where, params = find.scope(
        conn, viewer, hub_node_id=None, kind=None, include_superseded=False
    )
    args = {
        **params,
        "vec": Vector(embedder.embed([query])[0]),
        "max": 1.0,
        "pool": 50,
    }
    statement = SEMANTIC_SQL(embedder, embedder.dim, where, indexed=True)
    count_rows(conn)
    conn.execute("SET LOCAL enable_seqscan = off")
    try:
        row = conn.execute("EXPLAIN (FORMAT JSON) " + statement, args).fetchone()
    finally:
        conn.execute("RESET enable_seqscan")
    names: set[str] = set()
    todo = [row["QUERY PLAN"][0]["Plan"]]
    while todo:
        step = todo.pop()
        if "Index Name" in step:
            names.add(step["Index Name"])
        todo.extend(step.get("Plans", []))
    return names


def hnsw_names(names: set[str]) -> set[str]:
    return {n for n in names if n.startswith(index.INDEX_PREFIX)}


# ------------------------------------------------- claim 1: other model ignored


def test_rows_of_the_other_model_are_ignored_untouched_and_used_again_on_return(
    conn, make_account, monkeypatch
):
    alice = make_account(handle="alice")
    n = notes(conn, alice)

    use_embedder(monkeypatch, HASH)
    search_finds(conn, alice, n["kiln"], QUERIES["kiln"])
    hash_rows = stored(conn, HASH.model)
    assert node_ids(hash_rows) == live_ids(conn) and {r["dim"] for r in hash_rows} == {
        256
    }

    # the switch to a model of another dimension: no error, the leg takes part
    use_embedder(monkeypatch, ALT)
    for name in ("kiln", "garden", "taxes", "hike"):
        search_finds(conn, alice, n[name], QUERIES[name])
    alt_rows = stored(conn, ALT.model)
    assert node_ids(alt_rows) == live_ids(conn) and {r["dim"] for r in alt_rows} == {
        384
    }
    assert stored(conn, HASH.model) == hash_rows  # not one byte changed

    # back: the old rows serve again, nothing is embedded a second time
    use_embedder(monkeypatch, HASH)
    assert index.missing_ids(conn, HASH, alice.id, 100) == []
    assert index.catch_up(conn, HASH, alice.id) == 0
    search_finds(conn, alice, n["taxes"], QUERIES["taxes"])
    assert stored(conn, HASH.model) == hash_rows
    assert stored(conn, ALT.model) == alt_rows


@pytest.mark.parametrize("current", [HASH, ALT_SAME_DIM])
def test_the_vector_leg_never_reads_a_row_of_another_model_of_the_same_dimension(
    conn, make_account, monkeypatch, current
):
    """Same dimension makes the distance operator work on a foreign row, so only
    the model filter keeps it out: a perfect match of the other model is not a
    hit."""
    other = ALT_SAME_DIM if current is HASH else HASH
    assert other.dim == current.dim and other.model != current.model
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    twin = make_atom(conn, alice, "ceramic kiln glaze firing schedule", title="Twin")
    use_embedder(monkeypatch, current)
    index.catch_up(conn, current, alice.id)
    # the kiln note has no vector of the current model, only an exact match of
    # the query under the other model; its twin is a hit of the current one
    conn.execute(
        "DELETE FROM embedding WHERE node_id = %s AND model = %s",
        (n["kiln"]["id"], current.model),
    )
    conn.execute(
        "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
        " VALUES (%s, %s, %s, %s, %s)",
        (
            n["kiln"]["id"],
            other.model,
            other.dim,
            Vector(other.embed([QUERIES["kiln"]])[0]),
            n["kiln"]["head_version"],
        ),
    )
    planted = stored(conn, other.model)

    exact = vector_leg(conn, current, alice, QUERIES["kiln"])
    assert twin["id"] in exact and n["kiln"]["id"] not in exact

    index.ensure_vector_index(conn, current)
    where, params = find.scope(
        conn, alice, hub_node_id=None, kind=None, include_superseded=False
    )
    args = {
        **params,
        "vec": Vector(current.embed([QUERIES["kiln"]])[0]),
        "max": 1.0,
        "pool": 50,
    }
    rows = conn.execute(
        SEMANTIC_SQL(current, current.dim, where, indexed=True), args
    ).fetchall()
    assert twin["id"] in {r["id"] for r in rows}
    assert n["kiln"]["id"] not in {r["id"] for r in rows}
    assert stored(conn, other.model) == planted


def test_a_search_stays_correct_with_rows_of_two_other_models_in_the_table(
    conn, make_account, monkeypatch
):
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    for model in (HASH, ALT, HashEmbedder(dim=3, model="tiny")):
        use_embedder(monkeypatch, model)
        index.catch_up(conn, model, alice.id)
    assert {
        r["model"]
        for r in conn.execute("SELECT DISTINCT model FROM embedding").fetchall()
    } == {"hash-256", "alt-384", "tiny-3"}
    use_embedder(monkeypatch, HASH)
    for name in ("kiln", "garden"):
        search_finds(conn, alice, n[name], QUERIES[name])


# --------------------------------------------------- claim 2: catch-up on its own


def test_after_a_switch_a_search_embeds_what_the_viewer_reads_under_the_new_model(
    conn, make_account, monkeypatch
):
    alice, bob = make_account(handle="alice"), make_account(handle="bob")
    mine = notes(conn, alice)
    bobs = make_atom(conn, bob, "bob keeps pottery glaze recipes", title="Pottery")

    use_embedder(monkeypatch, HASH)
    search_finds(conn, alice, mine["kiln"], QUERIES["kiln"])
    assert bobs["id"] not in node_ids(stored(conn, HASH.model))  # not alice's to read
    hash_rows = stored(conn, HASH.model)

    use_embedder(monkeypatch, ALT)
    wanted = {m["id"] for m in mine.values()}
    assert set(index.missing_ids(conn, ALT, alice.id, 100)) == wanted
    assert bobs["id"] in index.missing_ids(conn, ALT, None, 100)  # not scoped
    assert stored(conn, ALT.model) == []  # nobody ran reindex

    search_finds(conn, alice, mine["kiln"], QUERIES["kiln"])
    assert node_ids(stored(conn, ALT.model)) == wanted  # alice's world, no more
    assert index.missing_ids(conn, ALT, alice.id, 100) == []
    assert index.missing_ids(conn, ALT, bob.id, 100) == [bobs["id"]]

    # bob's own search catches his note up
    found = find.hybrid_search(conn, bob, "pottery glaze recipes")
    assert found.semantic == find.SEMANTIC_ON and bobs["id"] in semantic_ids(found)
    assert node_ids(stored(conn, ALT.model)) == wanted | {bobs["id"]}
    assert stored(conn, HASH.model) == hash_rows


def test_a_node_changed_while_the_other_model_was_current_is_fresh_on_return(
    conn, make_account, monkeypatch
):
    """Staleness is the stamp ``version_hash != head_version`` of the CURRENT
    model's row alone (``missing_ids``), so a change that only the write hook of
    the then-current model saw leaves the other model's row stale and found."""
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    kiln = n["kiln"]
    for model in (HASH, ALT):
        use_embedder(monkeypatch, model)
        index.catch_up(conn, model, alice.id)
    assert index.missing_ids(conn, HASH, alice.id, 100) == []
    assert index.missing_ids(conn, ALT, alice.id, 100) == []

    # while the hash model is current, the kiln note gets a new head
    use_embedder(monkeypatch, HASH)
    rewritten = "a wholly different text about sourdough starters and rye flour"
    conn.execute(
        "UPDATE node SET text = %s, head_version = 'sha256:second' WHERE id = %s",
        (rewritten, kiln["id"]),
    )
    hooks.on_node_changed(conn, kiln["id"])
    fresh = {r["node_id"]: r["version_hash"] for r in stored(conn, HASH.model)}
    stale = {r["node_id"]: r["version_hash"] for r in stored(conn, ALT.model)}
    assert fresh[kiln["id"]] == "sha256:second"  # the write hook restamped it
    assert stale[kiln["id"]] == kiln["head_version"]  # the other model did not
    assert index.missing_ids(conn, HASH, alice.id, 100) == []
    assert index.missing_ids(conn, ALT, alice.id, 100) == [kiln["id"]]

    # back to the other model: the next search of the viewer refreshes the row
    use_embedder(monkeypatch, ALT)
    found = find.hybrid_search(conn, alice, "sourdough starters rye flour")
    assert found.semantic == find.SEMANTIC_ON and kiln["id"] in semantic_ids(found)
    row = next(r for r in stored(conn, ALT.model) if r["node_id"] == kiln["id"])
    assert row["version_hash"] == "sha256:second"
    close = conn.execute(
        "SELECT vec <=> %s::vector AS d FROM embedding WHERE node_id = %s"
        " AND model = %s",
        (
            Vector(ALT.embed([index.node_text("Kiln", rewritten)])[0]),
            kiln["id"],
            ALT.model,
        ),
    ).fetchone()
    assert close["d"] < 1e-6  # computed from the new text, not the old one
    assert index.missing_ids(conn, ALT, alice.id, 100) == []
    assert {r["node_id"] for r in stored(conn, HASH.model)} == live_ids(conn)


# ------------------------------------------------------------ claim 3: reindex


def test_reindex_fills_the_current_model_for_every_node_and_keeps_the_other(
    conn, make_account, monkeypatch
):
    """What ``reindex`` (without ``--drop``) does with the other model's rows:
    it leaves them as they are, stale or not, except for the nodes tombstoned
    since, whose vectors it removes for every model. It embeds every live node
    of the instance, readable by this viewer or not, and again even when the
    row is fresh."""
    alice, bob = make_account(handle="alice"), make_account(handle="bob")
    n = notes(conn, alice)
    bobs = make_atom(conn, bob, "bob keeps pottery glaze recipes", title="Pottery")
    for model in (HASH, ALT):
        use_embedder(monkeypatch, model)
        index.catch_up(conn, model, None)
    # the hash model's row of the taxes note is stale, and the hike note dies
    conn.execute(
        "UPDATE node SET head_version = 'sha256:later' WHERE id = %s",
        (n["taxes"]["id"],),
    )
    tombstone(conn, n["hike"])
    kept = {r["node_id"]: r for r in stored(conn, HASH.model)}
    assert n["hike"]["id"] in kept

    use_embedder(monkeypatch, ALT)
    live = live_ids(conn)
    assert n["hike"]["id"] not in live and bobs["id"] in live
    assert index.reindex(conn, ALT) == len(live)

    alt_rows = stored(conn, ALT.model)
    assert node_ids(alt_rows) == live  # every live node, bob's included
    assert {
        r["version_hash"] for r in alt_rows if r["node_id"] == n["taxes"]["id"]
    } == {"sha256:later"}
    assert index.missing_ids(conn, ALT, None, 100) == []
    hash_rows = {r["node_id"]: r for r in stored(conn, HASH.model)}
    assert n["hike"]["id"] not in hash_rows  # vectors of a dead node go, all models
    for node_id, row in hash_rows.items():
        assert row == kept[node_id]  # the rest is exactly as it was, stale included
    assert hash_rows[n["taxes"]["id"]]["version_hash"] != "sha256:later"

    # a second run writes them all again
    assert index.reindex(conn, ALT) == len(live)
    assert stored(conn, ALT.model) == alt_rows
    # the index of the model it filled exists
    assert index.vector_index_name(ALT.model, ALT.dim) in vector_indexes(conn)

    # and the other direction, now that the first model is current again
    use_embedder(monkeypatch, HASH)
    assert index.reindex(conn, HASH) == len(live)
    assert node_ids(stored(conn, HASH.model)) == live
    assert stored(conn, ALT.model) == alt_rows


def test_reindex_drop_empties_every_model_and_removes_the_other_vector_indexes(
    conn, make_account, monkeypatch
):
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    for model in (HASH, ALT):
        use_embedder(monkeypatch, model)
        index.ensure_vector_index(conn, model)
        index.catch_up(conn, model, alice.id)
    assert vector_indexes(conn) == {
        index.vector_index_name("hash-256", 256),
        index.vector_index_name("alt-384", 384),
    }

    use_embedder(monkeypatch, HASH)
    live = live_ids(conn)
    assert index.reindex(conn, HASH, drop=True) == len(live)
    assert stored(conn, ALT.model) == []
    assert node_ids(stored(conn, HASH.model)) == live
    assert vector_indexes(conn) == {index.vector_index_name("hash-256", 256)}
    search_finds(conn, alice, n["kiln"], QUERIES["kiln"])


def test_reindex_command_fills_each_model_in_turn_and_drop_leaves_one(
    fresh_db_url, monkeypatch, capsys
):
    monkeypatch.setenv("RETICULA_DATABASE_URL", fresh_db_url)
    with connect(fresh_db_url) as c:
        alice = accounts.create_account(c, "switch@example.com", "pw-pw-pw-pw")
        make_atom(c, alice, "cli note about otters", title="Otters")
        make_atom(c, alice, "cli note about badgers", title="Badgers")
        c.commit()

    use_embedder(monkeypatch, HASH)
    assert main(["reindex"]) == 0
    assert "embedded 2 nodes with hash-256" in capsys.readouterr().out
    with connect(fresh_db_url) as c:
        hash_rows = stored(c, HASH.model)
        assert len(hash_rows) == 2

    use_embedder(monkeypatch, ALT)
    assert main(["reindex"]) == 0
    assert "embedded 2 nodes with alt-384" in capsys.readouterr().out
    with connect(fresh_db_url) as c:
        assert len(stored(c, ALT.model)) == 2
        assert stored(c, HASH.model) == hash_rows
        assert vector_indexes(c) == {
            index.vector_index_name("hash-256", 256),
            index.vector_index_name("alt-384", 384),
        }

    use_embedder(monkeypatch, HASH)
    assert main(["reindex", "--drop"]) == 0
    assert "embedded 2 nodes with hash-256" in capsys.readouterr().out
    with connect(fresh_db_url) as c:
        assert stored(c, ALT.model) == []
        assert len(stored(c, HASH.model)) == 2
        assert vector_indexes(c) == {index.vector_index_name("hash-256", 256)}


# ------------------------------------------------------ claim 4: the vector index


def test_each_model_has_its_own_index_and_the_plan_uses_the_current_ones(
    conn, make_account, monkeypatch
):
    alice = make_account(handle="alice")
    notes(conn, alice)
    hash_index = index.vector_index_name(HASH.model, HASH.dim)
    alt_index = index.vector_index_name(ALT.model, ALT.dim)
    assert hash_index != alt_index and hash_index.endswith("_256")
    assert alt_index.endswith("_384")

    use_embedder(monkeypatch, HASH)
    assert index.ensure_vector_index(conn, HASH) == hash_index
    assert index.catch_up(conn, HASH, alice.id) > 0
    assert index.has_vector_index(conn, HASH) and not index.has_vector_index(conn, ALT)
    assert planned_indexes(conn, HASH, alice, QUERIES["kiln"]) & {
        hash_index,
        alt_index,
    } == {hash_index}

    # the switch: the new model's index is made before its vectors are
    use_embedder(monkeypatch, ALT)
    assert index.ensure_vector_index(conn, ALT) == alt_index
    assert index.ensure_vector_index(conn, ALT) == alt_index  # idempotent
    assert index.catch_up(conn, ALT, alice.id) > 0
    assert vector_indexes(conn) == {hash_index, alt_index}
    definitions = {
        r["indexname"]: r["indexdef"]
        for r in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'embedding'"
        ).fetchall()
    }
    assert "vector(384)" in definitions[alt_index]
    assert "model = 'alt-384'" in definitions[alt_index]
    assert "vector(256)" in definitions[hash_index]
    plan = planned_indexes(conn, ALT, alice, QUERIES["kiln"])
    assert hnsw_names(plan) == {alt_index}  # the current one, and not the first

    use_embedder(monkeypatch, HASH)  # and back
    plan = planned_indexes(conn, HASH, alice, QUERIES["kiln"])
    assert hnsw_names(plan) == {hash_index}


def test_dropping_the_other_vector_indexes_keeps_the_current_one_and_all_rows(
    conn, make_account, monkeypatch
):
    alice = make_account(handle="alice")
    notes(conn, alice)
    for model in (HASH, ALT):
        use_embedder(monkeypatch, model)
        index.ensure_vector_index(conn, model)
        index.catch_up(conn, model, alice.id)
    rows = (stored(conn, HASH.model), stored(conn, ALT.model))
    hash_index = index.vector_index_name(HASH.model, HASH.dim)
    alt_index = index.vector_index_name(ALT.model, ALT.dim)

    assert index.drop_other_vector_indexes(conn, ALT) == [hash_index]
    assert vector_indexes(conn) == {alt_index}
    assert index.drop_other_vector_indexes(conn, ALT) == []  # nothing left to drop
    assert index.has_vector_index(conn, ALT) and not index.has_vector_index(conn, HASH)
    # the table and its primary key are not indexes of a model
    assert conn.execute("SELECT to_regclass('embedding_pkey') AS r").fetchone()["r"]
    assert (stored(conn, HASH.model), stored(conn, ALT.model)) == rows

    # the plan of the dropped model has nothing of its own to use any more
    assert hash_index not in planned_indexes(conn, HASH, alice, QUERIES["kiln"])
    # and ensure brings it back, next to the other, when the model returns
    use_embedder(monkeypatch, HASH)
    assert index.ensure_vector_index(conn, HASH) == hash_index
    assert vector_indexes(conn) == {hash_index, alt_index}
    assert hnsw_names(planned_indexes(conn, HASH, alice, QUERIES["kiln"])) == {
        hash_index
    }


def test_the_indexed_vector_leg_answers_right_after_every_switch(
    conn, make_account, monkeypatch, indexed_calls
):
    """With ``EXACT_MAX = 0`` every search goes down the indexed path as soon as
    the current model has an index (the worker and the command line make it; a
    search does not), and falls back to the exact scan before that."""
    monkeypatch.setattr(find, "EXACT_MAX", 0)
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    names = ("kiln", "garden", "taxes", "hike")

    def all_found(expected: tuple[str, int, bool]) -> None:
        indexed_calls.clear()
        for name in names:
            search_finds(conn, alice, n[name], QUERIES[name])
        assert set(indexed_calls) == {expected}

    use_embedder(monkeypatch, HASH)
    all_found(("hash-256", 256, False))  # no index yet: the exact scan answers
    index.ensure_vector_index(conn, HASH)
    count_rows(conn)
    all_found(("hash-256", 256, True))

    use_embedder(monkeypatch, ALT)  # switched, and its index is not there yet
    all_found(("alt-384", 384, False))
    index.ensure_vector_index(conn, ALT)
    count_rows(conn)
    all_found(("alt-384", 384, True))
    assert index.has_vector_index(conn, HASH)  # the first one is still there

    use_embedder(monkeypatch, HASH)  # back
    all_found(("hash-256", 256, True))

    # both forms agree on who is a hit
    indexed = vector_leg(conn, HASH, alice, QUERIES["garden"])
    monkeypatch.setattr(find, "EXACT_MAX", 20_000)
    assert set(vector_leg(conn, HASH, alice, QUERIES["garden"])) == set(indexed)
    assert n["garden"]["id"] in indexed

    # the last form used, as the planner sees it, reads the current index only
    monkeypatch.setattr(find, "EXACT_MAX", 0)
    use_embedder(monkeypatch, ALT)
    indexed_calls.clear()
    search_finds(conn, alice, n["kiln"], QUERIES["kiln"])
    assert indexed_calls[-1] == ("alt-384", 384, True)
    assert hnsw_names(planned_indexes(conn, ALT, alice, QUERIES["kiln"])) == {
        index.vector_index_name("alt-384", 384)
    }


def test_the_indexed_path_ignores_a_foreign_row_of_another_dimension(
    conn, make_account, monkeypatch, indexed_calls
):
    """The cast of the indexed expression sits behind the model filter, so a
    vector of another dimension in the same table never reaches it."""
    monkeypatch.setattr(find, "EXACT_MAX", 0)
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    use_embedder(monkeypatch, HASH)
    index.ensure_vector_index(conn, HASH)
    index.catch_up(conn, HASH, alice.id)
    conn.execute(
        "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
        " VALUES (%s, 'other-3', 3, '[1,2,3]', 'x'), (%s, 'other-3', 3, '[3,2,1]', 'x')",
        (n["kiln"]["id"], n["garden"]["id"]),
    )
    count_rows(conn)
    for name in ("kiln", "garden"):
        search_finds(conn, alice, n[name], QUERIES[name])
    assert indexed_calls and {c[2] for c in indexed_calls} == {True}


# ---------------------------------------- hash -> real model -> hash, fastembed


def test_switching_to_a_real_model_and_back_keeps_both_sets_of_vectors(
    conn, make_account, monkeypatch
):
    real = load_real_model(monkeypatch)
    alice = make_account(handle="alice")
    n = notes(conn, alice)

    use_embedder(monkeypatch, HASH)
    search_finds(conn, alice, n["kiln"], QUERIES["kiln"])
    hash_rows = stored(conn, HASH.model)
    assert node_ids(hash_rows) == live_ids(conn)

    use_embedder(monkeypatch, real)
    assert real.model != HASH.model and real.dim != HASH.dim
    assert set(index.missing_ids(conn, real, alice.id, 100)) == live_ids(conn)
    found = find.hybrid_search(conn, alice, "pottery oven for glazed ceramics")
    assert found.semantic == find.SEMANTIC_ON
    assert node_ids(stored(conn, real.model)) == live_ids(conn)  # one search did it
    assert {r["dim"] for r in stored(conn, real.model)} == {real.dim}
    assert n["kiln"]["id"] in semantic_ids(found)  # by meaning, not by words
    assert stored(conn, HASH.model) == hash_rows
    real_rows = stored(conn, real.model)

    use_embedder(monkeypatch, HASH)
    assert index.catch_up(conn, HASH, alice.id) == 0
    for name in ("kiln", "taxes"):
        search_finds(conn, alice, n[name], QUERIES[name])
    assert stored(conn, HASH.model) == hash_rows
    assert stored(conn, real.model) == real_rows


def test_a_change_made_under_the_hash_model_reaches_the_real_model_on_return(
    conn, make_account, monkeypatch
):
    real = load_real_model(monkeypatch)
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    kiln = n["kiln"]
    for model in (HASH, real):
        use_embedder(monkeypatch, model)
        index.catch_up(conn, model, alice.id)
    assert index.missing_ids(conn, real, alice.id, 100) == []

    use_embedder(monkeypatch, HASH)
    rewritten = "sourdough bread needs a lively starter and plenty of rye flour"
    conn.execute(
        "UPDATE node SET text = %s, head_version = 'sha256:second' WHERE id = %s",
        (rewritten, kiln["id"]),
    )
    hooks.on_node_changed(conn, kiln["id"])
    assert index.missing_ids(conn, HASH, alice.id, 100) == []
    assert index.missing_ids(conn, real, alice.id, 100) == [kiln["id"]]

    use_embedder(monkeypatch, real)
    found = find.hybrid_search(conn, alice, "baking bread with a sourdough culture")
    assert found.semantic == find.SEMANTIC_ON and kiln["id"] in semantic_ids(found)
    row = next(r for r in stored(conn, real.model) if r["node_id"] == kiln["id"])
    assert row["version_hash"] == "sha256:second"
    assert index.missing_ids(conn, real, alice.id, 100) == []


def test_reindex_and_the_vector_index_follow_a_real_model_and_back(
    conn, make_account, monkeypatch, indexed_calls
):
    monkeypatch.setattr(find, "EXACT_MAX", 0)
    real = load_real_model(monkeypatch)
    alice = make_account(handle="alice")
    n = notes(conn, alice)
    hash_index = index.vector_index_name(HASH.model, HASH.dim)
    real_index = index.vector_index_name(real.model, real.dim)
    assert hash_index != real_index

    use_embedder(monkeypatch, HASH)
    live = live_ids(conn)
    assert index.reindex(conn, HASH) == len(live)
    count_rows(conn)
    assert vector_indexes(conn) == {hash_index}
    assert hnsw_names(planned_indexes(conn, HASH, alice, QUERIES["kiln"])) == {
        hash_index
    }
    hash_rows = stored(conn, HASH.model)

    use_embedder(monkeypatch, real)
    assert index.reindex(conn, real) == len(live)
    count_rows(conn)
    assert node_ids(stored(conn, real.model)) == live
    assert vector_indexes(conn) == {hash_index, real_index}
    assert hnsw_names(planned_indexes(conn, real, alice, QUERIES["kiln"])) == {
        real_index
    }
    indexed_calls.clear()
    found = find.hybrid_search(conn, alice, "pottery oven for glazed ceramics")
    assert found.semantic == find.SEMANTIC_ON and n["kiln"]["id"] in semantic_ids(found)
    assert render.short_ref(n["kiln"]["address"]) in handlers.search(
        conn, alice, query="pottery oven for glazed ceramics"
    )
    assert {c[:2] for c in indexed_calls} == {(real.model, real.dim)}
    assert {c[2] for c in indexed_calls} == {True}
    assert stored(conn, HASH.model) == hash_rows

    use_embedder(monkeypatch, HASH)
    assert hnsw_names(planned_indexes(conn, HASH, alice, QUERIES["kiln"])) == {
        hash_index
    }
    indexed_calls.clear()
    search_finds(conn, alice, n["garden"], QUERIES["garden"])
    assert {c for c in indexed_calls} == {(HASH.model, HASH.dim, True)}

    # the operator who wants only one model left drops the rest
    assert index.reindex(conn, HASH, drop=True) == len(live)
    assert stored(conn, real.model) == []
    assert vector_indexes(conn) == {hash_index}
