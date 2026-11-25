"""The cost of ``search(since=...)`` and of the block in ``start`` on a large
account. Not part of the suite: it runs only when asked for, and prints.

    RECENT_BENCH=1 scripts/test tests/test_recent_bench.py -s

The account: 30,000 notes in 20 hubs. Ten hubs are the viewer's own with
15,000 notes of theirs; ten belong to five other people, were shared with the
viewer 400 days ago, and hold 15,000 notes of those people. Notes are spread
evenly over the last 730 days, so about 290 of them fall into the last seven;
one note in twenty was edited later. ``REST`` adds as many notes again that the
viewer cannot read (other people's hubs, not shared): the rest of an instance.

What the planner knew: the rows are committed in a ``fresh_db_url`` database
and ``ANALYZE`` ran on every table read here before the first measurement, as
on a server in use. Every note has its two log rows (the note, its filing), so
``record`` has the size it would have; their envelopes are filler bytes.

Migration 0350: its indexes are dropped after the load, the two ``since``
calls are timed without them, then each statement of the migration file is
run and timed (the build time on a database of this size), and everything is
timed with them.

How it is timed: the calls of one group take turns, ``RUNS`` rounds, so that
whatever else the machine does in that time falls on all of them alike. Under
each call stand its statements that take a millisecond or more (median of the
time each took in a call), so a number can be traced to the statement behind
it. The block in ``start`` is timed inside ``start`` itself.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from reticula_mcp import accounts
from reticula_mcp.accounts import Account
from reticula_mcp.content import handlers as content
from reticula_mcp.db import connect
from reticula_mcp.db.migrate import migrations_dir
from reticula_mcp.db.pool import Conn
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import recent
from reticula_mcp.search.handlers import search
from tests.conftest import PASSWORD
from tests.graph_support import addr, node
from tests.test_recent_support import Clock

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RECENT_BENCH") != "1", reason="a measurement, run on request"
    ),
    pytest.mark.timeout(1800),
]
#: RECENT_BENCH_PLANS=1 prints the plan of every statement slower than 20 ms.
PLANS = os.environ.get("RECENT_BENCH_PLANS") == "1"

HUBS = 20  # half the viewer's own, half shared with them
PER_HUB = 1_500  # notes in each
OTHERS = 5  # people who own the shared hubs, two each
REST = int(os.environ.get("RECENT_BENCH_REST", "0"))  # unreadable notes on top
RUNS = 40
TABLES = ("node", "membership", "record", "hub_grant", "supersedes", "account")
FILLER = b"x" * 600  # about the size of a signed envelope
MIGRATION = "0350_recent_events.sql"
INDEXES = ("node_updated_idx", "record_member_created_idx", "membership_record_idx")


def load(conn: Conn, who: Account, hub_node_id: int, count: int) -> None:
    """``count`` notes of ``who`` in one hub: the node, its two log rows and
    its filing, as ``put(hub=...)`` leaves them, without the signing."""
    conn.execute(
        """
        WITH src AS MATERIALIZED (
            SELECT g,
                   'ret:' || %(p)s || '/' || gen_random_uuid()::text AS address,
                   'ret:' || %(p)s || '/' || gen_random_uuid()::text AS filing,
                   now() - random() * interval '730 days' AS at,
                   random() < 0.05 AS edited
            FROM generate_series(1, %(count)s) g
        ), atom AS (
            INSERT INTO record (version_hash, address, type, author, key_id,
                   created_at, audience, content_hash, format_version, envelope,
                   signature)
            SELECT 'bench:' || s.address, s.address, 'atom', %(p)s, 'k1', s.at,
                   'hub', 'bench', 0, %(filler)s, %(filler)s
            FROM src s
            RETURNING id, address
        ), member AS (
            INSERT INTO record (version_hash, address, type, author, key_id,
                   created_at, audience, content_hash, format_version, envelope,
                   signature)
            SELECT 'bench:' || s.filing, s.filing, 'member', %(p)s, 'k1',
                   s.at + interval '2 milliseconds', 'hub', 'bench', 0,
                   %(filler)s, %(filler)s
            FROM src s
            RETURNING id, address
        ), nodes AS (
            INSERT INTO node (address, author, author_account_id, kind,
                   head_record_id, head_version, title, text, audience,
                   created_at, updated_at)
            SELECT s.address, %(p)s, %(a)s, 'note', atom.id, 'bench:' || s.address,
                   'Note ' || s.g || ' of hub ' || %(hub)s,
                   repeat('What was measured on run ' || s.g || ' and why. ', 8),
                   'hub', s.at,
                   CASE WHEN s.edited THEN s.at + random() * (now() - s.at)
                        ELSE s.at END
            FROM src s JOIN atom ON atom.address = s.address
            RETURNING id, address
        )
        INSERT INTO membership (address, node_id, hub_node_id, author, audience,
                                withdrawn, record_id, created_at)
        SELECT s.filing, nodes.id, %(hub)s, %(p)s, 'hub', false, member.id,
               s.at + interval '2 milliseconds'
        FROM src s
        JOIN nodes ON nodes.address = s.address
        JOIN member ON member.address = s.filing
        """,
        {
            "p": who.person_id,
            "a": who.id,
            "hub": hub_node_id,
            "count": count,
            "filler": FILLER,
        },
    )


def hub_id(conn: Conn, owner: Account, title: str) -> int:
    made = addr(graph.put(conn, owner, text=title, kind="hub", title=title))
    return int(node(conn, made)["id"])


def build(conn: Conn, clock: Clock) -> tuple[Account, list[int]]:
    """The account described at the top. Returns the viewer and their hubs."""
    me = accounts.create_account(conn, "me@example.com", PASSWORD, "mel")
    people = [
        accounts.create_account(conn, f"p{i}@example.com", PASSWORD, f"per{i}")
        for i in range(OTHERS)
    ]
    mine: list[int] = []
    shared: list[tuple[Account, int]] = []
    unshared: list[tuple[Account, int]] = []
    with clock.ago(days=800):
        mine = [hub_id(conn, me, f"Mine {i}") for i in range(HUBS // 2)]
        for i in range(HUBS // 2):
            owner = people[i % OTHERS]
            shared.append((owner, hub_id(conn, owner, f"Shared {i}")))
            if REST:
                unshared.append((owner, hub_id(conn, owner, f"Closed {i}")))
    with clock.ago(days=400):
        for owner, hub in shared:
            ref = conn.execute(
                "SELECT address FROM node WHERE id = %s", (hub,)
            ).fetchone()
            assert ref is not None
            graph.share(conn, owner, hub=ref["address"], person="@mel", mode="read")
    for hub in mine:
        load(conn, me, hub, PER_HUB)
    for owner, hub in shared:
        load(conn, owner, hub, PER_HUB)
    for owner, hub in unshared:
        load(conn, owner, hub, REST // len(unshared))
    conn.commit()
    return me, [*mine, *(hub for _, hub in shared)]


Call = Callable[[Conn], Any]


class Probe:
    """Stands in for the connection and keeps the time each statement took."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn
        self.spent: dict[str, float] = {}

    def execute(self, query: Any, params: Any = None, **kw: Any) -> Any:
        started = time.perf_counter()
        try:
            return self._conn.execute(query, params, **kw)
        finally:
            took = (time.perf_counter() - started) * 1000
            key = " ".join(str(query).split())[:100]
            self.spent[key] = self.spent.get(key, 0.0) + took

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def explained(conn: Conn, call: Call) -> None:
    """Run ``call`` once with auto_explain on and print the plans it logs."""
    notes: list[str] = []
    conn.add_notice_handler(lambda d: notes.append(d.message_primary or ""))
    conn.execute("LOAD 'auto_explain'")
    conn.execute("SET auto_explain.log_min_duration = '20ms'")
    conn.execute("SET auto_explain.log_analyze = on")
    conn.execute("SET auto_explain.log_nested_statements = on")
    conn.execute("SET client_min_messages = log")
    call(conn)
    conn.rollback()
    conn.execute("SET auto_explain.log_min_duration = -1")
    conn.execute("SET client_min_messages = notice")
    conn.commit()
    for note in notes:
        print(note)


def analyze(conn: Conn) -> None:
    for table in TABLES:
        conn.execute(f"ANALYZE {table}")
    conn.commit()


def middle(times: list[float]) -> float:
    return round(statistics.median(times), 1)


def report(conn: Conn, cases: dict[str, Call]) -> None:
    """Median and worst time of each call in milliseconds over ``RUNS`` rounds
    in which the calls take turns, each in its own transaction as on the
    server, after one round to warm up; and its statements of 1 ms or more."""
    total: dict[str, list[float]] = {name: [] for name in cases}
    parts: dict[str, list[dict[str, float]]] = {name: [] for name in cases}
    first: dict[str, str] = {}
    for name, call in cases.items():
        if PLANS:
            print(f"--- plans of {name}")
            explained(conn, call)
        call(conn)
        conn.rollback()
    for _ in range(RUNS):
        for name, call in cases.items():
            probe = Probe(conn)
            started = time.perf_counter()
            out = call(cast(Conn, probe))
            total[name].append((time.perf_counter() - started) * 1000)
            conn.rollback()
            parts[name].append(probe.spent)
            first[name] = str(out).splitlines()[0][:110] if isinstance(out, str) else ""
    for name in cases:
        print(
            f"{middle(total[name]):8.1f} ms median {max(total[name]):8.1f} ms worst"
            f"  {name}" + (f"\n    {first[name]}" if first[name] else "")
        )
        seen = {key for run in parts[name] for key in run}
        each = {key: middle([run.get(key, 0.0) for run in parts[name]]) for key in seen}
        small = [took for took in each.values() if took < 1.0]
        for key, took in sorted(each.items(), key=lambda item: -item[1]):
            if took >= 1.0:
                print(f"    {took:8.1f} ms  {key}")
        inside = middle([sum(run.values()) for run in parts[name]])
        print(
            f"    {round(sum(small), 1):8.1f} ms  {len(small)} more statements\n"
            f"    {round(middle(total[name]) - inside, 1):8.1f} ms  not in a statement",
            flush=True,
        )


def build_indexes(conn: Conn) -> None:
    """Run each statement of the migration file and print how long it took."""
    text = (migrations_dir() / MIGRATION).read_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    for statement in (part.strip() for part in code.split(";")):
        if not statement:
            continue
        started = time.perf_counter()
        conn.execute(statement)
        conn.commit()
        took = (time.perf_counter() - started) * 1000
        print(f"{took:8.1f} ms  {' '.join(statement.split())}", flush=True)


def test_cost(fresh_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    with connect(fresh_db_url) as conn:
        me, hubs = build(conn, clock)
        shared_hub = conn.execute(
            "SELECT address FROM node WHERE id = %s", (hubs[-1],)
        ).fetchone()
        assert shared_hub is not None
        counted = conn.execute(
            "SELECT count(*) AS n FROM node WHERE updated_at >= now() - interval"
            " '7 days'"
        ).fetchone()
        assert counted is not None
        print(
            f"\n{HUBS * PER_HUB} notes in {HUBS} hubs, {REST} more unreadable;"
            f" changed in the last 7 days on the instance: {counted['n']}"
        )

        def news(c: Conn) -> int:
            since = datetime.now(UTC) - timedelta(days=recent.NEWS_DAYS)
            return len(recent.news(c, me, since=since).counts)

        week: dict[str, Call] = {
            'search(query="", since="7d")': lambda c: search(
                c, me, query="", since="7d"
            ),
            'search(query="", since="7d", hub=<shared hub>)': lambda c: search(
                c, me, query="", since="7d", hub=shared_hub["address"]
            ),
            "recent.news(7 days): the block's reads, and the hubs start has": news,
        }
        for index in INDEXES:
            conn.execute(f"DROP INDEX {index}")
        conn.commit()
        analyze(conn)
        print("--- without the indexes of 0350")
        report(conn, week)
        print(f"--- {MIGRATION}, statement by statement")
        build_indexes(conn)
        analyze(conn)
        print("--- with them")
        report(conn, week)
        report(
            conn,
            {
                'search(query="", since="365d")': lambda c: search(
                    c, me, query="", since="365d"
                ),
                'search(query="", kind="note") [the listing today]': lambda c: search(
                    c, me, query="", kind="note"
                ),
            },
        )

        block: list[float] = []
        lines = content._news_lines

        def timed_block(c: Conn, viewer: Account, rows: list[Any]) -> list[str]:
            started = time.perf_counter()
            out = lines(c, viewer, rows)
            block.append((time.perf_counter() - started) * 1000)
            return out

        def without(c: Conn) -> str:
            with monkeypatch.context() as patch:
                patch.setattr(content, "_news_lines", lambda c, viewer, rows: [])
                return content.start(c, me)

        monkeypatch.setattr(content, "_news_lines", timed_block)
        report(
            conn,
            {
                "start()": lambda c: content.start(c, me),
                "start() without the block": without,
            },
        )
        timed = block[-RUNS:]
        print(
            f"{middle(timed):8.1f} ms median {max(timed):8.1f} ms worst"
            "  the block, timed inside start()"
        )
        print("--- the block as start printed it")
        for line in content.start(conn, me).splitlines():
            if line.startswith(("New from other people", "Newly shared with you")):
                print(line)
        conn.rollback()
