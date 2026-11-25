"""The cost of ``get`` on a big hub in lettered groups. Not part of the suite:
it runs only when asked for, and prints.

    GROUPS_BENCH=1 scripts/test tests/test_groups_bench.py -s

Three hubs of 220, 1,000 and 5,000 notes on one account that has 10,000 more
notes in another hub. Each note's text is a few words of one of 40 subjects;
each has a 384-dimension vector near its subject's, stored as the vector of a
model the bench names (the hash embedder of the tests has no vectors worth
grouping by). Rows committed, ``ANALYZE`` done. Each call is timed ``RUNS``
times, the calls taking turns; with the vectors and with the words only.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable

import numpy as np
import pytest

from reticula_mcp import accounts
from reticula_mcp.accounts import Account
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Conn
from reticula_mcp.search import groups, read
from reticula_mcp.search.handlers import get
from tests.conftest import PASSWORD
from tests.graph_support import mk_hub, mk_note

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GROUPS_BENCH") != "1", reason="a measurement, run on request"
    ),
    pytest.mark.timeout(1800),
]
RUNS = 20
SIZES = (220, 1_000, 5_000)
OTHER_NOTES = 10_000
SUBJECTS = 40
MODEL = "bench-model"


def load(conn: Conn, who: Account, hub: str, count: int, seed: str, tag: str) -> None:
    """``count`` notes in ``hub`` by SQL, borrowing the signed record of
    ``seed``; text from one of ``SUBJECTS`` vocabularies; a vector each."""
    conn.execute(
        """
        WITH s AS (SELECT head_record_id, head_version FROM node WHERE address = %(seed)s),
        h AS (SELECT id FROM node WHERE address = %(hub)s),
        ins AS (
          INSERT INTO node (address, author, author_account_id, kind, head_record_id,
                 head_version, title, text, audience, payload, created_at, updated_at)
          SELECT 'ret:' || %(p)s || '/' || gen_random_uuid()::text, %(p)s, %(a)s,
                 'note', s.head_record_id, s.head_version,
                 'topic' || (g %% %(subjects)s) || ' ' || %(tag)s || g,
                 -- letters only: a word is what groups.words_of takes
                 'alpha' || translate((g %% %(subjects)s)::text, '0123456789', 'bcdfghjklm')
                   || ' beta' || translate((g %% %(subjects)s)::text, '0123456789', 'bcdfghjklm')
                   || ' gamma' || translate(((g * 7) %% %(subjects)s)::text, '0123456789',
                                            'bcdfghjklm')
                   || ' meeting notes today',
                 'hub', '{}'::jsonb,
                 now() - (g || ' seconds')::interval, now() - (g || ' seconds')::interval
          FROM generate_series(1, %(n)s) g, s
          RETURNING id, head_version, title)
        , mem AS (
          INSERT INTO membership (address, node_id, hub_node_id, author, audience,
                                  withdrawn, record_id)
          SELECT 'ret:' || %(p)s || '/' || gen_random_uuid()::text, ins.id, h.id, %(p)s,
                 'hub', false, (SELECT head_record_id FROM s)
          FROM ins, h)
        SELECT ins.id, ins.head_version, ins.title FROM ins
        """,
        {
            "p": who.person_id,
            "a": who.id,
            "seed": seed,
            "hub": hub,
            "n": count,
            "subjects": SUBJECTS,
            "tag": tag,
        },
    )
    rows = conn.execute(
        "SELECT n.id, n.head_version, n.title FROM node n JOIN membership m"
        " ON m.node_id = n.id JOIN node h ON h.id = m.hub_node_id"
        " WHERE h.address = %s",
        (hub,),
    ).fetchall()
    rng = np.random.default_rng(len(rows))
    centres = np.random.default_rng(7).normal(size=(SUBJECTS, 384))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO embedding (node_id, model, dim, vec, version_hash)"
            " VALUES (%s, %s, 384, %s::vector, %s)",
            [
                (
                    r["id"],
                    MODEL,
                    "["
                    + ",".join(
                        f"{v:.5f}"
                        for v in centres[int(r["title"].split()[0][5:])]
                        + rng.normal(size=384) * 0.5
                    )
                    + "]",
                    r["head_version"],
                )
                for r in rows
                if r["title"].startswith("topic")
            ],
        )


def get_part(conn: Conn, viewer: Account, hub: str, letters: str, **kw: object) -> str:
    """A lettered group of a hub: its id, a colon and the letters."""
    return get(conn, viewer, ref=f"{hub}:{letters}", **kw)


_spent: dict[str, list[float]] = {}


def _timed(fn: Callable[..., object], name: str) -> Callable[..., object]:
    def run(*args: object, **kw: object) -> object:
        started = time.perf_counter()
        try:
            return fn(*args, **kw)
        finally:
            _spent.setdefault(name, []).append((time.perf_counter() - started) * 1000)

    return run


def test_cost(fresh_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with connect(fresh_db_url) as conn:
        me = accounts.create_account(conn, "me@example.com", PASSWORD, "mel")
        seed = mk_note(conn, me, "seed")
        other = mk_hub(conn, me, "Elsewhere")
        load(conn, me, other, OTHER_NOTES, seed, "x")
        hubs = {}
        for size in SIZES:
            hubs[size] = mk_hub(conn, me, f"Hub of {size}")
            load(conn, me, hubs[size], size, seed, f"h{size}x")
        conn.commit()
        for table in ("node", "membership", "embedding", "record", "hub_grant"):
            conn.execute(f"ANALYZE {table}")
        conn.commit()

        def case(size: int, part: str | None) -> Callable[[], str]:
            if part is None:
                return lambda: get(conn, me, ref=hubs[size])
            return lambda: get_part(conn, me, hubs[size], part)

        for mode, model in (("vectors", MODEL), ("words only", None)):
            monkeypatch.setattr(groups, "_model", lambda model=model: model)
            cases = {}
            for size in SIZES:
                cases[f"get(hub of {size:,})"] = case(size, None)
                cases[f"get(hub of {size:,}, part='A')"] = case(size, "A")
                if size > 676:
                    cases[f"get(hub of {size:,}, part='AA')"] = case(size, "AA")
            took: dict[str, list[float]] = {name: [] for name in cases}
            for name, call in cases.items():
                call()  # warm
                conn.rollback()
            for _ in range(RUNS):
                for name, call in cases.items():
                    started = time.perf_counter()
                    call()
                    took[name].append((time.perf_counter() - started) * 1000)
                    conn.rollback()
            print(f"\n--- {mode}")
            for name, times in took.items():
                print(
                    f"  {statistics.median(times):7.1f} ms median"
                    f" {max(times):7.1f} worst  {name}"
                )
        monkeypatch.setattr(groups, "_model", lambda: MODEL)
        monkeypatch.setattr(groups, "_model", lambda: MODEL)
        monkeypatch.setattr(groups, "build", _timed(groups.build, "build"))
        monkeypatch.setattr(groups, "load", _timed(groups.load, "load"))
        for size in SIZES:
            _spent.clear()
            for _ in range(5):
                get(conn, me, ref=hubs[size])
                conn.rollback()
            with monkeypatch.context() as flat:
                flat.setattr(read, "_grouped", lambda conn, viewer, top: None)
                plain = []
                for _ in range(5):
                    started = time.perf_counter()
                    get(conn, me, ref=hubs[size])
                    plain.append((time.perf_counter() - started) * 1000)
                    conn.rollback()
            print(
                f"  hub of {size:,}: "
                + ", ".join(
                    f"{k} {statistics.median(v):.1f} ms" for k, v in _spent.items()
                )
                + f"; get without groups {statistics.median(plain):.1f} ms"
            )
        print("\n--- the listing of the hub of 220, as get prints it")
        print(get(conn, me, ref=hubs[220]))
        print("\n--- part='A' of it")
        print(get_part(conn, me, hubs[220], "A"))
