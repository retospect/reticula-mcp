"""readable_if_live(viewer, ids) (migration 0353) against readable_nodes, on
the generated graphs of tests/test_readable_hubs.py.

- On live nodes it is readable_nodes, exactly.
- On deleted notes (not hubs) it is what readable_nodes returns once those
  notes are no longer deleted: the reader could have read them.
A deleted hub is left out of the second comparison: readable_hubs knows live
hubs only, so undeleting a hub would also open what is filed in it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from reticula_mcp.db.pool import Conn
from tests.test_readable_hubs import World, _world_in_own_database, graph


def _seeds() -> list[int]:
    chosen = os.environ.get("READABLE_IF_LIVE_SEEDS")
    if chosen:
        return [int(s) for s in chosen.split(",")]
    return list(range(2000, 2120))


SEEDS = _seeds()


@pytest.fixture(scope="module")
def world(template_db: str) -> Iterator[World]:
    yield from _world_in_own_database(template_db)


def readable(conn: Conn, viewer: int, ids: list[int]) -> set[int]:
    rows = conn.execute(
        "SELECT node_id FROM readable_nodes(%s) WHERE node_id = ANY(%s)", (viewer, ids)
    ).fetchall()
    return {r["node_id"] for r in rows}


def if_live(conn: Conn, viewer: int, ids: list[int]) -> set[int]:
    rows = conn.execute(
        "SELECT node_id FROM readable_if_live(%s, %s)", (viewer, ids)
    ).fetchall()
    return {r["node_id"] for r in rows}


def test_the_graphs_have_deleted_notes_that_someone_could_read(world: World) -> None:
    """So that the comparison below of deleted notes is not empty."""
    told = 0
    for seed in SEEDS:
        with graph(world, seed) as g:
            dead = [i for i, n in g.nodes.items() if n.tomb and n.kind != "hub"]
            told += any(if_live(world.conn, v, dead) for v in world.viewers)
    assert told >= len(SEEDS) // 4, told


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_readable_if_live_is_readable_nodes_without_the_tombstone(
    world: World, seed: int
) -> None:
    conn = world.conn
    with graph(world, seed) as g:
        live = [i for i, n in g.nodes.items() if not n.tomb]
        dead = [i for i, n in g.nodes.items() if n.tomb and n.kind != "hub"]
        before = {v: if_live(conn, v, dead) for v in world.viewers}
        for v in world.viewers:
            assert if_live(conn, v, live) == readable(conn, v, live), (
                f"live nodes differ for viewer {v} (READABLE_IF_LIVE_SEEDS={seed})"
            )
        if not dead:
            return
        conn.execute("UPDATE node SET tombstoned_at = NULL WHERE id = ANY(%s)", (dead,))
        for v in world.viewers:
            assert before[v] == readable(conn, v, dead), (
                f"deleted notes differ for viewer {v} (READABLE_IF_LIVE_SEEDS={seed})"
            )
