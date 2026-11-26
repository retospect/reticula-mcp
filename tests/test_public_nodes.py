"""public_nodes() (migration 0354) on the generated graphs of
tests/test_readable_hubs.py, with public grants added to them: some by the
hub's author, some by someone else, some with mode none or add.

For every graph:
- everything it returns is written by the author of a hub that author made
  public, and that author may read it (readable_nodes of their account);
- nothing it returns is deleted, private, an instruction or debug note, or
  superseded by a live note;
- a hub's sub-hubs and notes come only through filings by their own author;
- no account's readable_nodes changes when the public grants are added.
"""

from __future__ import annotations

import os
import random
from collections.abc import Iterator

import pytest

from reticula_mcp.db.pool import Conn
from tests.test_readable_hubs import World, _world_in_own_database, graph


def _seeds() -> list[int]:
    chosen = os.environ.get("PUBLIC_NODES_SEEDS")
    if chosen:
        return [int(s) for s in chosen.split(",")]
    return list(range(3000, 3150))


SEEDS = _seeds()


@pytest.fixture(scope="module")
def world(template_db: str) -> Iterator[World]:
    yield from _world_in_own_database(template_db)


def readable(conn: Conn, viewers: list[int]) -> dict[int, set[int]]:
    return {
        v: {
            r["node_id"]
            for r in conn.execute(
                "SELECT node_id FROM readable_nodes(%s)", (v,)
            ).fetchall()
        }
        for v in viewers
    }


def add_public_grants(w: World, seed: int, hubs: list[int]) -> set[int]:
    """Grants to 'public' on some hubs; returns the hubs whose author gave read."""
    rnd = random.Random(seed)
    persons = w.people
    good: set[int] = set()
    for k, hub in enumerate(hubs):
        if rnd.random() > 0.5:
            continue
        author = w.conn.execute(
            "SELECT author FROM node WHERE id = %s", (hub,)
        ).fetchone()["author"]
        by = author if rnd.random() < 0.7 else rnd.choice(persons)
        mode = rnd.choice(["read", "read", "read", "none", "add"])
        w.conn.execute(
            "INSERT INTO hub_grant (hub_node_id, grantee, mode, granted_by, address,"
            " record_id) VALUES (%s, 'public', %s, %s, %s, %s)",
            (hub, mode, by, f"ret:{by}/pub{seed}-{k}", w.record_id),
        )
        if by == author and mode == "read":
            good.add(hub)
    return good


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_public_nodes_show_only_the_owners_own_live_notes(
    world: World, seed: int
) -> None:
    conn = world.conn
    with graph(world, seed) as g:
        hubs = [i for i, n in g.nodes.items() if n.kind == "hub"]
        before = readable(conn, world.viewers)
        add_public_grants(world, seed, hubs)
        after = readable(conn, world.viewers)
        assert before == after, f"a public grant changed what an account reads ({seed})"

        rows = conn.execute(
            "SELECT node_id, parent_hub_id FROM public_nodes()"
        ).fetchall()
        roots = {r["node_id"] for r in rows if r["parent_hub_id"] is None}
        # each root: its author granted it read
        for root in roots:
            ok = conn.execute(
                "SELECT 1 FROM hub_grant g JOIN node h ON h.id = g.hub_node_id"
                " WHERE g.hub_node_id = %s AND g.grantee = 'public' AND g.mode = 'read'"
                " AND g.granted_by = h.author",
                (root,),
            ).fetchone()
            assert ok, f"hub {root} shown without its owner's read grant ({seed})"
        # walk each row up to its root: one author all the way
        parent: dict[int, set[int]] = {}
        for r in rows:
            if r["parent_hub_id"] is not None:
                parent.setdefault(r["node_id"], set()).add(r["parent_hub_id"])
        info = {
            r["id"]: r
            for r in conn.execute(
                "SELECT n.id, n.author, n.kind, n.audience, n.tombstoned_at,"
                " EXISTS (SELECT 1 FROM supersedes s JOIN node w ON w.id = s.new_node_id"
                "   WHERE s.old_node_id = n.id AND w.tombstoned_at IS NULL) AS superseded"
                " FROM node n WHERE n.id = ANY(%s)",
                ([r["node_id"] for r in rows],),
            ).fetchall()
        }
        accounts = dict(
            (p, a)
            for a, p in (
                (aid, world.persons[h]) for h, aid in world.account_ids.items()
            )
        )
        for r in rows:
            n = info[r["node_id"]]
            assert n["tombstoned_at"] is None and n["audience"] == "hub", (seed, n)
            assert n["kind"] in ("hub", "note", "paper"), (seed, n)
            if n["kind"] != "hub":
                assert not n["superseded"], (seed, n)
            p = r["parent_hub_id"]
            if p is not None:
                assert info[p]["author"] == n["author"], (seed, n, "author differs")
                # the filing is the author's own, live, audience hub
                filed = conn.execute(
                    "SELECT 1 FROM membership m WHERE m.node_id = %s AND m.hub_node_id = %s"
                    " AND NOT m.withdrawn AND m.audience = 'hub' AND m.author = %s",
                    (n["id"], p, n["author"]),
                ).fetchone()
                assert filed, (seed, n, "no own filing")
            owner = accounts.get(n["author"])
            if owner is not None:
                assert n["id"] in after[owner], (seed, n, "owner cannot read it")


def test_the_generated_graphs_have_public_hubs_with_notes(world: World) -> None:
    """So that the comparison above is not empty."""
    with_notes = 0
    for seed in SEEDS:
        with graph(world, seed) as g:
            hubs = [i for i, n in g.nodes.items() if n.kind == "hub"]
            add_public_grants(world, seed, hubs)
            kinds = {
                r["kind"]
                for r in world.conn.execute(
                    "SELECT n.kind FROM public_nodes() p JOIN node n ON n.id = p.node_id"
                ).fetchall()
            }
            with_notes += bool(kinds - {"hub"})
    assert with_notes >= len(SEEDS) // 4, with_notes


@pytest.mark.parametrize("seed", SEEDS[:60], ids=lambda s: f"seed{s}")
def test_the_public_hubs_are_those_of_a_python_walk(world: World, seed: int) -> None:
    """The hubs of public_nodes() equal a walk written here from the graph:
    from each hub its live owner made public, down owner-filed, live,
    audience-hub edges to the owner's own live audience-hub sub-hubs."""
    with graph(world, seed) as g:
        hubs = [i for i, n in g.nodes.items() if n.kind == "hub"]
        good = add_public_grants(world, seed, hubs)
        roots = {
            h for h in good if not g.nodes[h].tomb and g.nodes[h].audience == "hub"
        }
        want: set[int] = set()
        for root in roots:
            owner = g.nodes[root].author
            todo, seen = [root], {root}
            while todo:
                at = todo.pop()
                for edge in g.edges:
                    c = g.nodes.get(edge.node)
                    if (
                        edge.hub == at
                        and c is not None
                        and c.kind == "hub"
                        and not edge.withdrawn
                        and edge.audience == "hub"
                        and edge.author == c.author == owner
                        and not c.tomb
                        and c.audience == "hub"
                        and c.id not in seen
                    ):
                        seen.add(c.id)
                        todo.append(c.id)
            want |= seen
        got = {
            r["node_id"]
            for r in world.conn.execute(
                "SELECT p.node_id FROM public_nodes() p JOIN node n ON n.id = p.node_id"
                " WHERE n.kind = 'hub'"
            ).fetchall()
        }
        assert got == want, (seed, sorted(got - want), sorted(want - got))
