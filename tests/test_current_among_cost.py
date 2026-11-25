"""``current_among(viewer, ids)`` reads the rows of the ids in question, not
every note the viewer may read.

That rests on the planner: it takes the cut ``= ANY (ARRAY(...))`` into each
branch of the inlined ``readable_nodes``. If a change to either function, or
to Postgres, stops it doing so, the answer stays right
(``tests/test_current_among.py``) and the function becomes as slow as
``current_nodes``. These tests count the rows the plan reads from ``node`` and
``membership``; they do not take time.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from tests.helpers import make_atom
from tests.search_support import file_under, grant, hub_of, supersede

NOTES = 400  # in the hub, read by both viewers
ASKED = 6


def _rows_read(conn: Conn, statement: str, params: dict[str, Any]) -> int:
    """The rows the plan of ``statement`` reads from ``node`` and
    ``membership``, over all loops.

    With a few hundred rows a sequential scan or a hash join over a whole table
    is the cheaper plan, and the planner takes it whatever the condition; at
    30,000 notes it walks from the ids through the indexes. Here that walk is
    asked for (no sequential scan, no hash or merge join), so that a small
    graph shows what a big one does. A condition that does not reach a branch
    still makes the branch read every row, through an index."""
    for setting in ("enable_seqscan", "enable_hashjoin", "enable_mergejoin"):
        conn.execute(f"SET LOCAL {setting} = off")
    (row,) = conn.execute(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params
    ).fetchall()
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


@pytest.fixture
def graph(conn: Conn, make_account) -> tuple[Account, Account, list[int]]:
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = hub_of(conn, ada, "Studio")
    grant(conn, hub, ben, "read")
    notes = []
    for i in range(NOTES):
        note = make_atom(conn, ada, f"kiln log entry {i}", title=f"Entry {i}")
        file_under(conn, note, hub)
        notes.append(note)
    for i in range(0, ASKED, 2):  # half of the asked notes have a newer one
        supersede(conn, notes[-1 - i], notes[i])
    conn.execute("ANALYZE node")
    conn.execute("ANALYZE membership")
    conn.execute("ANALYZE supersedes")
    return ada, ben, [n["id"] for n in notes[:ASKED]]


@pytest.mark.parametrize("who", ["ada", "ben"])
def test_a_short_list_reads_only_its_own_rows(conn: Conn, graph, who: str) -> None:
    ada, ben, ids = graph
    viewer = ada if who == "ada" else ben
    params = {"viewer": viewer.id, "ids": ids}
    among = _rows_read(
        conn, "SELECT node_id FROM current_among(%(viewer)s, %(ids)s::bigint[])", params
    )
    whole = _rows_read(
        conn,
        "SELECT node_id FROM current_nodes(%(viewer)s) WHERE node_id = ANY(%(ids)s)",
        params,
    )
    # the count sees a full pass: current_nodes reads every note
    assert whole >= NOTES, whole
    # the ids, the notes that supersede them, their filings and the hub: a
    # few rows per id, never a pass over the notes
    assert among <= 20 * ASKED, among
    answer = conn.execute(
        "SELECT node_id FROM current_among(%(viewer)s, %(ids)s::bigint[])", params
    ).fetchall()
    assert sorted(r["node_id"] for r in answer) == ids[1::2]


def test_the_count_sees_the_cut_taken_out(conn: Conn, graph) -> None:
    """The same body with the cut after readable_nodes in place of inside it
    reads every note: the count above would catch it."""
    ada, _ben, ids = graph
    conn.execute(
        """
        CREATE FUNCTION pg_temp.among_uncut(viewer bigint, ids bigint[])
        RETURNS TABLE (node_id bigint) LANGUAGE sql STABLE AS $$
            SELECT c.node_id FROM current_nodes(viewer) c
            WHERE c.node_id = ANY (ARRAY(SELECT DISTINCT unnest(ids)))
        $$
        """
    )
    uncut = _rows_read(
        conn,
        "SELECT node_id FROM pg_temp.among_uncut(%(viewer)s, %(ids)s::bigint[])",
        {"viewer": ada.id, "ids": ids},
    )
    assert uncut >= NOTES, uncut
