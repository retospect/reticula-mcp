"""``get`` of a deleted note tells only those who could have read it that it was
withdrawn. ``read._withdrawn`` asks ``readable_if_live`` (migration 0353) for
that; before, it carried its own copy of the rule. The statement of before is
kept here as text and compared with the function on the generated graphs of
``tests/test_readable_hubs.py``: every viewer and an unknown one, every
deleted node, by address and by short id."""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from reticula_mcp.db.pool import Conn
from reticula_mcp.resolve import ends_with_params, ends_with_sql
from reticula_mcp.search import read
from tests.test_readable_hubs import World, _world_in_own_database, graph

# _withdrawn's condition before readable_if_live, word for word but for the
# list of hubs, which it took as a parameter (readable_hub_ids)
OLD = """
        SELECT n.id, n.address FROM node n
        WHERE n.tombstoned_at IS NOT NULL AND {cond}
          AND (n.author = %(person)s
               OR (n.audience = 'hub' AND EXISTS (
                    SELECT 1 FROM membership m
                    WHERE m.node_id = n.id AND NOT m.withdrawn
                      AND m.audience = 'hub' AND m.author = n.author
                      AND m.hub_node_id = ANY(%(hubs)s::bigint[]))))
        LIMIT 2
"""


def _seeds() -> list[int]:
    chosen = os.environ.get("WITHDRAWN_SEEDS")
    if chosen:
        return [int(s) for s in chosen.split(",")]
    return list(range(3000, 3120))


SEEDS = _seeds()


@pytest.fixture(scope="module")
def world(template_db: str) -> Iterator[World]:
    yield from _world_in_own_database(template_db)


def reader(conn: Conn, viewer: int) -> SimpleNamespace:
    """The viewer as _withdrawn reads it: account id and person id."""
    row = conn.execute(
        "SELECT person_id FROM account WHERE id = %s", (viewer,)
    ).fetchone()
    return SimpleNamespace(id=viewer, person_id=row["person_id"] if row else None)


def old_withdrawn(conn: Conn, viewer: int, ref: str) -> dict[str, Any] | None:
    person = conn.execute(
        "SELECT person_id FROM account WHERE id = %s", (viewer,)
    ).fetchone()
    hubs = [
        r["hub_id"]
        for r in conn.execute("SELECT hub_id FROM readable_hubs(%s)", (viewer,))
    ]
    tail: dict[str, Any] = {}
    if ref.startswith("ret:"):
        cond = "n.address = %(ref)s"
    else:
        cond = ends_with_sql("n.address")
        tail = ends_with_params(ref)
    rows = conn.execute(
        OLD.format(cond=cond),
        {
            **tail,
            "ref": ref,
            "person": person["person_id"] if person else None,
            "hubs": hubs,
        },
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_withdrawn_tells_whom_the_rule_of_before_told(world: World, seed: int) -> None:
    conn = world.conn
    with graph(world, seed):
        gone = conn.execute(
            "SELECT address FROM node WHERE tombstoned_at IS NOT NULL"
        ).fetchall()
        live = conn.execute(
            "SELECT address FROM node WHERE tombstoned_at IS NULL LIMIT 3"
        ).fetchall()
        # the generator's addresses are not hex, so they are compared whole;
        # a short id goes through the same statement (tests/test_resolve_ref_cost.py
        # reads a withdrawn note by short id)
        refs = [r["address"] for r in gone]
        refs += [r["address"] for r in live]  # a live node is never "withdrawn"
        for viewer in world.viewers:
            who = reader(conn, viewer)
            for ref in refs:
                want = old_withdrawn(conn, viewer, ref)
                got = read._withdrawn(conn, who, ref)
                assert got == want, (seed, viewer, ref)


def test_the_comparison_sees_both_answers(world: World) -> None:
    """Over the graphs, the function tells somebody and refuses somebody: a
    comparison that only ever met None would prove nothing."""
    conn = world.conn
    told = refused = 0
    for seed in SEEDS[:40]:
        with graph(world, seed):
            gone = [
                r["address"]
                for r in conn.execute(
                    "SELECT address FROM node WHERE tombstoned_at IS NOT NULL"
                ).fetchall()
            ]
            for viewer in world.viewers:
                for ref in gone:
                    if read._withdrawn(conn, reader(conn, viewer), ref):
                        told += 1
                    else:
                        refused += 1
    assert told >= 20 and refused >= 20, (told, refused)
