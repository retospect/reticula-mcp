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
from reticula_mcp.resolve import resolve_ref
from tests.graph_support import mk_hub, mk_note, share
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
    + readable_among_sql(
        "SELECT a.id FROM node a WHERE right(a.address, %(n)s) = %(suffix)s"
    )
    + ") AND right(n.address, %(n)s) = %(suffix)s ORDER BY n.id LIMIT 6"
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
    assert (
        '"SELECT a.id FROM node a WHERE right(a.address, %(n)s) = %(suffix)s"' in source
    )
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
                    NEW_BY_SUFFIX,
                    {"viewer": viewer, "n": len(suffix), "suffix": suffix},
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
    # by suffix: one pass over the addresses for the suffix (an index for it is
    # another change), and nothing more per readable note
    suffix = address[-8:]
    old = _rows_read(conn, OLD_BY_SUFFIX, (ada.id, 8, suffix))
    new = _rows_read(conn, NEW_BY_SUFFIX, {"viewer": ada.id, "n": 8, "suffix": suffix})
    assert old >= 2 * NOTES, old
    assert new <= NOTES + 30, new
