"""Gate for splitting ``reach`` out of ``readable_nodes`` into ``readable_hubs``.

``readable_nodes(viewer)`` is the one place that decides which notes an account
may read. A later migration takes the recursive part ``reach`` out into
``readable_hubs(viewer)`` and builds ``readable_nodes`` on top of it. That must
not change who can read what, for anyone, in any graph.

This file is written from the definition in migration 0200 alone. It keeps that
definition as SQL text (``READ_REACH`` and ``READ_FINAL`` below), installs it
under another name inside the test's transaction, and compares the function
under test with it on many random graphs, for every account as viewer.

* ``test_readable_nodes_unchanged[seedN]`` and
  ``test_readable_hubs_unchanged[seedN]`` check one graph each. A failure names
  the seed. Replay one graph with ``-k seedN``, or a list of seeds with the
  environment variable ``READABLE_HUBS_SEEDS=12,40``.
* ``test_wrong_variant_is_told_apart`` installs wrong variants of the rule and
  asserts that the same comparison finds each of them on at least one graph.
  A comparison that cannot tell a wrong rule from the real one is no gate.
* ``test_generator_covers_every_shape`` asserts that the generator keeps
  producing every shape it is meant to produce.
"""

from __future__ import annotations

import itertools
import os
import random
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reticula_mcp.db import connect
from reticula_mcp.db.pool import Conn

# ---------------------------------------------------------------------------
# The rule, as defined in 0200_graph_access.sql, lines 27 to 62, word for word.
# READ_REACH is the WITH RECURSIVE part, READ_FINAL the three branches after it.
# ---------------------------------------------------------------------------

READ_REACH = """\
    WITH RECURSIVE me AS (
        SELECT a.person_id FROM account a WHERE a.id = viewer
    ),
    reach(hub_id) AS (
        SELECT h.id
        FROM node h JOIN me ON h.author = me.person_id
        WHERE h.kind = 'hub' AND h.tombstoned_at IS NULL
      UNION
        SELECT h.id
        FROM hub_grant g
        JOIN me ON g.grantee = me.person_id
        JOIN node h ON h.id = g.hub_node_id
        WHERE g.mode IN ('read', 'add')
          AND h.kind = 'hub' AND h.tombstoned_at IS NULL AND h.audience = 'hub'
      UNION
        SELECT c.id
        FROM reach r
        JOIN membership m ON m.hub_node_id = r.hub_id
        JOIN node c ON c.id = m.node_id
        WHERE NOT m.withdrawn AND m.audience = 'hub' AND m.author = c.author
          AND c.kind = 'hub' AND c.tombstoned_at IS NULL AND c.audience = 'hub'
    )
"""

READ_FINAL = """\
    SELECT n.id
    FROM node n JOIN me ON n.author = me.person_id
    WHERE n.tombstoned_at IS NULL
  UNION
    SELECT n.id
    FROM reach r JOIN node n ON n.id = r.hub_id
    WHERE n.tombstoned_at IS NULL AND n.audience = 'hub'
  UNION
    SELECT n.id
    FROM reach r
    JOIN membership m ON m.hub_node_id = r.hub_id
    JOIN node n ON n.id = m.node_id
    WHERE NOT m.withdrawn AND m.audience = 'hub' AND m.author = n.author
      AND n.tombstoned_at IS NULL AND n.audience = 'hub'"""

MIGRATION = (
    Path(__file__).parent.parent
    / "src"
    / "reticula_mcp"
    / "db"
    / "migrations"
    / "0200_graph_access.sql"
)

OLD_NODES = "tmp_old_readable_nodes"
OLD_REACH = "tmp_old_reach"


def test_the_kept_text_is_the_text_of_0200() -> None:
    text = MIGRATION.read_text()
    assert READ_REACH + READ_FINAL in text


# ---------------------------------------------------------------------------
# Wrong variants of the rule. Each is a list of (old, new) text edits, applied
# to the reach part and to the final part. Every ``old`` must occur exactly once
# in its part, so that a changed rule text fails here and not silently.
# ---------------------------------------------------------------------------

Edit = tuple[str, str]


class Variant(NamedTuple):
    reach: tuple[Edit, ...] = ()
    final: tuple[Edit, ...] = ()


def _depth_limited(levels: int) -> Variant:
    """Recursion cut to ``levels`` steps below the first two branches."""
    return Variant(
        reach=(
            ("reach(hub_id) AS (", "reach(hub_id, d) AS ("),
            (
                "SELECT h.id\n        FROM node h JOIN me",
                "SELECT h.id, 0\n        FROM node h JOIN me",
            ),
            (
                "SELECT h.id\n        FROM hub_grant g",
                "SELECT h.id, 0\n        FROM hub_grant g",
            ),
            (
                "SELECT c.id\n        FROM reach r",
                "SELECT c.id, r.d + 1\n        FROM reach r",
            ),
            (
                "AND c.audience = 'hub'\n    )",
                f"AND c.audience = 'hub' AND r.d < {levels}\n    )",
            ),
        )
    )


_MEMBER_REACH = "WHERE NOT m.withdrawn AND m.audience = 'hub' AND m.author = c.author"
_MEMBER_FINAL = "WHERE NOT m.withdrawn AND m.audience = 'hub' AND m.author = n.author"
_C_TESTS = "AND c.kind = 'hub' AND c.tombstoned_at IS NULL AND c.audience = 'hub'"
_GRANT_TESTS = "AND h.kind = 'hub' AND h.tombstoned_at IS NULL AND h.audience = 'hub'"
_OWN_HUB = "WHERE h.kind = 'hub' AND h.tombstoned_at IS NULL"
_OWN_NOTE = (
    "FROM node n JOIN me ON n.author = me.person_id\n    WHERE n.tombstoned_at IS NULL"
)
_HUB_READABLE = "WHERE n.tombstoned_at IS NULL AND n.audience = 'hub'\n  UNION"
_N_TESTS = "AND n.tombstoned_at IS NULL AND n.audience = 'hub'"

VARIANTS: dict[str, Variant] = {
    # Who filed it, whether it was withdrawn, audience of the filing.
    "author_dropped_in_reach": Variant(
        reach=((_MEMBER_REACH, "WHERE NOT m.withdrawn AND m.audience = 'hub'"),)
    ),
    "author_dropped_in_final": Variant(
        final=((_MEMBER_FINAL, "WHERE NOT m.withdrawn AND m.audience = 'hub'"),)
    ),
    "withdrawn_dropped_in_reach": Variant(
        reach=((_MEMBER_REACH, "WHERE m.audience = 'hub' AND m.author = c.author"),)
    ),
    "withdrawn_dropped_in_final": Variant(
        final=((_MEMBER_FINAL, "WHERE m.audience = 'hub' AND m.author = n.author"),)
    ),
    "filing_audience_dropped_in_reach": Variant(
        reach=((_MEMBER_REACH, "WHERE NOT m.withdrawn AND m.author = c.author"),)
    ),
    "filing_audience_dropped_in_final": Variant(
        final=((_MEMBER_FINAL, "WHERE NOT m.withdrawn AND m.author = n.author"),)
    ),
    # audience = 'hub' on the member, on the granted hub, on a hub that is read.
    "member_audience_dropped_in_reach": Variant(
        reach=((_C_TESTS, "AND c.kind = 'hub' AND c.tombstoned_at IS NULL"),)
    ),
    "member_audience_dropped_in_final": Variant(
        final=((_N_TESTS, "AND n.tombstoned_at IS NULL"),)
    ),
    "granted_hub_audience_dropped": Variant(
        reach=((_GRANT_TESTS, "AND h.kind = 'hub' AND h.tombstoned_at IS NULL"),)
    ),
    "own_hub_needs_audience_hub": Variant(
        reach=((_OWN_HUB, _OWN_HUB + " AND h.audience = 'hub'"),)
    ),
    # Tombstone test dropped on a hub (three places) and on a note (two places).
    "tombstone_dropped_on_own_hub": Variant(
        reach=((_OWN_HUB, "WHERE h.kind = 'hub'"),)
    ),
    "tombstone_dropped_on_granted_hub": Variant(
        reach=((_GRANT_TESTS, "AND h.kind = 'hub' AND h.audience = 'hub'"),)
    ),
    "tombstone_dropped_on_member_hub": Variant(
        reach=((_C_TESTS, "AND c.kind = 'hub' AND c.audience = 'hub'"),)
    ),
    "tombstone_dropped_on_own_note": Variant(
        final=((_OWN_NOTE, "FROM node n JOIN me ON n.author = me.person_id"),)
    ),
    "tombstone_dropped_on_member_note": Variant(
        final=((_N_TESTS, "AND n.audience = 'hub'"),)
    ),
    # The kind test: a note is not a hub.
    "kind_dropped_on_own_hub": Variant(
        reach=((_OWN_HUB, "WHERE h.tombstoned_at IS NULL"),)
    ),
    "kind_dropped_on_granted_hub": Variant(
        reach=((_GRANT_TESTS, "AND h.tombstoned_at IS NULL AND h.audience = 'hub'"),)
    ),
    "kind_dropped_on_member_hub": Variant(
        reach=((_C_TESTS, "AND c.tombstoned_at IS NULL AND c.audience = 'hub'"),)
    ),
    # Grant modes.
    "mode_none_accepted": Variant(
        reach=(("g.mode IN ('read', 'add')", "g.mode IN ('read', 'add', 'none')"),)
    ),
    "mode_add_ignored": Variant(
        reach=(("g.mode IN ('read', 'add')", "g.mode IN ('read')"),)
    ),
    "mode_read_ignored": Variant(
        reach=(("g.mode IN ('read', 'add')", "g.mode IN ('add')"),)
    ),
    # Recursion depth.
    "recursion_cut_to_one_level": _depth_limited(1),
    "recursion_cut_to_two_levels": _depth_limited(2),
    "recursion_cut_to_three_levels": _depth_limited(3),
    # Branches of the final union.
    "own_notes_branch_dropped": Variant(
        final=(
            (
                "    SELECT n.id\n    FROM node n JOIN me ON n.author = me.person_id\n"
                "    WHERE n.tombstoned_at IS NULL\n  UNION\n",
                "",
            ),
        )
    ),
    "readable_hubs_branch_dropped": Variant(
        final=(
            (
                "    SELECT n.id\n    FROM reach r JOIN node n ON n.id = r.hub_id\n"
                "    WHERE n.tombstoned_at IS NULL AND n.audience = 'hub'\n  UNION\n",
                "",
            ),
        )
    ),
    "member_branch_dropped": Variant(
        final=(
            (
                "\n  UNION\n    SELECT n.id\n    FROM reach r\n    JOIN membership m"
                " ON m.hub_node_id = r.hub_id\n    JOIN node n ON n.id = m.node_id\n"
                "    WHERE NOT m.withdrawn AND m.audience = 'hub'"
                " AND m.author = n.author\n"
                "      AND n.tombstoned_at IS NULL AND n.audience = 'hub'",
                "",
            ),
        )
    ),
}


# Two tests of the rule that no graph can show. A hub in ``reach`` has passed a
# tombstone test at its entry. It is either the viewer's own (then the first
# branch of the final union returns it, whatever its audience) or it has
# audience 'hub' from the grant or the filing test. So the audience and the
# tombstone test in the second branch of the final union are redundant, and a
# rule without them returns the same set on every graph. These variants are
# kept to show that: no graph tells them apart, and the reason is the rule.
REDUNDANT: dict[str, Variant] = {
    "readable_hub_audience_dropped": Variant(
        final=((_HUB_READABLE, "WHERE n.tombstoned_at IS NULL\n  UNION"),)
    ),
    "tombstone_dropped_on_readable_hub": Variant(
        final=((_HUB_READABLE, "WHERE n.audience = 'hub'\n  UNION"),)
    ),
}
ALL_VARIANTS = {**VARIANTS, **REDUNDANT}


def _apply(text: str, edits: tuple[Edit, ...]) -> str:
    for old, new in edits:
        assert text.count(old) == 1, f"edit does not apply exactly once: {old!r}"
        text = text.replace(old, new)
    return text


def _variant_texts(variant: Variant) -> tuple[str, str]:
    return _apply(READ_REACH, variant.reach), _apply(READ_FINAL, variant.final)


def _install_function(conn: Conn, name: str, column: str, body: str) -> None:
    """Install ``body`` as a function under ``name`` (constant names only)."""
    conn.execute(
        f"CREATE FUNCTION {name}(viewer bigint) RETURNS TABLE ({column} bigint)"
        f" LANGUAGE sql STABLE AS $body$\n{body}\n$body$"
    )


def _nodes_name(variant: str) -> str:
    return f"tmp_nodes_{variant}"


def _reach_name(variant: str) -> str:
    return f"tmp_reach_{variant}"


# ---------------------------------------------------------------------------
# The world: four accounts, one stored record every row can point at, and the
# installed functions. One transaction for the whole module, rolled back at the
# end; each graph lives in a savepoint of it.
# ---------------------------------------------------------------------------

HANDLES = ("ada", "ben", "carol", "dave")
FOREIGN = "pforeign"  # a person with no account on this instance
NO_SUCH_ACCOUNT = -1


@dataclass
class World:
    conn: Conn
    record_id: int
    account_ids: dict[str, int]  # handle -> account id
    persons: dict[str, str]  # handle -> person id

    @property
    def viewers(self) -> list[int]:
        return [*self.account_ids.values(), NO_SUCH_ACCOUNT]

    @property
    def people(self) -> list[str]:
        return [*self.persons.values(), FOREIGN]


# Variants whose reach part differs from the real rule's.
REACH_VARIANTS = tuple(
    name for name, v in ALL_VARIANTS.items() if _variant_texts(v)[0] != READ_REACH
)


@contextmanager
def _open_world(url: str) -> Iterator[World]:
    conn = connect(url, options="-c statement_timeout=60s")
    try:
        rec = conn.execute(
            "INSERT INTO record (version_hash, address, type, author, key_id,"
            " created_at, audience, content_hash, format_version, envelope, signature)"
            " VALUES ('v-readable-hubs', 'ret:pstub/readable-hubs', 'atom', 'pstub',"
            " 'k1', now(), 'hub', 'c', 0, '\\x'::bytea, '\\x'::bytea) RETURNING id"
        ).fetchone()
        assert rec is not None
        account_ids: dict[str, int] = {}
        persons: dict[str, str] = {}
        for handle in HANDLES:
            person = f"p{handle}"
            row = conn.execute(
                "INSERT INTO account (person_id, email, password_hash, handle,"
                " identity_address) VALUES (%s, %s, '!', %s, %s) RETURNING id",
                (
                    person,
                    f"{handle}@example.com",
                    handle,
                    f"ret:{person}/identity",
                ),
            ).fetchone()
            assert row is not None
            account_ids[handle] = row["id"]
            persons[handle] = person
        w = World(conn, rec["id"], account_ids, persons)
        _install_function(conn, OLD_NODES, "node_id", READ_REACH + READ_FINAL)
        _install_function(
            conn, OLD_REACH, "hub_id", READ_REACH + "    SELECT hub_id FROM reach"
        )
        for name, variant in ALL_VARIANTS.items():
            reach, final = _variant_texts(variant)
            _install_function(conn, _nodes_name(name), "node_id", reach + final)
            if name in REACH_VARIANTS:
                _install_function(
                    conn,
                    _reach_name(name),
                    "hub_id",
                    reach + "    SELECT hub_id FROM reach",
                )
        yield w
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def _world_in_own_database(template_db: str) -> Iterator[World]:
    """A world in a private clone of the migrated template. Graphs that are
    rolled back leave dead rows behind (autovacuum is off in test databases),
    which slow every later statement on the same tables. Each part of this file
    that builds many graphs has its own clone, so that it starts clean."""
    admin = os.environ["RETICULA_TEST_DATABASE_URL"]
    name = f"reticula_rh_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template_db)
            )
        )
    try:
        with _open_world(make_conninfo(admin, dbname=name)) as w:
            yield w
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def world(template_db: str) -> Iterator[World]:
    """For the checks of single graphs."""
    yield from _world_in_own_database(template_db)


@pytest.fixture(scope="module")
def survey_world(template_db: str) -> Iterator[World]:
    """For the pass over all graphs."""
    yield from _world_in_own_database(template_db)


# ---------------------------------------------------------------------------
# The graph generator.
# ---------------------------------------------------------------------------


class N(NamedTuple):
    id: int
    author: str
    kind: str
    audience: str
    tomb: bool


class E(NamedTuple):
    node: int
    hub: int
    author: str
    audience: str
    withdrawn: bool


class G(NamedTuple):
    hub: int
    grantee: str
    mode: str


@dataclass
class Graph:
    seed: int
    nodes: dict[int, N]
    edges: list[E]
    grants: list[G]
    shapes: Counter[str]


def _clean(nodes: dict[int, N], e: E) -> bool:
    """A filing of a hub under a hub that the rule follows."""
    child, parent = nodes[e.node], nodes[e.hub]
    return (
        not e.withdrawn
        and e.audience == "hub"
        and e.author == child.author
        and child.kind == "hub"
        and not child.tomb
        and child.audience == "hub"
        and parent.kind == "hub"
        and not parent.tomb
        and parent.audience == "hub"
    )


def _longest_chain(nodes: dict[int, N], edges: list[E]) -> int:
    """Most hubs on one path of clean filings, a cycle cut where it closes."""
    parents: dict[int, set[int]] = {}
    for e in edges:
        if _clean(nodes, e):
            parents.setdefault(e.node, set()).add(e.hub)

    def up(hub: int, seen: frozenset[int]) -> int:
        best = 1
        for p in parents.get(hub, ()):
            if p not in seen:
                best = max(best, 1 + up(p, seen | {p}))
        return best

    return max((up(h, frozenset({h})) for h in parents), default=0)


def _has_cycle(nodes: dict[int, N], edges: list[E]) -> bool:
    parents: dict[int, set[int]] = {}
    for e in edges:
        if _clean(nodes, e):
            parents.setdefault(e.node, set()).add(e.hub)

    def reaches(start: int, target: int, seen: set[int]) -> bool:
        for p in parents.get(start, ()):
            if p == target:
                return True
            if p not in seen:
                seen.add(p)
                if reaches(p, target, seen):
                    return True
        return False

    return any(reaches(h, h, set()) for h in parents)


def _shapes(nodes: dict[int, N], edges: list[E], grants: list[G]) -> Counter[str]:
    c: Counter[str] = Counter()
    hubs = {i for i, n in nodes.items() if n.kind == "hub"}
    filed = {e.node for e in edges}
    parent_of = {e.hub for e in edges}
    chain = _longest_chain(nodes, edges)
    for floor in (3, 4, 5):
        if chain >= floor:
            c[f"chain_of_{floor}_hubs"] += 1
    if _has_cycle(nodes, edges):
        c["cycle"] += 1
    for n in nodes.values():
        if n.kind == "hub" and n.audience == "private":
            c["private_hub"] += 1
        if n.kind != "hub" and n.audience == "private":
            c["private_note"] += 1
        if n.audience not in ("hub", "private"):
            c["unknown_audience"] += 1
        if n.kind == "hub" and n.tomb:
            c["tombstoned_hub"] += 1
            if n.id in filed and n.id in parent_of:
                c["tombstoned_hub_in_a_chain"] += 1
        if n.kind != "hub" and n.tomb:
            c["tombstoned_note"] += 1
        if n.author == FOREIGN:
            c["author_without_account"] += 1
    under: Counter[int] = Counter()
    for e in edges:
        child, parent = nodes[e.node], nodes[e.hub]
        if e.withdrawn:
            c["withdrawn_filing"] += 1
        if e.audience != "hub":
            c["private_filing"] += 1
        if e.author != child.author:
            c[
                "note_filed_by_other" if child.kind != "hub" else "hub_filed_by_other"
            ] += 1
        if parent.kind != "hub":
            c["filed_under_a_note"] += 1
        if child.kind == "hub" and parent.kind == "hub" and not e.withdrawn:
            under[e.node] += 1
    # distinct parents, not duplicate rows
    two = {
        i
        for i in under
        if len({e.hub for e in edges if e.node == i and nodes[e.hub].kind == "hub"})
        >= 2
    }
    c["hub_under_two_hubs"] += len(two)
    for g in grants:
        hub = nodes[g.hub]
        place = "sub" if g.hub in filed else "top"
        if hub.kind == "hub":
            c[f"grant_{g.mode}_on_{place}_hub"] += 1
        else:
            c["grant_on_a_note"] += 1
        if hub.audience == "private":
            c["grant_on_private_hub"] += 1
        if hub.tomb:
            c["grant_on_tombstoned_hub"] += 1
    c["hubs"] += len(hubs)
    return c


def _build(w: World, seed: int) -> Graph:
    rng = random.Random(seed)
    conn = w.conn
    people = w.people
    weights = [1.0] * len(w.persons) + [0.5]

    def person() -> str:
        return rng.choices(people, weights)[0]

    def other(p: str) -> str:
        return rng.choice([x for x in people if x != p])

    n_hubs = rng.randint(6, 16)
    n_notes = rng.randint(8, 36)
    ids = [
        r["i"]
        for r in conn.execute(
            "SELECT nextval(pg_get_serial_sequence('node', 'id')) AS i"
            " FROM generate_series(1, %s)",
            (n_hubs + n_notes,),
        )
    ]
    nodes: dict[int, N] = {}
    for i in ids[:n_hubs]:
        aud = rng.choices(["hub", "private", "team"], [8, 2, 0.4])[0]
        nodes[i] = N(i, person(), "hub", aud, rng.random() < 0.12)
    for i in ids[n_hubs:]:
        kind = rng.choices(["note", "paper"], [9, 1])[0]
        aud = rng.choices(["hub", "private", "team"], [7, 2.5, 0.3])[0]
        nodes[i] = N(i, person(), kind, aud, rng.random() < 0.1)
    hubs = ids[:n_hubs]
    notes = ids[n_hubs:]

    edges: list[E] = []

    def file(child: int, parent: int, flaws: float = 1.0) -> None:
        c = nodes[child]
        author = other(c.author) if rng.random() < 0.1 * flaws else c.author
        aud = rng.choice(["private", "team"]) if rng.random() < 0.08 * flaws else "hub"
        withdrawn = rng.random() < 0.08 * flaws
        edges.append(E(child, parent, author, aud, withdrawn))
        if rng.random() < 0.08:  # filed again later, with other flags
            edges.append(
                E(
                    child,
                    parent,
                    c.author,
                    "hub",
                    rng.random() < 0.5,
                )
            )

    # Chains of three to six hubs: each filed under the one before.
    chains: list[list[int]] = []
    for _ in range(rng.randint(1, 3)):
        chain = rng.sample(hubs, min(rng.randint(3, 6), len(hubs)))
        for h in chain:
            if rng.random() < 0.8:
                nodes[h] = nodes[h]._replace(audience="hub", tomb=False)
        if len(chain) >= 3 and rng.random() < 0.3:
            mid = chain[rng.randint(1, len(chain) - 2)]
            nodes[mid] = nodes[mid]._replace(audience="hub", tomb=True)
        for a, b in itertools.pairwise(chain):
            file(b, a, 0.5)
        if rng.random() < 0.2:  # a cycle through the whole chain
            file(chain[0], chain[-1], 0.3)
        chains.append(chain)
    if rng.random() < 0.1:  # two hubs under each other
        a, b = rng.sample(hubs, 2)
        file(a, b, 0.3)
        file(b, a, 0.3)

    # More filings of hubs: parents come earlier in a random order, so no cycles.
    order = rng.sample(hubs, len(hubs))
    for pos, h in enumerate(order):
        if pos and rng.random() < 0.35:
            file(h, rng.choice(order[:pos]))
        if pos and rng.random() < 0.15:  # a second parent
            file(h, rng.choice(order[:pos]))
        if notes and rng.random() < 0.05:  # a hub filed under a note
            file(h, rng.choice(notes))
    for n in notes:
        for _ in range(rng.choices([0, 1, 2, 3], [2, 5, 2, 1])[0]):
            if rng.random() < 0.08:
                file(n, rng.choice([x for x in notes if x != n] or hubs))
            else:
                file(n, rng.choice(hubs))

    # Grants: on tops and sub-hubs of chains, on private hubs, anywhere.
    grants: dict[tuple[int, str], str] = {}
    account_people = list(w.persons.values())

    def grant(target: int, grantee: str, mode: str) -> None:
        grants[(target, grantee)] = mode

    for chain in chains:
        if rng.random() < 0.6:
            who = rng.choice(account_people)
            grant(chain[0], who, rng.choice(["read", "add", "read", "none"]))
            if rng.random() < 0.6:
                sub = chain[rng.randint(1, len(chain) - 1)]
                grant(sub, who, rng.choice(["read", "add", "none", "none"]))
    private_hubs = [h for h in hubs if nodes[h].audience == "private"]
    for _ in range(rng.randint(0, 5)):
        if private_hubs and rng.random() < 0.25:
            target = rng.choice(private_hubs)
        elif notes and rng.random() < 0.1:
            target = rng.choice(notes)
        else:
            target = rng.choice(hubs)
        who = rng.choice(account_people) if rng.random() < 0.9 else FOREIGN
        grant(target, who, rng.choices(["read", "add", "none"], [3, 3, 2])[0])
    grant_rows = [G(t, g, m) for (t, g), m in grants.items()]

    now_or_null = "CASE WHEN %s THEN now() END"
    conn.execute(
        "INSERT INTO node (id, address, author, kind, head_record_id, head_version,"
        " audience, tombstoned_at) VALUES "
        + ",".join(f"(%s,%s,%s,%s,%s,%s,%s,{now_or_null})" for _ in nodes),
        [
            v
            for n in nodes.values()
            for v in (
                n.id,
                f"ret:{n.author}/n{n.id}",
                n.author,
                n.kind,
                w.record_id,
                f"h{n.id}",
                n.audience,
                n.tomb,
            )
        ],
    )
    if edges:
        conn.execute(
            "INSERT INTO membership (address, node_id, hub_node_id, author, audience,"
            " withdrawn, record_id) VALUES "
            + ",".join("(%s,%s,%s,%s,%s,%s,%s)" for _ in edges),
            [
                v
                for k, e in enumerate(edges)
                for v in (
                    f"ret:{e.author}/m{seed}-{k}",
                    e.node,
                    e.hub,
                    e.author,
                    e.audience,
                    e.withdrawn,
                    w.record_id,
                )
            ],
        )
    if grant_rows:
        conn.execute(
            "INSERT INTO hub_grant (hub_node_id, grantee, mode, granted_by, address,"
            " record_id) VALUES " + ",".join("(%s,%s,%s,%s,%s,%s)" for _ in grant_rows),
            [
                v
                for k, g in enumerate(grant_rows)
                for v in (
                    g.hub,
                    g.grantee,
                    g.mode,
                    nodes[g.hub].author,
                    f"ret:{nodes[g.hub].author}/g{seed}-{k}",
                    w.record_id,
                )
            ],
        )
    return Graph(seed, nodes, edges, grant_rows, _shapes(nodes, edges, grant_rows))


@contextmanager
def graph(w: World, seed: int) -> Iterator[Graph]:
    """One generated graph inside a savepoint that is always rolled back."""
    w.conn.execute("SAVEPOINT readable_graph")
    try:
        yield _build(w, seed)
    finally:
        w.conn.execute("ROLLBACK TO SAVEPOINT readable_graph")
        w.conn.execute("RELEASE SAVEPOINT readable_graph")


# ---------------------------------------------------------------------------
# The comparison. ``fn`` is the NAME of the function under test.
# ---------------------------------------------------------------------------


def result_sets(conn: Conn, fn: str, viewers: list[int]) -> dict[int, set[int]]:
    """For each viewer, the ids that function ``fn`` returns (its one column)."""
    rows = conn.execute(
        sql.SQL(
            "SELECT v.id AS viewer, ARRAY(SELECT f.* FROM {fn}(v.id) AS f) AS ids"
            " FROM unnest(%s::bigint[]) AS v(id)"
        ).format(fn=sql.Identifier(fn)),
        (viewers,),
    ).fetchall()
    return {r["viewer"]: set(r["ids"]) for r in rows}


def hubs_among(conn: Conn, fn: str, viewers: list[int]) -> dict[int, set[int]]:
    """For each viewer, the nodes of kind 'hub' among what ``fn`` returns.
    ``fn`` is a function of the shape of ``readable_nodes``."""
    rows = conn.execute(
        sql.SQL(
            "SELECT v.id AS viewer, ARRAY(SELECT n.id FROM {fn}(v.id) AS f"
            " JOIN node n ON n.id = f.node_id WHERE n.kind = 'hub') AS ids"
            " FROM unnest(%s::bigint[]) AS v(id)"
        ).format(fn=sql.Identifier(fn)),
        (viewers,),
    ).fetchall()
    return {r["viewer"]: set(r["ids"]) for r in rows}


Difference = dict[int, tuple[set[int], set[int]]]


def _diff(want: dict[int, set[int]], got: dict[int, set[int]]) -> Difference:
    return {v: (want[v] - got[v], got[v] - want[v]) for v in want if want[v] != got[v]}


def differences(conn: Conn, fn: str, reference: str, viewers: list[int]) -> Difference:
    """Per viewer where ``fn`` differs from ``reference``: (missing, extra)."""
    return _diff(result_sets(conn, reference, viewers), result_sets(conn, fn, viewers))


def _seeds() -> list[int]:
    chosen = os.environ.get("READABLE_HUBS_SEEDS")
    if chosen:
        return [int(s) for s in chosen.split(",")]
    return list(range(1000, 1400))


SEEDS = _seeds()


def _check(w: World, seed: int, fn: str, reference: str) -> None:
    with graph(w, seed) as g:
        diff = differences(w.conn, fn, reference, w.viewers)
        shown = {
            v: {"missing": sorted(m), "extra": sorted(x)} for v, (m, x) in diff.items()
        }
        names = {i: h for h, i in w.account_ids.items()}
        text = {names.get(v, v): d for v, d in shown.items()}
        assert not diff, (
            f"{fn} differs from the rule of 0200 on graph seed {seed} "
            f"(replay: READABLE_HUBS_SEEDS={seed}): {text}; "
            f"shapes: {dict(g.shapes)}"
        )


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_readable_nodes_unchanged(world: World, seed: int) -> None:
    _check(world, seed, "readable_nodes", OLD_NODES)


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_readable_hubs_unchanged(world: World, seed: int) -> None:
    _check(world, seed, "readable_hubs", OLD_REACH)


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_hubs_among_readable_nodes_are_the_reach(world: World, seed: int) -> None:
    """The property that lets ``h.id IN readable_nodes`` for a hub ``h`` become
    ``h.id = ANY(readable hubs)``: the nodes of kind 'hub' that the old
    ``readable_nodes`` returns are exactly the hubs of the old ``reach``."""
    with graph(world, seed) as g:
        diff = _diff(
            result_sets(world.conn, OLD_REACH, world.viewers),
            hubs_among(world.conn, OLD_NODES, world.viewers),
        )
        assert not diff, (
            f"hubs among readable_nodes differ from reach on graph seed {seed}"
            f" (replay: READABLE_HUBS_SEEDS={seed}); viewer: (in reach only,"
            f" readable only): {diff}; shapes: {dict(g.shapes)}"
        )


# ---------------------------------------------------------------------------
# One pass over all graphs: coverage of the generator, and the wrong variants.
# ---------------------------------------------------------------------------


@dataclass
class Survey:
    graphs: int = 0
    shapes: Counter[str] = field(default_factory=Counter)  # graphs having the shape
    viewer_shapes: Counter[str] = field(default_factory=Counter)
    # variant -> number of graphs on which it was told apart. A variant that has
    # been told apart on CAP graphs for every question asked of it is not
    # evaluated on further graphs (it costs time), so counts stop near CAP.
    node_told_apart: Counter[str] = field(default_factory=Counter)
    hub_told_apart: Counter[str] = field(default_factory=Counter)
    # variant -> graphs on which "hubs among its nodes == its reach" breaks
    property_broken: Counter[str] = field(default_factory=Counter)
    first_seed: dict[str, int] = field(default_factory=dict)


CAP = int(os.environ.get("READABLE_HUBS_CAP", "15"))

# Wrong variants that break "hubs among readable nodes == reach" on some graph.
# Found by running; the test below keeps them breaking it.
BREAK_THE_PROPERTY = (
    "author_dropped_in_final",
    "withdrawn_dropped_in_final",
    "filing_audience_dropped_in_final",
    "member_audience_dropped_in_reach",
    "member_audience_dropped_in_final",
    "granted_hub_audience_dropped",
    "own_hub_needs_audience_hub",
    "tombstone_dropped_on_own_hub",
    "tombstone_dropped_on_granted_hub",
    "tombstone_dropped_on_member_hub",
    "tombstone_dropped_on_own_note",
    "tombstone_dropped_on_member_note",
    "kind_dropped_on_own_hub",
    "kind_dropped_on_granted_hub",
    "kind_dropped_on_member_hub",
    "recursion_cut_to_one_level",
    "recursion_cut_to_two_levels",
    "recursion_cut_to_three_levels",
    "own_notes_branch_dropped",
    "readable_hubs_branch_dropped",
)


@pytest.fixture(scope="module")
def survey(survey_world: World) -> Survey:
    world = survey_world
    s = Survey()
    for seed in SEEDS:
        with graph(world, seed) as g:
            s.graphs += 1
            for shape, n in g.shapes.items():
                if n:
                    s.shapes[shape] += 1
            conn = world.conn
            old = result_sets(conn, OLD_NODES, world.viewers)
            reach = result_sets(conn, OLD_REACH, world.viewers)
            for handle, account_id in world.account_ids.items():
                me = world.persons[handle]
                seen = old[account_id]
                if any(
                    g.nodes[i].kind != "hub" and g.nodes[i].author != me
                    for i in seen
                    if i in g.nodes
                ):
                    s.viewer_shapes["viewer_reads_somebody_elses_note"] += 1
                if any(
                    g.nodes[i].author != me for i in reach[account_id] if i in g.nodes
                ):
                    s.viewer_shapes["viewer_reaches_somebody_elses_hub"] += 1
                if any(
                    g.nodes[i].author != me and g.nodes[i].kind == "hub"
                    for i in seen
                    if i in g.nodes
                ):
                    s.viewer_shapes["viewer_reads_somebody_elses_hub"] += 1
                if not seen & set(g.nodes):
                    s.viewer_shapes["viewer_reads_nothing_of_the_graph"] += 1
                s.viewer_shapes["viewers"] += 1
            for name in ALL_VARIANTS:
                own_reach = name in REACH_VARIANTS
                need_nodes = s.node_told_apart[name] < CAP
                need_hubs = own_reach and s.hub_told_apart[name] < CAP
                need_property = (
                    name in BREAK_THE_PROPERTY and s.property_broken[name] < CAP
                )
                if not (need_nodes or need_hubs or need_property):
                    continue
                fn = _nodes_name(name)
                got = result_sets(conn, fn, world.viewers)
                if _diff(old, got):
                    s.node_told_apart[name] += 1
                    s.first_seed.setdefault(name, seed)
                variant_reach = (
                    result_sets(conn, _reach_name(name), world.viewers)
                    if own_reach
                    else reach
                )
                if own_reach and _diff(reach, variant_reach):
                    s.hub_told_apart[name] += 1
                if name in BREAK_THE_PROPERTY and _diff(
                    variant_reach, hubs_among(conn, fn, world.viewers)
                ):
                    s.property_broken[name] += 1
            # the property itself, for the real rule
            if _diff(reach, hubs_among(conn, OLD_NODES, world.viewers)):
                s.property_broken["(the rule of 0200)"] += 1
    if os.environ.get("READABLE_HUBS_REPORT"):
        print("\ngraphs:", s.graphs, "cap:", CAP)
        print("shapes (graphs having it):", dict(sorted(s.shapes.items())))
        print("viewer shapes:", dict(sorted(s.viewer_shapes.items())))
        for name in ALL_VARIANTS:
            print(
                f"variant {name}: nodes {s.node_told_apart[name]}"
                f" hubs {s.hub_told_apart[name] if name in REACH_VARIANTS else '-'}"
                f" property {s.property_broken[name]}"
            )
        print(
            "property broken by the rule itself:",
            s.property_broken["(the rule of 0200)"],
        )
    return s


SHAPES = (
    "chain_of_3_hubs",
    "chain_of_4_hubs",
    "chain_of_5_hubs",
    "grant_read_on_top_hub",
    "grant_add_on_top_hub",
    "grant_none_on_top_hub",
    "grant_read_on_sub_hub",
    "grant_add_on_sub_hub",
    "grant_none_on_sub_hub",
    "grant_on_a_note",
    "grant_on_private_hub",
    "grant_on_tombstoned_hub",
    "private_hub",
    "private_note",
    "unknown_audience",
    "private_filing",
    "withdrawn_filing",
    "note_filed_by_other",
    "hub_filed_by_other",
    "filed_under_a_note",
    "tombstoned_note",
    "tombstoned_hub",
    "tombstoned_hub_in_a_chain",
    "hub_under_two_hubs",
    "cycle",
    "author_without_account",
)

VIEWER_SHAPES = (
    "viewer_reads_somebody_elses_note",
    "viewer_reads_somebody_elses_hub",
    "viewer_reaches_somebody_elses_hub",
)


def test_there_are_many_graphs(survey: Survey) -> None:
    assert survey.graphs >= 300 or os.environ.get("READABLE_HUBS_SEEDS")


@pytest.mark.parametrize("shape", SHAPES)
def test_generator_covers_every_shape(survey: Survey, shape: str) -> None:
    assert survey.shapes[shape] > 0, f"no graph had the shape {shape}"


@pytest.mark.parametrize("shape", VIEWER_SHAPES)
def test_generator_covers_every_viewer_shape(survey: Survey, shape: str) -> None:
    assert survey.viewer_shapes[shape] > 0, f"no viewer had the shape {shape}"


def test_there_are_at_least_four_accounts(world: World) -> None:
    assert len(world.account_ids) >= 4


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_wrong_variant_is_told_apart(survey: Survey, variant: str) -> None:
    assert survey.node_told_apart[variant] > 0, (
        f"the comparison of readable_nodes cannot tell {variant} from the rule"
    )
    if variant in REACH_VARIANTS:
        assert survey.hub_told_apart[variant] > 0, (
            f"the comparison of the hubs cannot tell {variant} from the rule"
        )


@pytest.mark.parametrize("variant", BREAK_THE_PROPERTY)
def test_wrong_variant_breaks_the_hub_property(survey: Survey, variant: str) -> None:
    assert survey.property_broken[variant] > 0, (
        f"{variant} keeps 'hubs among readable nodes == reach' on every graph"
    )


@pytest.mark.parametrize("variant", sorted(REDUNDANT))
def test_redundant_tests_of_the_rule_change_nothing(
    survey: Survey, variant: str
) -> None:
    """See ``REDUNDANT``: no graph can tell these apart, and none does."""
    assert survey.node_told_apart[variant] == 0


def test_the_rule_keeps_the_hub_property(survey: Survey) -> None:
    assert survey.property_broken["(the rule of 0200)"] == 0
