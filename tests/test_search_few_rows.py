"""The statements of ``get`` and ``search`` that ask who may read a few notes
read the rows of those notes only (``permissions.readable_among_sql`` and
``current_among_sql``), not every note the viewer may read.

Each such statement of a call is run again under ``EXPLAIN ANALYZE`` with
its own parameters, and the rows its plan reads from ``node`` and
``membership`` are counted. The gain rests on the planner taking the
condition into each branch of ``readable_nodes``; the count fails when it
stops doing so, or when a statement goes back to asking about every note.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

from reticula_mcp import permissions
from reticula_mcp.db.pool import Conn
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import handlers
from tests.graph_support import mk_note
from tests.helpers import make_atom
from tests.search_support import file_under, grant, hub_of, supersede, use_hash_embedder

NOTES = 400  # in the hub, besides the notes of the case
DOI = "10.5555/few.rows"
ASKS = ("readable_nodes(", "current_nodes(", "current_among(")


def _rows_read(conn: Conn, statement: str, params: Any) -> int:
    """As in tests/test_current_among_cost.py: sequential scans and hash or
    merge joins off, so that a few hundred notes plan as 30,000 do."""
    with conn.transaction():
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


@pytest.fixture
def case(conn, make_account, monkeypatch):
    use_hash_embedder(monkeypatch)
    ada, ben = make_account(), make_account()
    hub = hub_of(conn, ada, "Greenhouse")
    grant(conn, hub, ben, "read")
    for i in range(NOTES):
        file_under(conn, make_atom(conn, ada, f"sensor log {i}", title=f"Log {i}"), hub)
    centre = make_atom(conn, ada, "The gateway reboots nightly.", title="Gateway")
    file_under(conn, centre, hub)
    others = [
        make_atom(conn, ada, f"Gateway note {i}.", title=f"G {i}") for i in range(8)
    ]
    for o in others:
        file_under(conn, o, hub)
    for o in others[:3]:
        graph.link(conn, ada, ref=centre["address"], to=o["address"], rel="see-also")
    for o in others[3:6]:
        graph.link(conn, ada, ref=o["address"], to=centre["address"], rel="explains")
    supersede(conn, centre, others[6])
    supersede(conn, others[7], centre)
    paper = mk_note(conn, ada, "abstract", kind="paper", title="A paper", ext_id=DOI)
    about = [
        mk_note(conn, ada, f"note {i} on it", ext_id=DOI, hub=hub["address"])
        for i in range(4)
    ]
    conn.execute("UPDATE node SET kind = 'logbook' WHERE address = %s", (about[0],))
    for table in ("node", "membership", "link", "supersedes"):
        conn.execute(f"ANALYZE {table}")
    return ada, ben, centre, paper, about


def _asked(conn, monkeypatch, call) -> list[tuple[str, Any]]:
    """The statements of ``call`` that ask who may read, with their params."""
    seen: list[tuple[str, Any]] = []
    real = psycopg.Cursor.execute

    def spy(self, query, params=None, **kw):
        text = query if isinstance(query, str) else str(query)
        if any(a in text for a in ASKS) and not text.startswith("EXPLAIN"):
            seen.append((text, params))
        return real(self, query, params, **kw)

    monkeypatch.setattr(psycopg.Cursor, "execute", spy)
    try:
        with permissions.read_call():
            call()
    finally:
        monkeypatch.setattr(psycopg.Cursor, "execute", real)
    return seen


def _bounded(conn, asked: list[tuple[str, Any]], bound: int) -> None:
    assert asked
    for statement, params in asked:
        rows = _rows_read(conn, statement, params)
        assert rows <= bound, (rows, " ".join(statement.split())[:200])


def test_get_of_a_note_with_links_and_supersession(conn, monkeypatch, case):
    ada, ben, centre, _paper, _about = case
    for viewer in (ada, ben):
        for history in (False, True):
            asked = _asked(
                conn,
                monkeypatch,
                lambda v=viewer, h=history: handlers.get(
                    conn, v, ref=centre["address"], history=h
                ),
            )
            assert len(asked) >= 5  # resolve, and the four lists of _chain_lines
            _bounded(conn, asked, 60)


def test_get_of_a_paper_and_of_a_note_about_it(conn, monkeypatch, case):
    ada, _ben, _centre, paper, about = case
    for ref in (paper, about[1]):
        _bounded(
            conn,
            _asked(conn, monkeypatch, lambda r=ref: handlers.get(conn, ada, ref=r)),
            60,
        )


def test_a_kind_of_its_own_is_checked_on_its_notes_only(conn, monkeypatch, case):
    ada, *_ = case
    asked = _asked(
        conn, monkeypatch, lambda: handlers.search(conn, ada, query="", kind="logbook")
    )
    checks = [
        a for a in asked if a[0].startswith("SELECT 1 AS x FROM node n WHERE n.kind")
    ]
    assert len(checks) == 1
    _bounded(conn, checks, 20)


def test_the_member_counts_read_the_members_only(conn, monkeypatch, case):
    """A hub listing counts the members of each hub: their rows, not every
    note the viewer may read a second time."""
    ada, *_ = case
    asked = _asked(
        conn, monkeypatch, lambda: handlers.search(conn, ada, query="", kind="hub")
    )
    counts = [a for a in asked if "count(DISTINCT n.id) FILTER" in a[0]]
    assert len(counts) == 1
    members = NOTES + 9 + 4  # the logs, centre and others, the notes on the paper
    # each member: its membership row, its node row, and the few rows of the
    # read check; a pass over the readable notes on top would be 2 * NOTES more
    _bounded(conn, counts, 6 * members)
    statement, params = counts[0]
    whole = statement.replace(
        permissions.current_among_sql(
            "SELECT m2.node_id FROM membership m2 WHERE m2.hub_node_id = ANY(%(hubs)s)"
        ),
        permissions.current_nodes_sql(),
    )
    assert whole != statement
    assert _rows_read(conn, whole, params) > _rows_read(conn, statement, params)
