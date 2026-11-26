"""``resolve_ref`` asks ``readable_nodes`` about the notes in question only
(``permissions.readable_among_sql``), and answers as before.

* The statements of before are kept here as text and compared with the
  function's on the generated graphs of ``tests/test_readable_hubs.py``: every
  viewer, every address, unknown ones, and suffixes short enough to match
  several notes.
* The rows the plans read from ``node`` and ``membership`` are counted: a
  lookup by address reads a few rows, not every note the viewer may read. The
  gain rests on the planner taking the condition into each branch of
  ``readable_nodes``; the count fails when it stops doing so.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.permissions import readable_among_sql, readable_nodes_sql
from reticula_mcp.resolve import ends_with_params, ends_with_sql, resolve_ref
from tests.graph_support import delete_now, mk_hub, mk_note, share
from tests.helpers import make_atom
from tests.search_support import file_under, hub_of, use_hash_embedder
from tests.test_readable_hubs import World, graph, world  # noqa: F401

# the statements of resolve_ref before readable_among_sql, word for word
OLD_BY_ADDRESS = (
    "SELECT n.* FROM node n WHERE n.address = %s"
    f" AND n.id IN ({readable_nodes_sql('%s')})"
)
OLD_BY_SUFFIX = (
    "SELECT n.* FROM node n"
    f" WHERE n.id IN ({readable_nodes_sql('%s')})"
    " AND right(n.address, %s) = %s ORDER BY n.id LIMIT 6"
)
NEW_BY_ADDRESS = (
    "SELECT n.* FROM node n WHERE n.address = %(ref)s AND n.id IN"
    f" ({readable_among_sql('SELECT a.id FROM node a WHERE a.address = %(ref)s')})"
)
NEW_BY_SUFFIX = (
    "SELECT n.* FROM node n"
    " WHERE n.id IN ("
    + readable_among_sql(f"SELECT a.id FROM node a WHERE {ends_with_sql('a.address')}")
    + ") ORDER BY n.id LIMIT 6"
)
SEEDS = list(range(1000, 1100))


def test_the_statements_here_are_those_of_resolve_ref() -> None:
    """The new statements of this file are the ones resolve.py runs."""
    import inspect

    from reticula_mcp import resolve

    source = inspect.getsource(resolve)
    assert (
        "readable_among_sql('SELECT a.id FROM node a WHERE a.address = %(ref)s')"
        in source
    )
    assert "f\"SELECT a.id FROM node a WHERE {ends_with_sql('a.address')}\"" in source
    assert ") ORDER BY n.id LIMIT 6" in source
    assert "readable_nodes_sql" not in source


def _ids(rows: list[dict[str, Any]]) -> list[int]:
    return [r["id"] for r in rows]


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_the_answers_are_those_of_before(world: World, seed: int) -> None:  # noqa: F811
    conn = world.conn
    rng = random.Random(seed)
    with graph(world, seed):
        addresses = [r["address"] for r in conn.execute("SELECT address FROM node")]
        addresses.append(addresses[0][:-4] + "zzzz")  # an address of no node
        suffixes = {a[-k:] for a in addresses for k in (1, 2, 8)}
        suffixes |= {"".join(rng.choice("0123456789abcdef") for _ in range(6))}
        for viewer in world.viewers:
            for address in addresses:
                old = conn.execute(OLD_BY_ADDRESS, (address, viewer)).fetchall()
                new = conn.execute(
                    NEW_BY_ADDRESS, {"ref": address, "viewer": viewer}
                ).fetchall()
                assert old == new, (seed, viewer, address)
            for suffix in sorted(suffixes):
                old = conn.execute(
                    OLD_BY_SUFFIX, (viewer, len(suffix), suffix)
                ).fetchall()
                new = conn.execute(
                    NEW_BY_SUFFIX, {"viewer": viewer, **ends_with_params(suffix)}
                ).fetchall()
                assert _ids(old) == _ids(new) and old == new, (seed, viewer, suffix)


def test_the_replies_and_errors_are_those_of_before(conn, make_account, monkeypatch):
    use_hash_embedder(monkeypatch)
    ada, ben = make_account(), make_account()
    hub = mk_hub(conn, ada, "Studio")
    share(conn, ada, hub, ben, "read")
    shared = mk_note(conn, ada, "kiln shelf wash", hub=hub)
    private = mk_note(conn, ada, "my own draft", private=True)
    assert resolve_ref(conn, ben, shared)["address"] == shared
    assert resolve_ref(conn, ben, shared[-8:])["address"] == shared
    assert resolve_ref(conn, ben, shared.removeprefix("ret:"))["address"] == shared
    for ref, text in (
        (private, f"[NotFound] no readable note at {private}"),
        (private[-8:], f"[NotFound] no readable note ends in {private[-8:]!r}"),
    ):
        with pytest.raises(ReticulaError) as exc:
            resolve_ref(conn, ben, ref)
        assert str(exc.value).startswith(text), str(exc.value)
    assert resolve_ref(conn, ada, private[-8:])["address"] == private


# ------------------------------------------------------------------- rows read

NOTES = 400


def _rows_read(conn: Conn, statement: str, params: Any) -> int:
    """Rows the plan reads from ``node`` and ``membership``, all loops. As in
    tests/test_current_among_cost.py, sequential scans and hash or merge joins
    are switched off so that a graph of a few hundred notes plans as a big one
    does; a condition that does not reach a branch still reads every row."""
    for setting in ("enable_seqscan", "enable_hashjoin", "enable_mergejoin"):
        conn.execute(f"SET LOCAL {setting} = off")
    (row,) = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params)
    plan = next(iter(row.values()))
    if isinstance(plan, str):
        plan = json.loads(plan)
    total = 0

    def walk(p: dict[str, Any]) -> None:
        nonlocal total
        if p.get("Relation Name") in ("node", "membership"):
            total += int(p.get("Actual Rows", 0)) * int(p.get("Actual Loops", 1))
        for child in p.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    return total


def test_a_lookup_by_address_reads_a_few_rows(conn, make_account):
    ada = make_account()
    hub = hub_of(conn, ada, "Studio")
    notes = [
        make_atom(conn, ada, f"kiln log {i}", title=f"Entry {i}") for i in range(NOTES)
    ]
    for note in notes:
        file_under(conn, note, hub)
    for table in ("node", "membership"):
        conn.execute(f"ANALYZE {table}")
    address = notes[NOTES // 2]["address"]
    old = _rows_read(conn, OLD_BY_ADDRESS, (address, ada.id))
    new = _rows_read(conn, NEW_BY_ADDRESS, {"ref": address, "viewer": ada.id})
    assert old >= NOTES, old  # the count sees a full pass
    assert new <= 20, new
    # by suffix: the index node_address_tail finds the addresses with it
    suffix = address[-8:]
    old = _rows_read(conn, OLD_BY_SUFFIX, (ada.id, 8, suffix))
    new = _rows_read(
        conn, NEW_BY_SUFFIX, {"viewer": ada.id, **ends_with_params(suffix)}
    )
    assert old >= 2 * NOTES, old
    assert new <= 20, new


def test_the_suffix_is_looked_up_through_the_index(conn, make_account):
    """Also in a generic plan: the statement is prepared and run more than
    five times, as on a long-lived connection."""
    ada = make_account()
    notes = [make_atom(conn, ada, f"kiln log {i}") for i in range(50)]
    conn.execute("ANALYZE node")
    conn.execute("SET LOCAL plan_cache_mode = force_generic_plan")
    conn.execute("SET LOCAL enable_seqscan = off")
    suffix = notes[7]["address"][-8:]
    params = {"viewer": ada.id, **ends_with_params(suffix)}
    plan = conn.execute("EXPLAIN (FORMAT JSON) " + NEW_BY_SUFFIX, params).fetchone()
    assert "node_address_tail" in json.dumps(plan)
    for _ in range(7):
        rows = conn.execute(NEW_BY_SUFFIX, params, prepare=True).fetchall()
        assert [r["address"] for r in rows] == [notes[7]["address"]]


@pytest.mark.parametrize("suffix", ["0", "f", "-", "a-b", "ff0", "00000000-0000"])
def test_the_range_holds_every_address_with_the_suffix(conn, suffix):
    tail = ends_with_params(suffix)
    row = conn.execute(
        "SELECT reverse(x) ~>=~ %(tail_from)s AND reverse(x) ~<~ %(tail_to)s AS inside"
        " FROM (VALUES ('ret:p/' || %(tail)s), (%(tail)s), ('zz' || %(tail)s)) v(x)",
        tail,
    ).fetchall()
    assert all(r["inside"] for r in row)


def test_a_withdrawn_note_answers_by_short_id_as_by_address(
    conn, make_account, monkeypatch
):
    from reticula_mcp.search import handlers

    use_hash_embedder(monkeypatch)
    ada, ben, cleo = make_account(), make_account(), make_account()
    hub = mk_hub(conn, ada, "Studio")
    share(conn, ada, hub, ben, "read")
    gone = mk_note(conn, ada, "an old note", hub=hub)
    delete_now(conn, ada, gone)
    for viewer in (ada, ben):
        by_address = handlers.get(conn, viewer, ref=gone)
        assert f"{gone[-8:]} withdrawn" in by_address
        assert handlers.get(conn, viewer, ref=gone[-8:]) == by_address
    # somebody who could never read it learns nothing either way
    for ref in (gone, gone[-8:]):
        with pytest.raises(ReticulaError) as exc:
            handlers.get(conn, cleo, ref=ref)
        assert "[NotFound]" in str(exc.value)
