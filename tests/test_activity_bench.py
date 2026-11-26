"""What the activity log costs. Not part of the suite: it runs only when asked
for, and prints.

    ACTIVITY_BENCH=1 scripts/test tests/test_activity_bench.py -s

Three things:

* the cost of one tool call with the log on (``RETICULA_ACTIVITY_DAYS=30``) and
  off (``0``): 500 calls of ``profile`` and of ``get`` of a missing ref through
  ``run`` (``create_mcp``, called in process, no HTTP), the two settings taking
  turns over three rounds; and the cost of ``record`` alone;
* the writer: one INSERT of 200 rows, and rows a second over many batches, on
  real notes (every other row names one, so the statement looks it up);
* the disk size of the rows, per 1,000, over the partitions.

The test database runs with ``fsync=off`` and ``synchronous_commit=off``
(``scripts/test``), so commits cost less here than on a server.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reticula_mcp import accounts, activity
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Database
from reticula_mcp.server import create_mcp
from tests.conftest import PASSWORD
from tests.helpers import make_atom
from tests.test_activity import App, _Verifier

pytestmark = pytest.mark.skipif(
    not os.environ.get("ACTIVITY_BENCH"), reason="set ACTIVITY_BENCH=1 to run"
)

N = 500
ROUNDS = 3


def pct(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def report(label: str, samples: list[float], unit: str = "us") -> None:
    print(
        f"{label}: n={len(samples)} p50={pct(samples, 0.5):.1f} {unit}"
        f" p99={pct(samples, 0.99):.1f} {unit} mean={statistics.mean(samples):.1f} {unit}"
    )


def make(url: str, days: int) -> App:
    settings = Settings(database_url=url, activity_days=days)
    db = Database(url)
    db.open()
    mcp, wiring = create_mcp(settings, db)
    activity.start(db)
    return App(url, db, mcp, activity.log_of(db), _Verifier(wiring.provider))


def test_the_cost_of_one_call_with_the_log_on_and_off(fresh_db_url: str) -> None:
    on, off = make(fresh_db_url, 30), make(fresh_db_url, 0)
    ana = on.person("ana@example.com", "ana")
    token_on, token_off = on.token(ana, "a"), off.token(ana, "b")

    async def timed(
        app: App, access: Any, tool: str, args: dict[str, Any]
    ) -> list[float]:
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        auth_context_var.set(AuthenticatedUser(access))
        for _ in range(20):  # warm up the pool
            await app.mcp.call_tool(tool, args)
        out = []
        for _ in range(N):
            start = time.perf_counter_ns()
            await app.mcp.call_tool(tool, args)
            out.append((time.perf_counter_ns() - start) / 1000)
        return out

    results: dict[tuple[str, str], list[float]] = {}
    for tool, args in (("profile", {}), ("get", {"ref": "deadbeef"})):
        for _ in range(ROUNDS):
            for name, app, access in (("on", on, token_on), ("off", off, token_off)):
                got = asyncio.run(timed(app, access, tool, args))
                results.setdefault((tool, name), []).extend(got)
    for (tool, name), samples in results.items():
        report(f"{tool:8s} log {name:3s}", samples, "us")
    on.log and print(f"on: written={on.log.written} dropped={on.log.dropped}")

    log = activity.ActivityLog(on.db, 30, queue_size=200_000)
    log._running = True  # no thread: only the put
    put = []
    for _ in range(100_000):
        start = time.perf_counter_ns()
        log.record(1, "grant:abc", "c_4f2a9e1bxxxx", "get", "ok", ("542b1dbc",))
        put.append((time.perf_counter_ns() - start) / 1000)
    report("record() alone", put, "us")
    for app in (on, off):
        activity.stop(app.db)
        app.db.close()


def test_the_writer_and_the_size_of_a_row(
    fresh_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    midnight = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC)
    people = []
    notes: dict[int, list[str]] = {}
    with connect(fresh_db_url) as conn:
        for n in range(5):
            account = accounts.create_account(
                conn, f"p{n}@example.com", PASSWORD, f"person{n}"
            )
            people.append(account)
            notes[account.id] = [
                make_atom(conn, account, f"note {i}")["address"] for i in range(100)
            ]
            conn.execute(
                "INSERT INTO oauth_client (client_id, client_info) VALUES (%s, %s::jsonb)",
                (f"c_{n}f2a9e1b{n}{n}{n}{n}{n}", '{"client_name": "Claude"}'),
            )
        activity.maintain_in(conn, 30)
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
    now = max(datetime.now(UTC), midnight + timedelta(seconds=3_100))
    entries: list[activity.Entry] = []
    for i in range(20_000):
        who = people[i % 5]
        mine = notes[who.id][i % 100]
        entries.append(
            (
                now - timedelta(seconds=(i * 3) % 3_000),
                who.id,
                f"grant:{i % 7:032x}",
                f"c_{i % 5}f2a9e1b{i % 5}{i % 5}{i % 5}{i % 5}{i % 5}",
                ("get", "search", "put", "edit", "profile")[i % 5],
                "ok" if i % 17 else "NotFound",
                (mine[-8:],) if i % 2 else (),
            )
        )
    one_batch: list[float] = []
    db = Database(fresh_db_url)
    db.open()
    started = time.perf_counter()
    for at in range(0, len(entries), 200):
        batch = entries[at : at + 200]
        t0 = time.perf_counter()
        with db.connection() as conn:
            activity.write_batch(conn, batch)
        one_batch.append((time.perf_counter() - t0) * 1000)
    total = time.perf_counter() - started
    db.close()
    report("INSERT of 200 rows (half name a note)", one_batch, "ms")
    print(f"writer: {len(entries) / total:.0f} rows/s over {len(one_batch)} batches")
    with connect(fresh_db_url) as conn:
        rows = conn.execute("SELECT count(*) AS n FROM activity").fetchone()["n"]
        size = conn.execute(
            "SELECT sum(pg_total_relation_size(i.inhrelid)) AS bytes"
            " FROM pg_inherits i WHERE i.inhparent = 'activity'::regclass"
        ).fetchone()["bytes"]
        parent = conn.execute(
            "SELECT pg_total_relation_size('activity') AS b"
        ).fetchone()["b"]
        print(f"rows={rows} partitions={size} bytes parent={parent}")
        print(f"disk per 1,000 rows: {size / rows * 1000 / 1024:.1f} KiB")
        with_ref = conn.execute(
            "SELECT count(*) AS n FROM activity WHERE ref IS NOT NULL"
        ).fetchone()["n"]
        print(f"rows with a ref: {with_ref}")
    del monkeypatch
