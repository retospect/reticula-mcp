"""Gate for ``current_among(viewer, ids)``, the short-list form of ``current_nodes``.

``current_among(viewer bigint, ids bigint[]) RETURNS TABLE (node_id bigint)``
returns the ids among ``ids`` that ``current_nodes(viewer)`` returns: the same
function cut to ``ids``, each id at most once. A NULL array and an empty array
return no rows, NULL elements are ignored, ids that exist in no table are
ignored. It exists so that a caller with a short list does not pay for the full
``current_nodes``. The cut must not change the answer: a note that a newer note
hides stays hidden when the newer note is not in ``ids``.

This file is written from the definitions in migrations 0203
(``supersession_hides``, ``current_nodes``) and 0204 (``readable_hubs``,
``readable_nodes``) alone. It keeps their bodies as SQL text (the ``*_BODY``
constants below, each asserted to be in its migration), installs them in the
schema ``tmp_old`` of the test database, and compares ``current_among`` with
``tmp_old.current_nodes`` cut to the ids, on many random graphs, for every
account as viewer and for an account id that does not exist.

* ``test_current_among_equals_current_nodes[seedN]`` checks one graph. A
  failure names the seed. Replay one graph with ``-k seedN``, or a list of
  seeds with the environment variable ``CURRENT_AMONG_SEEDS=12,40`` (it changes
  which graphs the per-seed tests build; the survey below always uses all).
* ``test_spec_equals_current_nodes[seedN]`` runs the same comparison on a
  function of this file, written from the contract (the old function cut to
  ids). It passes today and checks the comparison itself.
* ``test_wrong_variant_is_told_apart`` installs wrong readings of the contract
  as SQL functions and asserts that the same comparison finds each of them on
  at least one graph. A comparison that cannot tell a wrong function from the
  right one is no gate. ``test_correct_variants_agree`` is the other side: two
  correct functions written in other ways must not be told apart.
* ``test_generator_covers_every_shape`` and friends assert that the generator
  keeps producing every shape of supersession and every kind of id list.

Graphs come from the generator of ``tests/test_readable_hubs.py`` (hubs, chains
of hubs, grants, filings, private things, tombstones, people without an
account), plus rows in ``supersedes`` and ``link`` in every shape the rule of
``supersession_hides`` tells apart (``_Extras`` below).
"""

from __future__ import annotations

import itertools
import os
import random
import time
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
from reticula_mcp.graph import handlers
from tests.graph_support import mk_hub, mk_note, share, sql_node_id
from tests.test_readable_hubs import FOREIGN, HANDLES, NO_SUCH_ACCOUNT, Graph, World
from tests.test_readable_hubs import _build as build_readable_graph

# ---------------------------------------------------------------------------
# The rule, word for word. Each constant is the text between the two $$ of a
# function in the migration named next to it.
# ---------------------------------------------------------------------------

MIGRATIONS = Path(__file__).parent.parent / "src" / "reticula_mcp" / "db" / "migrations"

# 0204_readable_hubs.sql, readable_hubs
READABLE_HUBS_BODY = """\
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
    SELECT r.hub_id FROM reach r
"""

# 0204_readable_hubs.sql, readable_nodes
READABLE_NODES_BODY = """\
    WITH me AS (
        SELECT a.person_id FROM account a WHERE a.id = viewer
    ),
    reach AS MATERIALIZED (
        SELECT h.hub_id FROM readable_hubs(viewer) h
    )
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
      AND n.tombstoned_at IS NULL AND n.audience = 'hub'
"""

# 0203_shared_rules.sql, supersession_hides
HIDES_BODY = """\
    SELECT o.author = n.author
        OR (
             o.kind NOT IN ('hub', 'instruction')
             -- scalar sub-selects with LIMIT 1: evaluated once per pair through
             -- the indexes, never flattened into a hash over all memberships
             -- of a big hub
             AND coalesce((
                 SELECT true
                 FROM membership mo
                 JOIN membership mn ON mn.hub_node_id = mo.hub_node_id
                 JOIN node h ON h.id = mo.hub_node_id
                 WHERE mo.node_id = o.id AND NOT mo.withdrawn
                   AND mo.audience = 'hub' AND mo.author = o.author
                   AND mn.node_id = n.id AND NOT mn.withdrawn
                   AND mn.audience = 'hub' AND mn.author = n.author
                   AND h.kind = 'hub' AND h.tombstoned_at IS NULL
                 LIMIT 1
             ), false)
             AND NOT coalesce((
                 SELECT true FROM link l
                 WHERE l.src_node_id = o.id AND l.rel = 'disputes'
                   AND l.dst_address = n.address
                 LIMIT 1
             ), false)
        )
    FROM node o, node n
    WHERE o.id = old_id AND n.id = new_id
"""

# 0203_shared_rules.sql, current_nodes
CURRENT_NODES_BODY = """\
    WITH rn AS MATERIALIZED (
        SELECT r.node_id FROM readable_nodes(viewer) r
    ),
    hidden AS MATERIALIZED (
        SELECT DISTINCT s.old_node_id AS id
        FROM supersedes s
        JOIN rn ON rn.node_id = s.new_node_id
        CROSS JOIN LATERAL supersession_hides(s.old_node_id, s.new_node_id) p
        WHERE p.hides
    )
    SELECT r.node_id
    FROM rn r
    WHERE NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.node_id)
"""


@pytest.mark.parametrize(
    ("body", "migration"),
    [
        (READABLE_HUBS_BODY, "0204_readable_hubs.sql"),
        (READABLE_NODES_BODY, "0204_readable_hubs.sql"),
        (HIDES_BODY, "0203_shared_rules.sql"),
        (CURRENT_NODES_BODY, "0203_shared_rules.sql"),
    ],
)
def test_the_kept_text_is_the_text_of_the_migration(body: str, migration: str) -> None:
    text = (MIGRATIONS / migration).read_text()
    assert f"$$\n{body}$$" in text


# Installed in the schema tmp_old, whose path puts tmp_old first: the bodies
# call readable_nodes and supersession_hides by their plain names and get these
# copies, whatever a later migration does to the real ones.
OLD = "tmp_old"
OLD_CURRENT = f"{OLD}.current_nodes"
OLD_READABLE = f"{OLD}.readable_nodes"
SPEC = f"{OLD}.spec_current_among"
MODEL = f"{OLD}.model_current_among"
REAL = "current_among"


def _create(conn: Conn, name: str, args: str, returns: str, body: str) -> None:
    """Install ``body`` as ``tmp_old.name`` (constant names and texts only)."""
    conn.execute(
        f"CREATE FUNCTION {OLD}.{name}({args}) RETURNS {returns}"
        f" LANGUAGE sql STABLE SET search_path = {OLD}, public"
        f" AS $body$\n{body}\n$body$"
    )


AMONG_ARGS = "viewer bigint, ids bigint[]"
AMONG_RETURNS = "TABLE (node_id bigint)"

# The contract, written directly: the old function cut to ids. ``= ANY`` of a
# NULL array is NULL, of an empty array false, a NULL element never matches.
SPEC_BODY = "SELECT c.node_id FROM current_nodes(viewer) c WHERE c.node_id = ANY(ids)"


def _model_sql(
    *,
    rn: str = "readable_nodes(viewer)",
    cut: str = "r.node_id = ANY(ids)",
    hiding_rn: str = "readable_nodes(viewer)",
    gate: str = "JOIN hrn ON hrn.node_id = s.new_node_id",
    hides: str = "supersession_hides(s.old_node_id, s.new_node_id)",
    hidden: str = "s.old_node_id",
    only: str = "s.old_node_id = ANY(ids)",
) -> str:
    """A reading of the contract that cuts first. The defaults are a correct one
    (it only looks at supersedes rows whose older note is in ``ids``); the wrong
    variants change one part each."""
    return f"""\
    WITH rn AS MATERIALIZED (
        SELECT r.node_id FROM {rn} r WHERE {cut}
    ),
    hrn AS MATERIALIZED (
        SELECT r.node_id FROM {hiding_rn} r
    ),
    hidden AS MATERIALIZED (
        SELECT DISTINCT {hidden} AS id
        FROM supersedes s
        {gate}
        CROSS JOIN LATERAL {hides} p
        WHERE p.hides AND {only}
    )
    SELECT r.node_id FROM rn r
    WHERE NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.node_id)"""


# A second correct function in another shape: ids first, then the same tests.
MODEL_BODY_2 = """\
    SELECT i.id
    FROM (SELECT DISTINCT u.i AS id FROM unnest(ids) u(i) WHERE u.i IS NOT NULL) i
    WHERE i.id IN (SELECT r.node_id FROM readable_nodes(viewer) r)
      AND NOT EXISTS (
        SELECT 1
        FROM supersedes s
        CROSS JOIN LATERAL supersession_hides(s.old_node_id, s.new_node_id) p
        WHERE s.old_node_id = i.id AND p.hides
          AND s.new_node_id IN (SELECT r.node_id FROM readable_nodes(viewer) r)
    )"""
MODEL_2 = f"{OLD}.model2_current_among"

_OTHER = "readable_nodes((SELECT min(a.id) FROM account a WHERE a.id > viewer))"

# Wrong readings of the contract, one part changed in each.
WRONG: dict[str, str] = {
    # What hides.
    "no_hiding_at_all": _model_sql(hides="(SELECT false AS hides)"),
    "hiding_only_by_newer_notes_in_ids": _model_sql(
        only="s.new_node_id = ANY(ids) AND s.old_node_id = ANY(ids)"
    ),
    "hiding_by_any_readable_newer_note": _model_sql(hides="(SELECT true AS hides)"),
    "hiding_by_unreadable_newer_note": _model_sql(gate=""),
    "hiding_by_newer_notes_that_are_current": _model_sql(
        hiding_rn="current_nodes(viewer)"
    ),
    "hiding_by_tombstoned_newer_notes_too": _model_sql(
        hiding_rn="(SELECT x.node_id FROM readable_nodes(viewer) x UNION"
        " SELECT n.id FROM node n WHERE n.tombstoned_at IS NOT NULL)"
    ),
    "hiding_by_what_another_viewer_reads": _model_sql(hiding_rn=_OTHER),
    "old_and_new_swapped_in_the_rule": _model_sql(
        hides="supersession_hides(s.new_node_id, s.old_node_id)"
    ),
    "old_and_new_swapped_in_the_hiding": _model_sql(
        hidden="s.new_node_id",
        gate="JOIN hrn ON hrn.node_id = s.old_node_id",
        hides="supersession_hides(s.new_node_id, s.old_node_id)",
        only="s.new_node_id = ANY(ids)",
    ),
    "hiding_gate_on_the_older_note": _model_sql(
        gate="JOIN hrn ON hrn.node_id = s.old_node_id"
    ),
    "only_the_same_author_hides": _model_sql(
        hides="(SELECT o.author = n.author AS hides FROM node o, node n"
        " WHERE o.id = s.old_node_id AND n.id = s.new_node_id)"
    ),
    # What is returned.
    "no_cut_to_ids": _model_sql(cut="true", only="true"),
    "empty_array_returns_everything": _model_sql(
        cut="(cardinality(ids) = 0 OR r.node_id = ANY(ids))",
        only="(cardinality(ids) = 0 OR s.old_node_id = ANY(ids))",
    ),
    "null_array_returns_everything": _model_sql(
        cut="(ids IS NULL OR r.node_id = ANY(ids))",
        only="(ids IS NULL OR s.old_node_id = ANY(ids))",
    ),
    "only_the_first_id_is_used": _model_sql(
        cut="r.node_id = ANY(ids[1:1])", only="s.old_node_id = ANY(ids[1:1])"
    ),
    "a_null_element_empties_the_answer": _model_sql(
        cut="(r.node_id = ANY(ids) AND array_position(ids, NULL) IS NULL)"
    ),
    "readable_notes_of_another_viewer": _model_sql(rn=_OTHER),
    "duplicate_ids_come_back_twice": """\
    SELECT u.i FROM unnest(ids) u(i) JOIN current_nodes(viewer) c ON c.node_id = u.i""",
}

Edit = tuple[str, str]
_DISPUTE = "AND l.dst_address = n.address"

# Wrong readings of the rule of supersession_hides itself (copies of its text
# with one edit each), used in the same function as the correct model.
RULE_EDITS: dict[str, tuple[Edit, ...]] = {
    "the_same_author_does_not_hide": (
        ("SELECT o.author = n.author\n        OR (", "SELECT false\n        OR ("),
    ),
    "no_common_hub_needed": (("ON mn.hub_node_id = mo.hub_node_id", "ON true"),),
    "dispute_ignored": (("WHERE l.src_node_id = o.id AND", "WHERE false AND"),),
    "dispute_of_any_rel": ((" AND l.rel = 'disputes'", ""),),
    "dispute_of_any_note": ((_DISPUTE, ""),),
    "dispute_by_dst_node_id": ((_DISPUTE, "AND l.dst_node_id = n.id"),),
    "dispute_in_either_direction": (
        ("WHERE l.src_node_id = o.id AND", "WHERE l.src_node_id IN (o.id, n.id) AND"),
        (_DISPUTE, "AND l.dst_address IN (n.address, o.address)"),
    ),
    "tombstoned_common_hub_counts": (
        ("AND h.kind = 'hub' AND h.tombstoned_at IS NULL", "AND h.kind = 'hub'"),
    ),
    "common_hub_may_be_a_note": (
        (
            "AND h.kind = 'hub' AND h.tombstoned_at IS NULL",
            "AND h.tombstoned_at IS NULL",
        ),
    ),
    "older_filing_by_anybody": (
        ("AND mo.audience = 'hub' AND mo.author = o.author", "AND mo.audience = 'hub'"),
    ),
    "newer_filing_by_anybody": (
        ("AND mn.audience = 'hub' AND mn.author = n.author", "AND mn.audience = 'hub'"),
    ),
    "withdrawn_older_filing_counts": (
        ("WHERE mo.node_id = o.id AND NOT mo.withdrawn", "WHERE mo.node_id = o.id"),
    ),
    "withdrawn_newer_filing_counts": (
        ("AND mn.node_id = n.id AND NOT mn.withdrawn", "AND mn.node_id = n.id"),
    ),
    "older_filing_audience_ignored": (
        ("AND mo.audience = 'hub' AND mo.author", "AND mo.author"),
    ),
    "newer_filing_audience_ignored": (
        ("AND mn.audience = 'hub' AND mn.author", "AND mn.author"),
    ),
    "an_old_hub_may_be_hidden": (
        ("o.kind NOT IN ('hub', 'instruction')", "o.kind NOT IN ('instruction')"),
    ),
    "an_old_instruction_may_be_hidden": (
        ("o.kind NOT IN ('hub', 'instruction')", "o.kind NOT IN ('hub')"),
    ),
}


def _edited(edits: tuple[Edit, ...]) -> str:
    text = HIDES_BODY
    for old, new in edits:
        assert text.count(old) == 1, f"edit does not apply exactly once: {old!r}"
        text = text.replace(old, new)
    return text


for _name in RULE_EDITS:
    WRONG[f"rule_{_name}"] = _model_sql(
        hides=f"{OLD}.hides_{_name}(s.old_node_id, s.new_node_id)"
    )


def install_old(conn: Conn) -> None:
    """The frozen reference, the spec, the correct models and the wrong
    variants, all in the schema ``tmp_old``."""
    conn.execute(f"CREATE SCHEMA {OLD}")
    _create(
        conn,
        "readable_hubs",
        "viewer bigint",
        "TABLE (hub_id bigint)",
        READABLE_HUBS_BODY,
    )
    _create(
        conn,
        "readable_nodes",
        "viewer bigint",
        "TABLE (node_id bigint)",
        READABLE_NODES_BODY,
    )
    _create(
        conn,
        "supersession_hides",
        "old_id bigint, new_id bigint",
        "TABLE (hides boolean)",
        HIDES_BODY,
    )
    _create(
        conn,
        "current_nodes",
        "viewer bigint",
        "TABLE (node_id bigint)",
        CURRENT_NODES_BODY,
    )
    _create(conn, "spec_current_among", AMONG_ARGS, AMONG_RETURNS, SPEC_BODY)
    _create(conn, "model_current_among", AMONG_ARGS, AMONG_RETURNS, _model_sql())
    _create(conn, "model2_current_among", AMONG_ARGS, AMONG_RETURNS, MODEL_BODY_2)
    for name, edits in RULE_EDITS.items():
        _create(
            conn, f"hides_{name}", "old_id bigint, new_id bigint",
            "TABLE (hides boolean)", _edited(edits),
        )  # fmt: skip
    for name, body in WRONG.items():
        _create(conn, f"wrong_{name}", AMONG_ARGS, AMONG_RETURNS, body)


def _wrong(name: str) -> str:
    return f"{OLD}.wrong_{name}"


# ---------------------------------------------------------------------------
# The world: four accounts, one stored record every row can point at, and the
# installed functions. One transaction for the whole module, rolled back at the
# end; each graph lives in a savepoint of it.
# ---------------------------------------------------------------------------


@contextmanager
def _open_world(url: str) -> Iterator[World]:
    conn = connect(url, options="-c statement_timeout=60s")
    try:
        rec = conn.execute(
            "INSERT INTO record (version_hash, address, type, author, key_id,"
            " created_at, audience, content_hash, format_version, envelope, signature)"
            " VALUES ('v-current-among', 'ret:pstub/current-among', 'atom', 'pstub',"
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
                (person, f"{handle}@example.com", handle, f"ret:{person}/identity"),
            ).fetchone()
            assert row is not None
            account_ids[handle] = row["id"]
            persons[handle] = person
        install_old(conn)
        yield World(conn, rec["id"], account_ids, persons)
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
    name = f"reticula_ca_{uuid.uuid4().hex[:12]}"
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
# What the generator adds to a graph of test_readable_hubs: supersessions.
# ---------------------------------------------------------------------------

PAIR_SHAPES = {
    "same_author": 12,
    "common_hub": 12,
    "no_common_hub": 7,
    "disputed": 6,
    "old_is_hub": 4,
    "old_is_instruction": 4,
    "flawed": 14,
    "private_newer": 7,
    "new_tombstoned": 4,
    "old_tombstoned": 2,
    "near_miss": 8,
    "noise": 6,
    "chain": 4,
    "two_newer": 4,
    "cycle": 3,
    "self": 1,
}

# One way each in which a filing or the hub it is in can fail the rule.
FLAWS = (
    "older_filing_withdrawn",
    "newer_filing_withdrawn",
    "older_filing_private",
    "newer_filing_private",
    "older_filing_by_other",
    "newer_filing_by_other",
    "container_tombstoned",
    "container_not_a_hub",
)


class _Extras:
    """Rows added on top of one generated graph, from a seed of their own."""

    def __init__(self, w: World, g: Graph, seed: int) -> None:
        self.w, self.g, self.seed = w, g, seed
        self.rng = random.Random(seed * 7919 + 17)
        self.ids = iter(
            r["i"]
            for r in w.conn.execute(
                "SELECT nextval(pg_get_serial_sequence('node', 'id')) AS i"
                " FROM generate_series(1, 120)"
            )
        )
        self.nodes: list[tuple[int, str, str, str, bool]] = []
        self.author: dict[int, str] = {n.id: n.author for n in g.nodes.values()}
        self.members: list[tuple[int, int, str, str, bool]] = []
        self.grants: dict[tuple[int, str], str] = {}
        self.links: list[tuple[int, str, str, int | None]] = []
        self.pairs: set[tuple[int, int]] = set()
        self.clean_hubs = [
            n.id
            for n in g.nodes.values()
            if n.kind == "hub" and not n.tomb and n.audience == "hub"
        ]
        self.plain_notes = [n.id for n in g.nodes.values() if n.kind != "hub"]

    # -- small pieces

    def person(self) -> str:
        people = self.w.people
        return self.rng.choices(people, [1.0] * len(self.w.persons) + [0.5])[0]

    def other(self, p: str) -> str:
        return self.rng.choice([x for x in self.w.people if x != p])

    def address(self, node: int) -> str:
        return f"ret:{self.author[node]}/n{node}"

    def fresh(
        self, author: str, kind: str = "note", audience: str = "hub", tomb: bool = False
    ) -> int:
        i = next(self.ids)
        self.nodes.append((i, author, kind, audience, tomb))
        self.author[i] = author
        return i

    def file(
        self,
        node: int,
        hub: int,
        *,
        author: str | None = None,
        audience: str = "hub",
        withdrawn: bool = False,
    ) -> None:
        self.members.append(
            (node, hub, author or self.author[node], audience, withdrawn)
        )

    def read_grant(self, hub: int) -> None:
        if self.rng.random() < 0.65:
            who = self.rng.choice(list(self.w.persons.values()))
            self.grants[(hub, who)] = self.rng.choice(["read", "read", "add"])

    def container(self, clean: bool = True) -> int:
        """A hub to file things in; somebody other than its owner may read it."""
        rng = self.rng
        if clean and self.clean_hubs and rng.random() < 0.55:
            hub = rng.choice(self.clean_hubs)
        else:
            hub = self.fresh(self.person(), "hub")
        self.read_grant(hub)
        return hub

    def pair(self, new: int, old: int) -> None:
        self.pairs.add((new, old))

    # -- one shape at a time

    def one_pair(self, shape: str) -> None:
        rng = self.rng
        a = self.person()
        b = a if shape == "same_author" else self.other(a)
        if shape == "old_is_hub":
            old_kind = "hub"
        elif shape == "old_is_instruction":
            old_kind = "instruction"
        else:
            old_kind = rng.choices(["note", "paper"], [9, 1])[0]
        new_kind = rng.choices(["note", "paper", "instruction", "hub"], [85, 4, 6, 5])[
            0
        ]
        old = self.fresh(
            a,
            old_kind,
            "private" if rng.random() < 0.08 else "hub",
            shape == "old_tombstoned",
        )
        new_private = shape == "private_newer" or (
            shape == "same_author" and rng.random() < 0.3
        )
        new = self.fresh(
            b,
            new_kind,
            "private" if new_private else "hub",
            shape == "new_tombstoned",
        )
        self.pair(new, old)
        if shape == "same_author":
            if rng.random() < 0.6:
                c = self.container()
                self.file(old, c)
                self.file(new, c)
        elif shape == "no_common_hub":
            if rng.random() < 0.3:
                c1 = self.container()
                self.file(old, c1)
                if rng.random() < 0.7:
                    c2 = self.container()
                    if c2 != c1:
                        self.file(new, c2)
            else:
                self.route(old, new)
        elif shape == "flawed":
            self.flawed(old, new, rng.choice(FLAWS))
        elif shape == "near_miss":
            c = self.container()
            self.file(old, c)
            self.file(new, c)
            kind = rng.choice(["from_newer", "other_rel", "of_another", "unresolved"])
            if kind == "from_newer":
                self.links.append((new, "disputes", self.address(old), old))
            elif kind == "other_rel":
                rel = rng.choice(["part-of", "relates-to", "cites"])
                self.links.append((old, rel, self.address(new), new))
            elif kind == "of_another":
                other = rng.choice([x for x in self.author if x not in (old, new)])
                self.links.append((old, "disputes", self.address(other), other))
            else:
                self.links.append((old, "disputes", self.address(new), None))
        elif shape == "noise":
            pool = sorted(self.g.nodes)
            if len(pool) >= 2:
                n2, o2 = rng.sample(pool, 2)
                self.pair(n2, o2)
        else:  # the rule's other-author case, filed in a common hub
            c = self.container()
            self.file(old, c)
            self.file(new, c)
            if shape == "disputed":
                dst = new if rng.random() < 0.8 else None
                self.links.append((old, "disputes", self.address(new), dst))

    def route(self, old: int, new: int) -> None:
        """File the two in two different hubs that one account may read, so
        that this account reads both whatever else is filed."""
        x1 = self.container()
        x2 = self.container()
        if x2 == x1:
            x2 = self.fresh(self.person(), "hub")
        self.file(old, x1)
        self.file(new, x2)
        reader = self.rng.choice(list(self.w.persons.values()))
        self.grants[(x1, reader)] = "read"
        self.grants[(x2, reader)] = "read"

    def flawed(self, old: int, new: int, flaw: str) -> None:
        if self.rng.random() < 0.75:
            self.route(old, new)
        if flaw == "container_tombstoned":
            c = self.fresh(self.person(), "hub", tomb=True)
        elif flaw == "container_not_a_hub":
            c = self.fresh(self.person(), "note")
        else:
            c = self.container()
        for node, who in ((old, "older"), (new, "newer")):
            self.file(
                node,
                c,
                withdrawn=flaw == f"{who}_filing_withdrawn",
                audience=(
                    self.rng.choice(["private", "team"])
                    if flaw == f"{who}_filing_private"
                    else "hub"
                ),
                author=(
                    self.other(self.author[node])
                    if flaw == f"{who}_filing_by_other"
                    else None
                ),
            )

    def several(self, shape: str) -> None:
        """A chain, two newer notes for one, a cycle, a note over itself."""
        rng = self.rng
        a = self.person()
        b = self.other(a)
        k = {"chain": rng.randint(3, 4), "two_newer": 3, "cycle": rng.randint(2, 3)}
        n = 1 if shape == "self" else k[shape]
        ids = [
            self.fresh(
                rng.choice([a, a, b]),
                "note",
                "private" if rng.random() < 0.1 else "hub",
                rng.random() < 0.05,
            )
            for _ in range(n)
        ]
        if rng.random() < 0.85:
            c = self.container()
            for i in ids:
                if rng.random() < 0.9:
                    self.file(i, c)
        if shape == "chain":
            for older, newer in itertools.pairwise(ids):
                self.pair(newer, older)
        elif shape == "two_newer":
            self.pair(ids[1], ids[0])
            self.pair(ids[2], ids[0])
        elif shape == "cycle":
            for i, j in zip(ids, [*ids[1:], ids[0]], strict=True):
                self.pair(i, j)
        else:
            self.pair(ids[0], ids[0])

    # -- the whole graph

    def apply(self) -> None:
        rng = self.rng
        shapes = list(PAIR_SHAPES)
        for shape in rng.choices(
            shapes, list(PAIR_SHAPES.values()), k=rng.randint(6, 14)
        ):
            if shape in ("chain", "two_newer", "cycle", "self"):
                self.several(shape)
            else:
                self.one_pair(shape)
        pool = [*self.author]
        for _ in range(rng.randint(0, 3)):  # links that mean nothing here
            src, dst = rng.sample(pool, 2)
            rel = rng.choice(["part-of", "relates-to", "disputes"])
            self.links.append((src, rel, self.address(dst), dst))
        self.flush()

    def flush(self) -> None:
        w, seed = self.w, self.seed
        conn = w.conn
        if self.nodes:
            conn.execute(
                "INSERT INTO node (id, address, author, kind, head_record_id,"
                " head_version, audience, tombstoned_at) VALUES "
                + ",".join(
                    "(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() END)"
                    for _ in self.nodes
                ),
                [
                    v
                    for i, author, kind, audience, tomb in self.nodes
                    for v in (
                        i,
                        self.address(i),
                        author,
                        kind,
                        w.record_id,
                        f"h{i}",
                        audience,
                        tomb,
                    )
                ],
            )
        if self.members:
            conn.execute(
                "INSERT INTO membership (address, node_id, hub_node_id, author,"
                " audience, withdrawn, record_id) VALUES "
                + ",".join("(%s,%s,%s,%s,%s,%s,%s)" for _ in self.members),
                [
                    v
                    for k, (node, hub, author, aud, wd) in enumerate(self.members)
                    for v in (
                        f"ret:{author}/x{seed}-{k}",
                        node,
                        hub,
                        author,
                        aud,
                        wd,
                        w.record_id,
                    )
                ],
            )
        for k, ((hub, grantee), mode) in enumerate(self.grants.items()):
            conn.execute(
                "INSERT INTO hub_grant (hub_node_id, grantee, mode, granted_by,"
                " address, record_id) VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (hub_node_id, grantee) DO UPDATE SET mode = EXCLUDED.mode",
                (
                    hub,
                    grantee,
                    mode,
                    self.author[hub],
                    f"ret:{self.author[hub]}/y{seed}-{k}",
                    w.record_id,
                ),
            )
        for src, rel, dst_address, dst in self.links:
            conn.execute(
                "INSERT INTO link (src_node_id, rel, dst_address, dst_node_id)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (src, rel, dst_address, dst),
            )
        for new, old in sorted(self.pairs):
            conn.execute(
                "INSERT INTO supersedes (new_node_id, old_node_id) VALUES (%s,%s)"
                " ON CONFLICT DO NOTHING",
                (new, old),
            )


# ---------------------------------------------------------------------------
# A reading of the graph in Python, from the tables. It is the second opinion
# on the rule (``test_the_python_reading_agrees``) and gives the shapes.
# ---------------------------------------------------------------------------


class NodeRow(NamedTuple):
    id: int
    address: str
    author: str
    kind: str
    audience: str
    tomb: bool


class Filing(NamedTuple):
    node: int
    hub: int
    author: str
    audience: str
    withdrawn: bool


class LinkRow(NamedTuple):
    src: int
    rel: str
    dst_address: str
    dst: int | None


@dataclass
class Model:
    nodes: dict[int, NodeRow]
    filings: list[Filing]
    links: list[LinkRow]
    pairs: list[tuple[int, int]]  # (new, old)


def _load(conn: Conn) -> Model:
    nodes = {
        r["id"]: NodeRow(
            r["id"], r["address"], r["author"], r["kind"], r["audience"], r["tomb"]
        )
        for r in conn.execute(
            "SELECT id, address, author, kind, audience,"
            " tombstoned_at IS NOT NULL AS tomb FROM node"
        )
    }
    filings = [
        Filing(
            r["node_id"], r["hub_node_id"], r["author"], r["audience"], r["withdrawn"]
        )
        for r in conn.execute(
            "SELECT node_id, hub_node_id, author, audience, withdrawn FROM membership"
        )
    ]
    links = [
        LinkRow(r["src_node_id"], r["rel"], r["dst_address"], r["dst_node_id"])
        for r in conn.execute(
            "SELECT src_node_id, rel, dst_address, dst_node_id FROM link"
        )
    ]
    pairs = [
        (r["new_node_id"], r["old_node_id"])
        for r in conn.execute("SELECT new_node_id, old_node_id FROM supersedes")
    ]
    return Model(nodes, filings, links, pairs)


def _filing_flaws(f: Filing, node: NodeRow, who: str) -> set[str]:
    flaws = set()
    if f.withdrawn:
        flaws.add(f"{who}_filing_withdrawn")
    if f.audience != "hub":
        flaws.add(f"{who}_filing_private")
    if f.author != node.author:
        flaws.add(f"{who}_filing_by_other")
    return flaws


def _common_hub_flaws(m: Model, old: NodeRow, new: NodeRow) -> list[set[str]]:
    """For each hub the two notes are both filed in (in any way): what fails
    the rule there. An empty set is a hub that satisfies it."""
    older = [f for f in m.filings if f.node == old.id]
    newer = [f for f in m.filings if f.node == new.id]
    out = []
    for hub in {f.hub for f in older} & {f.hub for f in newer}:
        h = m.nodes[hub]
        flaws: set[str] = set()
        if h.kind != "hub":
            flaws.add("container_not_a_hub")
        if h.tomb:
            flaws.add("container_tombstoned")
        best_old = min(
            (_filing_flaws(f, old, "older") for f in older if f.hub == hub), key=len
        )
        best_new = min(
            (_filing_flaws(f, new, "newer") for f in newer if f.hub == hub), key=len
        )
        out.append(flaws | best_old | best_new)
    return out


def _disputes(m: Model, old: NodeRow, new: NodeRow) -> list[LinkRow]:
    return [
        link
        for link in m.links
        if link.src == old.id
        and link.rel == "disputes"
        and link.dst_address == new.address
    ]


def _hides(m: Model, old_id: int, new_id: int) -> bool:
    """The rule of supersession_hides, read again in Python."""
    old, new = m.nodes[old_id], m.nodes[new_id]
    if old.author == new.author:
        return True
    return (
        old.kind not in ("hub", "instruction")
        and any(not flaws for flaws in _common_hub_flaws(m, old, new))
        and not _disputes(m, old, new)
    )


def _pair_shapes(m: Model, new_id: int, old_id: int) -> set[str]:
    out: set[str] = set()
    if new_id == old_id:
        return {"self_supersession"}
    old, new = m.nodes[old_id], m.nodes[new_id]
    if new.tomb:
        out.add("newer_tombstoned")
    if old.tomb:
        out.add("older_tombstoned")
    if new.audience != "hub":
        out.add("newer_not_for_the_hub")
    if old.author == new.author:
        out.add("same_author")
        if new.audience != "hub":
            out.add("same_author_newer_private")
        return out
    common = _common_hub_flaws(m, old, new)
    if not common:
        out.add("other_author_no_common_hub")
        return out
    for flaws in common:
        if len(flaws) == 1:
            out.add(f"common_hub_failed_only_by_{next(iter(flaws))}")
    if not any(not flaws for flaws in common):
        out.add("other_author_common_hub_that_fails_the_rule")
        return out
    direct = _disputes(m, old, new)
    if old.kind == "hub":
        out.add("older_is_a_hub_other_author_common_hub")
    elif old.kind == "instruction":
        out.add("older_is_an_instruction_other_author_common_hub")
    elif direct:
        out.add("other_author_disputed")
        if any(link.dst is None for link in direct):
            out.add("dispute_without_dst_node")
    else:
        out.add("other_author_common_hub")
        for link in m.links:
            if (
                link.src == new.id
                and link.rel == "disputes"
                and link.dst_address == old.address
            ):
                out.add("dispute_from_the_newer_note")
            if (
                link.src == old.id
                and link.rel != "disputes"
                and link.dst_address == new.address
            ):
                out.add("link_of_another_rel")
            if (
                link.src == old.id
                and link.rel == "disputes"
                and link.dst_address != new.address
            ):
                out.add("dispute_of_another_note")
    return out


def _structure_shapes(m: Model) -> set[str]:
    out: set[str] = set()
    newer_of: dict[int, set[int]] = {}
    older_of: dict[int, set[int]] = {}
    for new, old in m.pairs:
        if new != old:
            newer_of.setdefault(old, set()).add(new)
            older_of.setdefault(new, set()).add(old)
    if any(len(v) >= 2 for v in newer_of.values()):
        out.add("old_note_with_two_newer_notes")
    if any(n in older_of for n in newer_of):
        out.add("chain_of_supersessions")

    def reaches(start: int, target: int) -> bool:
        seen: set[int] = set()
        stack = list(older_of.get(start, ()))
        while stack:
            x = stack.pop()
            if x == target:
                return True
            if x not in seen:
                seen.add(x)
                stack.extend(older_of.get(x, ()))
        return False

    if any(reaches(n, n) for n in older_of):
        out.add("cycle_of_supersessions")
    return out


# ---------------------------------------------------------------------------
# The id lists, and the comparison.
# ---------------------------------------------------------------------------

Ids = list[int | None] | None


class Case(NamedTuple):
    kind: str
    ids: Ids


def _cases(
    rng: random.Random,
    m: Model,
    readable: set[int],
    current: set[int],
    viewer_shapes: Counter[str],
) -> list[Case]:
    all_ids = sorted(m.nodes)
    ghost = max(all_ids, default=0) + 1000
    rd = sorted(readable)
    hidden = sorted(readable - current)
    hiders: dict[int, set[int]] = {}
    for new, old in m.pairs:
        if new in readable and old in readable and _hides(m, old, new):
            hiders.setdefault(old, set()).add(new)
    newer_all = {new for new, _ in m.pairs}
    out: list[Case] = []

    def add(kind: str, ids: Ids) -> None:
        out.append(Case(kind, ids))

    def some(pool: list[int], p: float) -> list[int]:
        return [i for i in pool if rng.random() < p]

    add("all", all_ids)
    add("all_reversed_with_ghosts", [*reversed(all_ids), ghost, 0, -5])
    shuffled = all_ids[:]
    rng.shuffle(shuffled)
    add("all_shuffled", shuffled)
    for p in (0.15, 0.5, 0.85):
        add("subset", some(all_ids, p))
    add("few", rng.sample(all_ids, min(len(all_ids), rng.randint(1, 3))))
    # The trap: the older note of a supersession without the newer one that hides it.
    for old in rng.sample(hidden, min(2, len(hidden))):
        add("old_only", [old])
        add(
            "old_without_its_hiders",
            [
                old,
                *some(
                    [i for i in rd if i != old and i not in hiders.get(old, ())], 0.5
                ),
            ],
        )
        add(
            "old_without_any_newer_note",
            [old, *some([i for i in rd if i != old and i not in newer_all], 0.6)],
        )
    newer_readable = sorted(newer_all & readable)
    for new in rng.sample(newer_readable, min(2, len(newer_readable))):
        add("newer_only", [new])
    both = [(o, n) for n, o in m.pairs if n in readable and o in readable and n != o]
    for old, new in rng.sample(both, min(2, len(both))):
        add("old_and_newer", [old, new])
    unreadable = [i for i in all_ids if i not in readable]
    add("unreadable", unreadable)
    add("unreadable_and_readable", [*unreadable, *some(rd, 0.5)])
    for old, new in rng.sample(both, min(1, len(both))):
        add("old_and_newer_among_unreadable", [*some(unreadable, 0.7), old, new])
    add("ghost_ids", [ghost, ghost + 1, 0, -1, -ghost])
    add("real_and_ghost_ids", [*some(all_ids, 0.5), ghost, -3])
    add("empty", [])
    add("duplicates", all_ids + all_ids)
    dup_pool = rd or all_ids
    if dup_pool:
        x = rng.choice(dup_pool)
        y = rng.choice(all_ids)
        add("duplicates_few", [x, x, x, y, y])
        if hidden:
            add("duplicated_old_only", [hidden[0], hidden[0]])
    add("nulls_around", [None, *all_ids, None])
    add("nulls_between", [None, *some(all_ids, 0.5), None, *some(all_ids, 0.5)])
    add("only_null", [None])
    add("null_array", None)
    add("current_only", sorted(current))
    add("hidden_only", hidden)
    add("readable_only", rd)
    for kind in {c.kind for c in out}:
        viewer_shapes[f"list_{kind}"] += 1
    return out


def _literal(ids: Ids) -> str | None:
    """A bigint[] literal; the NULL array is None."""
    if ids is None:
        return None
    return "{" + ",".join("NULL" if i is None else str(i) for i in ids) + "}"


def _fn(name: str) -> sql.Composable:
    return sql.SQL(".").join(sql.Identifier(p) for p in name.split("."))


def answers(conn: Conn, fn: str, viewer: int, cases: list[Case]) -> list[list[int]]:
    """What function ``fn`` returns for each list, one query for all lists."""
    rows = conn.execute(
        sql.SQL(
            "SELECT l.n AS n, ARRAY(SELECT f.node_id"
            " FROM {fn}(%(v)s::bigint, l.ids::bigint[]) AS f) AS got"
            " FROM unnest(%(lists)s::text[]) WITH ORDINALITY AS l(ids, n)"
            " ORDER BY l.n"
        ).format(fn=_fn(fn)),
        {"v": viewer, "lists": [_literal(c.ids) for c in cases]},
    ).fetchall()
    return [sorted(r["got"]) for r in rows]


def _want(case: Case, current: set[int]) -> list[int]:
    """The old function's rows for the viewer, cut to the ids."""
    return sorted({i for i in (case.ids or []) if i is not None} & current)


@dataclass
class Prepared:
    seed: int
    model: Model
    readable: dict[int, set[int]]  # viewer -> tmp_old.readable_nodes
    current: dict[int, set[int]]  # viewer -> tmp_old.current_nodes
    cases: dict[int, list[Case]]
    shapes: Counter[str] = field(default_factory=Counter)
    viewer_shapes: Counter[str] = field(default_factory=Counter)
    python_disagrees: int = 0


class Mismatch(NamedTuple):
    viewer: int
    kind: str
    ids: Ids
    want: list[int]
    got: list[int]


def compare(conn: Conn, fn: str, p: Prepared) -> list[Mismatch]:
    bad = []
    for viewer, cases in p.cases.items():
        for case, got in zip(cases, answers(conn, fn, viewer, cases), strict=True):
            want = _want(case, p.current[viewer])
            if got != want:
                bad.append(Mismatch(viewer, case.kind, case.ids, want, got))
    return bad


def _viewer_shapes(m: Model, w: World, p: Prepared) -> None:
    for viewer in w.account_ids.values():
        readable, current = p.readable[viewer], p.current[viewer]
        hidden = readable - current
        hides = [
            (new, old)
            for new, old in m.pairs
            if old in readable and new != old and _hides(m, old, new)
        ]
        if hidden:
            p.viewer_shapes["viewer_hidden_note"] += 1
        if any(new not in readable and not m.nodes[new].tomb for new, _ in hides):
            p.viewer_shapes["viewer_hidden_note_newer_unreadable"] += 1
        if any(m.nodes[new].tomb for new, _ in hides):
            p.viewer_shapes["viewer_newer_tombstoned"] += 1
        if any(
            old in hidden
            and new in readable
            and m.nodes[new].author != m.nodes[old].author
            for new, old in hides
        ):
            p.viewer_shapes["viewer_hidden_by_other_author"] += 1
        if any(
            old in hidden and any(o2 in hidden for n2, o2 in hides if n2 == old)
            for _, old in hides
        ):
            p.viewer_shapes["viewer_chain_hidden"] += 1
        if any(i not in readable for i in m.nodes):
            p.viewer_shapes["viewer_cannot_read_some_of_the_graph"] += 1
        if any(
            old in readable and new in readable and old not in hidden and new != old
            for new, old in m.pairs
        ):
            p.viewer_shapes["viewer_reads_both_and_old_stays"] += 1
        p.viewer_shapes["viewers"] += 1


def _set_of(conn: Conn, fn: str, viewer: int) -> set[int]:
    rows = conn.execute(
        sql.SQL("SELECT f.node_id FROM {fn}(%s) AS f").format(fn=_fn(fn)), (viewer,)
    ).fetchall()
    return {r["node_id"] for r in rows}


def prepare(w: World, seed: int) -> Prepared:
    """Build the graph of this seed inside the open savepoint."""
    g = build_readable_graph(w, seed)
    _Extras(w, g, seed).apply()
    m = _load(w.conn)
    readable = {v: _set_of(w.conn, OLD_READABLE, v) for v in w.viewers}
    current = {v: _set_of(w.conn, OLD_CURRENT, v) for v in w.viewers}
    rng = random.Random(seed * 104729 + 3)
    p = Prepared(seed, m, readable, current, {})
    for v in w.viewers:
        p.cases[v] = _cases(rng, m, readable[v], current[v], p.viewer_shapes)
        # the Python reading of the rule gives the same current notes
        hidden = {
            old for new, old in m.pairs if new in readable[v] and _hides(m, old, new)
        }
        if readable[v] - hidden != current[v]:
            p.python_disagrees += 1
    for new, old in m.pairs:
        p.shapes.update(dict.fromkeys(_pair_shapes(m, new, old), 1))
    p.shapes.update(dict.fromkeys(_structure_shapes(m), 1))
    _viewer_shapes(m, w, p)
    p.shapes["graphs"] += 1
    return p


@contextmanager
def graph(w: World, seed: int) -> Iterator[Prepared]:
    """One generated graph inside a savepoint that is always rolled back."""
    w.conn.execute("SAVEPOINT current_graph")
    try:
        yield prepare(w, seed)
    finally:
        w.conn.execute("ROLLBACK TO SAVEPOINT current_graph")
        w.conn.execute("RELEASE SAVEPOINT current_graph")


def _short(ids: Ids) -> str:
    if ids is None:
        return "NULL"
    return str(ids if len(ids) <= 14 else [*ids[:14], "..."])


def _report(w: World, bad: list[Mismatch]) -> str:
    names = {i: h for h, i in w.account_ids.items()}
    lines = []
    for b in bad[:4]:
        extra = [i for i in b.got if b.got.count(i) > 1]
        lines.append(
            f"viewer {names.get(b.viewer, b.viewer)}, list {b.kind} {_short(b.ids)}:"
            f" want {b.want}, got {b.got}"
            + (f" (twice: {sorted(set(extra))})" if extra else "")
        )
    return f"{len(bad)} lists differ; " + "; ".join(lines)


def _chosen_seeds() -> list[int]:
    chosen = os.environ.get("CURRENT_AMONG_SEEDS")
    if chosen:
        return [int(s) for s in chosen.split(",")]
    return list(range(2000, 2200))


SEEDS = _chosen_seeds()
# The spec function is slow (a full current_nodes per list): fewer graphs.
SPEC_SEEDS = SEEDS[:12]
SURVEY_SEEDS = list(range(2000, 2100))


def _check(w: World, seed: int, fn: str) -> None:
    with graph(w, seed) as p:
        bad = compare(w.conn, fn, p)
        assert not bad, (
            f"{fn} differs from current_nodes cut to ids on graph seed {seed}"
            f" (replay: CURRENT_AMONG_SEEDS={seed}): {_report(w, bad)};"
            f" shapes: {sorted(k for k in p.shapes)}"
        )


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed{s}")
def test_current_among_equals_current_nodes(world: World, seed: int) -> None:
    _check(world, seed, REAL)


@pytest.mark.parametrize("seed", SPEC_SEEDS, ids=lambda s: f"seed{s}")
def test_spec_equals_current_nodes(world: World, seed: int) -> None:
    """The comparison on a function that is right by construction."""
    _check(world, seed, SPEC)


# ---------------------------------------------------------------------------
# Scenes with the real graph API, with answers written out by hand.
# ---------------------------------------------------------------------------


@pytest.fixture
def old_conn(conn: Conn) -> Conn:
    install_old(conn)
    return conn


FUNCTIONS = [pytest.param(SPEC, id="spec"), pytest.param(REAL, id="real")]


def among(conn: Conn, fn: str, viewer: int, ids: Ids) -> list[int]:
    return answers(conn, fn, viewer, [Case("one", ids)])[0]


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_the_older_note_stays_hidden_when_the_newer_is_not_asked_for(
    old_conn: Conn, make_account, fn: str
) -> None:
    ada, ben, cleo = (make_account(handle=h) for h in ("ada", "ben", "cleo"))
    hub = mk_hub(old_conn, ada, "Kiln")
    share(old_conn, ada, hub, ben, "read")
    old = sql_node_id(old_conn, mk_note(old_conn, ada, "glaze 1200 C", hub=hub))
    new = sql_node_id(
        old_conn,
        mk_note(
            old_conn, ada, "glaze 1222 C", hub=hub, supersedes=[_address(old_conn, old)]
        ),
    )
    for viewer in (ada, ben):
        assert among(old_conn, fn, viewer.id, [old]) == []
        assert among(old_conn, fn, viewer.id, [new]) == [new]
        assert among(old_conn, fn, viewer.id, [old, new]) == [new]
    assert among(old_conn, fn, cleo.id, [old, new]) == []


def _address(conn: Conn, node_id: int) -> str:
    row = conn.execute("SELECT address FROM node WHERE id = %s", (node_id,)).fetchone()
    assert row is not None
    return row["address"]


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_another_authors_correction_hides_until_the_author_objects(
    old_conn: Conn, make_account, fn: str
) -> None:
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(old_conn, ada, "Kiln")
    share(old_conn, ada, hub, ben, "add")
    old_address = mk_note(old_conn, ada, "glaze 1200 C", hub=hub)
    old = sql_node_id(old_conn, old_address)
    new_address = mk_note(
        old_conn, ben, "glaze 1180 C", hub=hub, supersedes=[old_address]
    )
    new = sql_node_id(old_conn, new_address)
    for viewer in (ada, ben):
        assert among(old_conn, fn, viewer.id, [old]) == []
        assert among(old_conn, fn, viewer.id, [old, new]) == [new]
    handlers.link(old_conn, ada, ref=old_address, to=new_address, rel="disputes")
    for viewer in (ada, ben):
        assert among(old_conn, fn, viewer.id, [old]) == [old]
        assert among(old_conn, fn, viewer.id, [old, new]) == [old, new]


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_a_private_newer_note_hides_the_older_for_its_author_only(
    old_conn: Conn, make_account, fn: str
) -> None:
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(old_conn, ada, "Kiln")
    share(old_conn, ada, hub, ben, "read")
    old_address = mk_note(old_conn, ada, "glaze 1200 C", hub=hub)
    old = sql_node_id(old_conn, old_address)
    new = sql_node_id(
        old_conn,
        mk_note(old_conn, ada, "glaze 1222 C", private=True, supersedes=[old_address]),
    )
    assert among(old_conn, fn, ada.id, [old]) == []
    assert among(old_conn, fn, ben.id, [old]) == [old]
    assert among(old_conn, fn, ben.id, [new]) == []


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_edge_arrays(old_conn: Conn, make_account, fn: str) -> None:
    ada = make_account(handle="ada")
    one = sql_node_id(old_conn, mk_note(old_conn, ada, "one"))
    two = sql_node_id(old_conn, mk_note(old_conn, ada, "two"))
    assert among(old_conn, fn, ada.id, None) == []
    assert among(old_conn, fn, ada.id, []) == []
    assert among(old_conn, fn, ada.id, [None]) == []
    assert among(old_conn, fn, ada.id, [None, two, None]) == [two]
    assert among(old_conn, fn, ada.id, [one, one, two, one, two]) == [one, two]
    assert among(old_conn, fn, ada.id, [0, -1, one + two + 10_000]) == []
    assert among(old_conn, fn, NO_SUCH_ACCOUNT, [one, two]) == []


def test_the_function_has_the_contract_signature(conn: Conn) -> None:
    row = conn.execute(
        "SELECT pg_get_function_arguments(p.oid) AS args,"
        " pg_get_function_result(p.oid) AS result"
        " FROM pg_proc p WHERE p.proname = 'current_among'"
        " AND p.pronamespace = 'public'::regnamespace"
    ).fetchone()
    assert row is not None, "function current_among(bigint, bigint[]) does not exist"
    assert row["args"] == "viewer bigint, ids bigint[]"
    assert row["result"] == "TABLE(node_id bigint)"


# ---------------------------------------------------------------------------
# One pass over all graphs: coverage of the generator, the wrong variants, the
# correct variants.
# ---------------------------------------------------------------------------

CAP = int(os.environ.get("CURRENT_AMONG_CAP", "1"))


@dataclass
class Survey:
    graphs: int = 0
    shapes: Counter[str] = field(default_factory=Counter)  # graphs having it
    viewer_shapes: Counter[str] = field(default_factory=Counter)
    python_disagrees: int = 0
    # variant -> graphs on which it was told apart (counted up to CAP), and the
    # kinds of list that told it apart (graphs, up to the same cap)
    told_apart: Counter[str] = field(default_factory=Counter)
    told_by: dict[str, Counter[str]] = field(default_factory=dict)
    first_seed: dict[str, int] = field(default_factory=dict)
    correct_differs: Counter[str] = field(default_factory=Counter)
    correct_checked: Counter[str] = field(default_factory=Counter)
    timings: dict[str, float] = field(default_factory=dict)


@pytest.fixture(scope="module")
def survey(survey_world: World) -> Survey:
    w = survey_world
    s = Survey()
    names = list(WRONG)
    t0 = time.perf_counter()
    for seed in SURVEY_SEEDS:
        with graph(w, seed) as p:
            s.graphs += 1
            s.shapes.update(dict.fromkeys(p.shapes, 1))
            s.viewer_shapes.update(p.viewer_shapes)
            s.python_disagrees += p.python_disagrees
            if seed % 8 == 0:  # the correct functions cost as much as the wrong
                for fn in (MODEL, MODEL_2):
                    s.correct_checked[fn] += 1
                    if compare(w.conn, fn, p):
                        s.correct_differs[fn] += 1
            for name in names:
                if s.told_apart[name] >= CAP:
                    continue
                started = time.perf_counter()
                bad = compare(w.conn, _wrong(name), p)
                s.timings[name] = s.timings.get(name, 0.0) + (
                    time.perf_counter() - started
                )
                if bad:
                    s.told_apart[name] += 1
                    s.first_seed.setdefault(name, seed)
                    kinds = s.told_by.setdefault(name, Counter())
                    kinds.update(dict.fromkeys({b.kind for b in bad}, 1))
        if os.environ.get("CURRENT_AMONG_REPORT") and s.graphs % 10 == 0:
            slow = sorted(s.timings.items(), key=lambda kv: -kv[1])[:4]
            print(
                f"graph {s.graphs}: {time.perf_counter() - t0:.1f}s, slowest "
                f"{[(n, round(t, 1)) for n, t in slow]}",
                flush=True,
            )
    if os.environ.get("CURRENT_AMONG_REPORT"):
        print("\ngraphs:", s.graphs, "cap:", CAP)
        print("shapes (graphs having it):", dict(sorted(s.shapes.items())))
        print("viewer shapes:", dict(sorted(s.viewer_shapes.items())))
        for name in names:
            print(
                f"variant {name}: told apart on {s.told_apart[name]} graphs,"
                f" first seed {s.first_seed.get(name)},"
                f" by lists {dict(s.told_by.get(name, {}))}"
            )
    return s


PAIR_SHAPE_NAMES = (
    "same_author",
    "same_author_newer_private",
    "other_author_common_hub",
    "other_author_no_common_hub",
    "other_author_disputed",
    "other_author_common_hub_that_fails_the_rule",
    "older_is_a_hub_other_author_common_hub",
    "older_is_an_instruction_other_author_common_hub",
    "newer_tombstoned",
    "older_tombstoned",
    "newer_not_for_the_hub",
    "dispute_without_dst_node",
    "dispute_from_the_newer_note",
    "link_of_another_rel",
    "dispute_of_another_note",
    *(f"common_hub_failed_only_by_{flaw}" for flaw in FLAWS),
    "chain_of_supersessions",
    "old_note_with_two_newer_notes",
    "cycle_of_supersessions",
    "self_supersession",
)

VIEWER_SHAPE_NAMES = (
    "viewer_hidden_note",
    "viewer_hidden_note_newer_unreadable",
    "viewer_newer_tombstoned",
    "viewer_hidden_by_other_author",
    "viewer_chain_hidden",
    "viewer_cannot_read_some_of_the_graph",
    "viewer_reads_both_and_old_stays",
)

LIST_KINDS = (
    "all",
    "all_reversed_with_ghosts",
    "all_shuffled",
    "subset",
    "few",
    "old_only",
    "old_without_its_hiders",
    "old_without_any_newer_note",
    "newer_only",
    "old_and_newer",
    "old_and_newer_among_unreadable",
    "unreadable",
    "unreadable_and_readable",
    "ghost_ids",
    "real_and_ghost_ids",
    "empty",
    "duplicates",
    "duplicates_few",
    "duplicated_old_only",
    "nulls_around",
    "nulls_between",
    "only_null",
    "null_array",
    "current_only",
    "hidden_only",
    "readable_only",
)

# The lists that hold the older note of a supersession and not the newer one.
TRAP_LISTS = {"old_only", "old_without_its_hiders", "old_without_any_newer_note"}


def test_there_are_many_graphs(survey: Survey) -> None:
    assert survey.graphs >= 100
    assert len(SEEDS) >= 100 or os.environ.get("CURRENT_AMONG_SEEDS")


def test_there_are_at_least_four_accounts(world: World) -> None:
    assert len(world.account_ids) >= 4
    assert NO_SUCH_ACCOUNT not in world.account_ids.values()
    assert NO_SUCH_ACCOUNT in world.viewers
    assert FOREIGN in world.people


@pytest.mark.parametrize("shape", PAIR_SHAPE_NAMES)
def test_generator_covers_every_shape(survey: Survey, shape: str) -> None:
    assert survey.shapes[shape] >= 5, f"fewer than 5 graphs had the shape {shape}"


@pytest.mark.parametrize("shape", VIEWER_SHAPE_NAMES)
def test_generator_covers_every_viewer_shape(survey: Survey, shape: str) -> None:
    assert survey.viewer_shapes[shape] >= 5, f"fewer than 5 viewers had {shape}"


@pytest.mark.parametrize("kind", LIST_KINDS)
def test_generator_covers_every_kind_of_list(survey: Survey, kind: str) -> None:
    assert survey.viewer_shapes[f"list_{kind}"] >= 50, f"few viewers got list {kind}"


def test_the_python_reading_agrees_on_all_graphs(survey: Survey) -> None:
    assert survey.python_disagrees == 0


@pytest.mark.parametrize("fn", [MODEL, MODEL_2])
def test_correct_variants_agree(survey: Survey, fn: str) -> None:
    """Two correct functions written in other shapes (the second takes the ids
    first) are not told apart from the old function on any graph."""
    assert survey.correct_checked[fn] >= 12
    assert survey.correct_differs[fn] == 0, (
        f"{fn} is right and was told apart on {survey.correct_differs[fn]} graphs"
    )


@pytest.mark.parametrize("variant", sorted(WRONG))
def test_wrong_variant_is_told_apart(survey: Survey, variant: str) -> None:
    assert survey.told_apart[variant] > 0, (
        f"the comparison cannot tell {variant} from current_nodes cut to ids"
    )


def test_the_trap_lists_are_what_finds_hiding_by_newer_notes_in_ids(
    survey: Survey,
) -> None:
    """A cut that only lets newer notes in ``ids`` hide is seen by the lists with
    the older note and without the newer, and by no other list that has both."""
    kinds = survey.told_by["hiding_only_by_newer_notes_in_ids"]
    assert sum(kinds[k] for k in TRAP_LISTS) > 0
    for kind in ("all", "all_shuffled", "readable_only", "duplicates"):
        assert kinds[kind] == 0, f"list {kind} holds both notes and cannot see it"


def test_the_empty_list_is_what_finds_the_empty_array_returning_everything(
    survey: Survey,
) -> None:
    assert survey.told_by["empty_array_returns_everything"]["empty"] > 0
    assert survey.told_by["null_array_returns_everything"]["null_array"] > 0
    assert survey.told_by["duplicate_ids_come_back_twice"]["duplicates"] > 0
    assert survey.told_by["a_null_element_empties_the_answer"]["nulls_around"] > 0
