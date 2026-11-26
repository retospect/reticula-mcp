"""The person's own activity log: one row per tool call, kept for a number of days.

A row says what the CALLER did and nothing else (migration 0357)::

    at          when the call ended (UTC)
    account_id  the account that made the call, never the owner of what it read
    connection  the connection key (connection.py): grant:<id> or token:<id>
    client      "Claude (c_4f2a9e1b)": the name the connector registered, or the
                label of an operator token, cut to 60 characters, then the start
                of the client id that the request log prints as ``client=``
    tool        the tool's name
    outcome     ``ok``, or the error code of the call (``NotFound``, ...)
    ref         the short id of the note or hub the call named, only when the
                caller wrote it; else NULL

Never a query, a note text, a reply, a handle, or the address of another person.
No row is ever written about who read a person's notes.

How it stays off the call path. ``server.run`` puts one small tuple on a bounded
queue after the handler returned or raised (``ActivityLog.record``: a
``put_nowait``; a full queue drops the row and counts it). One daemon thread
takes what is queued every second or at 200 rows and writes it with ONE
statement on a pooled connection. That statement also looks up the client name
and checks, for the one reference of each row, that the caller wrote the note,
so neither is paid for in the call. A failed write drops its batch and logs one
warning a minute; a caller never sees it.

Partitions: one per UTC day, ``activity_YYYYMMDD``. At start and then every
hour the thread makes today's and the next two days' (a row is never written to
a day that has none), and drops the partitions whose day ended more than
``RETICULA_ACTIVITY_DAYS`` days ago. Retention never deletes a row. The
database role of the server owns the table, and an owner may create and drop
its own partitions: no superuser right is needed. With 0 days the log is off:
nothing is queued, written or made, and nothing that exists is touched.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import weakref
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg import sql

from reticula_mcp.db.pool import Conn, Database
from reticula_mcp.format.address import ADDRESS_SCHEME
from reticula_mcp.requestlog import one_line

log = logging.getLogger("reticula_mcp.activity")

QUEUE_SIZE = 10_000
BATCH_ROWS = 200  # a batch is written when this many are waiting ...
FLUSH_SECONDS = 1.0  # ... or after this long
MAINTAIN_SECONDS = 3600.0
WARN_SECONDS = 60.0  # at most one warning line per this long
LOOKAHEAD_DAYS = 2  # partitions made ahead of today
CLIENT_NAME_MAX = 60
CLIENT_PREFIX = 10  # as the request log's client=
LOCK_KEY = 0x61637476  # one process maintains the partitions at a time
LOCK_WAIT = "5s"  # DDL gives up when it cannot get its lock (a retry follows)
LIMIT_MAX = 500
WEB = "web"  # the connection of an event on the web page
EVENT_MAX = 40

_PARTITION_RE = re.compile(r"^activity_(\d{4})(\d{2})(\d{2})$")
_ADDRESS_RE = re.compile(r"^ret:p[a-z2-7]{26}/[0-9a-f-]{36}")
_SUFFIX_RE = re.compile(r"^([0-9a-f-]{6,60})(?::.*)?$", re.S)
_SPLIT_RE = re.compile(r"[\s,]+")
#: the parameters of a tool call that name a note or hub, in the order the
#: first of them that does is the one the row can carry
REF_PARAMS = ("ref", "hub", "to")
REF_MAX = 200  # characters of a parameter taken (resolve.MAX_REF)

#: (ended at, account id, connection key, client id, tool, outcome, the ref /
#: hub / to arguments that were given, in that order)
Entry = tuple[datetime, int, str | None, str | None, str, str, tuple[str, ...]]


# Accounts whose rows were erased in this process (``erase``): nothing more is
# queued or written for them, whatever is still on the queue. Account ids are
# never reused, so the set only grows by one per deleted account.
_erased: set[int] = set()
_erased_lock = threading.Lock()


def erase(conn: Conn, account_id: int) -> None:
    """Delete every row of the account, in the caller's transaction (the
    caller commits), and stop logging for it: what is already queued, in this
    process, is dropped by the writer. The account is not deleted by this; a
    row from another process is kept out by the writer's join on a disabled
    account.

    The id is marked before the DELETE and stays marked if the caller's
    transaction rolls back: this process then logs nothing for the account until
    it restarts. Accepted (RETICULA1): an account deletion that is rolled back is
    rare, and the cost is a gap in a log, never a row that should be gone."""
    with _erased_lock:
        _erased.add(account_id)
    conn.execute("DELETE FROM activity WHERE account_id = %s", (account_id,))


def partition_name(day: date) -> str:
    return f"activity_{day:%Y%m%d}"


def _day_of(name: str) -> date | None:
    found = _PARTITION_RE.match(name)
    if not found:
        return None
    try:
        return date(*(int(g) for g in found.groups()))
    except ValueError:
        return None


def _flat(text: str, limit: int) -> str:
    """Printable ASCII on one line, as the request log writes ``client=``."""
    return one_line(text, limit)


def parse_ref(text: str) -> tuple[str | None, str | None]:
    """What one argument names, as ``(address, suffix)``: a full address, or the
    last 6+ hex characters of an id. Both None when it names no note: a word
    handle, a person, anything else. Only values of these two shapes ever reach
    the database; the argument itself is never stored. The forms ``<id>:A`` (a
    group of a hub) and ``<id>:yes-<code>`` (a delete) carry the id before
    the colon, and several refs in one argument name the first."""
    parts = _SPLIT_RE.split(text.strip()[:REF_MAX])
    token = parts[0] if parts else ""
    if token.startswith(ADDRESS_SCHEME):  # exact, as resolve_ref has it
        found = _ADDRESS_RE.match(token)
        return (found.group(0), None) if found else (None, None)
    found = _SUFFIX_RE.match(token.lower())  # a suffix is lowered there
    if found and len(found.group(1).replace("-", "")) >= 6:
        return None, found.group(1)
    return None, None


def _refs(refs: Iterable[str]) -> list[tuple[str | None, str | None]]:
    """Every argument of ref, hub and to (in that order) that names a note, as
    parse_ref has it. The writer keeps the first that is the caller's own."""
    found = []
    for text in refs:
        address, suffix = parse_ref(text)
        if address or suffix:
            found.append((address, suffix))
    return found


# One statement for a whole batch. `a` is the caller (an account that is gone,
# or disabled as a deleted account is, is skipped, not an error); `n` the client
# name; `r` the short id of the first argument of ref, hub and to that names
# exactly one note whose author is the caller. The candidates of every row come
# in arrays of their own (`c`, with the row's number and their position). A
# suffix is looked up as resolve.ends_with_sql does, through the index
# node_address_tail.
INSERT_SQL = """
INSERT INTO activity (at, account_id, connection, client, tool, outcome, ref)
SELECT v.at, v.account_id, v.connection,
       nullif(concat_ws(' ', nullif(n.name, ''), '(' || v.short || ')'), ''),
       v.tool, v.outcome, r.short
FROM unnest(
        %(at)s::timestamptz[], %(account)s::bigint[], %(connection)s::text[],
        %(short)s::text[], %(client)s::text[], %(token)s::bigint[],
        %(tool)s::text[], %(outcome)s::text[]
     ) WITH ORDINALITY AS v(at, account_id, connection, short, client_id,
                            token_id, tool, outcome, rn)
JOIN account a ON a.id = v.account_id AND a.status <> 'disabled'
CROSS JOIN LATERAL (
    SELECT left(btrim(regexp_replace(coalesce(
        CASE WHEN v.token_id IS NOT NULL
             THEN (SELECT t.label FROM oauth_token t
                    WHERE t.id = v.token_id AND t.account_id = v.account_id)
             ELSE (SELECT c.client_info->>'client_name' FROM oauth_client c
                    WHERE c.client_id = v.client_id)
        END, ''), '[[:cntrl:][:space:]]+', ' ', 'g')), %(name_max)s) AS name
) n
LEFT JOIN LATERAL (
    SELECT p.short FROM (
        SELECT c.pos, min(right(m.address, 8)) AS short
        FROM unnest(
                %(c_rn)s::bigint[], %(c_pos)s::int[], %(c_addr)s::text[],
                %(c_tail)s::text[], %(c_from)s::text[], %(c_to)s::text[]
             ) AS c(rn, pos, addr, tail, tail_from, tail_to)
        CROSS JOIN LATERAL (
            SELECT x.address FROM node x
             WHERE c.addr IS NOT NULL AND x.address = c.addr
               AND x.author = a.person_id
            UNION ALL
            SELECT x.address FROM node x
             WHERE c.tail IS NOT NULL
               AND reverse(x.address) ~>=~ c.tail_from
               AND reverse(x.address) ~<~ c.tail_to
               AND right(x.address, length(c.tail)) = c.tail
               AND x.author = a.person_id
            LIMIT 2
        ) m
        WHERE c.rn = v.rn
        GROUP BY c.pos
        HAVING count(*) = 1
    ) p
    ORDER BY p.pos
    LIMIT 1
) r ON true
"""


def _arrays(entries: Sequence[Entry]) -> dict[str, Any]:
    """The columns of a batch, one array each (the ``unnest`` of INSERT_SQL)."""
    cols: dict[str, list[Any]] = {
        name: []
        for name in (
            ("at", "account", "connection", "short", "client", "token")
            + ("tool", "outcome")
            + ("c_rn", "c_pos", "c_addr", "c_tail", "c_from", "c_to")
        )
    }
    for rn, (at, account_id, conn_key, client_id, tool, outcome, refs) in enumerate(
        entries, start=1
    ):
        key = _flat(conn_key, 100) if conn_key else None
        client = _flat(client_id, 200) if client_id else None
        token_id = None
        if key and key.startswith("token:") and key[6:].isdigit():
            token_id = int(key[6:])
        cols["at"].append(at)
        cols["account"].append(account_id)
        cols["connection"].append(key)
        cols["short"].append(client[:CLIENT_PREFIX] if client else None)
        cols["client"].append(client)
        cols["token"].append(token_id)
        cols["tool"].append(_flat(tool, 60))
        cols["outcome"].append(_flat(outcome, 60))
        for pos, (address, suffix) in enumerate(_refs(refs)):
            low = suffix[::-1] if suffix else None
            cols["c_rn"].append(rn)
            cols["c_pos"].append(pos)
            cols["c_addr"].append(address)
            cols["c_tail"].append(suffix)
            cols["c_from"].append(low)
            cols["c_to"].append(low[:-1] + chr(ord(low[-1]) + 1) if low else None)
    return {**cols, "name_max": CLIENT_NAME_MAX}


def write_batch(conn: Conn, entries: Sequence[Entry]) -> int:
    """Write ``entries`` with one statement; the number of rows written."""
    if not entries:
        return 0
    return conn.execute(INSERT_SQL, _arrays(entries)).rowcount


def create_partition(conn: Conn, day: date) -> bool:
    """Make the partition of ``day`` (UTC) unless it exists; True when made."""
    name = partition_name(day)
    found = conn.execute("SELECT to_regclass(%s) AS found", (name,)).fetchone()
    if found and found["found"]:
        return False
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} PARTITION OF activity"
            " FOR VALUES FROM ({}) TO ({})"
        ).format(
            sql.Identifier(name),
            sql.Literal(start.isoformat()),
            sql.Literal((start + timedelta(days=1)).isoformat()),
        )
    )
    return True


def maintain_in(
    conn: Conn, days: int, now: datetime | None = None
) -> tuple[int, int] | None:
    """Make the partitions of today and the next two days when they are
    missing and drop those whose day ended more than ``days`` days ago, in the
    transaction of ``conn``. Returns ``(made, dropped)``; None when another
    process holds the advisory lock, which is at it (one at a time)."""
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    conn.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT}'")
    got = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_KEY,)
    ).fetchone()
    if not got or not got["ok"]:
        return None
    have = [
        r["name"]
        for r in conn.execute(
            "SELECT c.relname AS name FROM pg_inherits i"
            " JOIN pg_class c ON c.oid = i.inhrelid"
            " WHERE i.inhparent = to_regclass('activity') ORDER BY c.relname"
        ).fetchall()
    ]
    made = dropped = 0
    for ahead in range(LOOKAHEAD_DAYS + 1):
        made += create_partition(conn, today + timedelta(days=ahead))
    for name in have:
        day = _day_of(name)
        if day is None:
            continue
        ended = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
        if now - ended > timedelta(days=days):
            conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(name)))
            dropped += 1
    return made, dropped


def maintain(db: Database, days: int, now: datetime | None = None) -> None:
    """:func:`maintain_in` in a transaction of its own. DDL waits for its lock
    at most ``LOCK_WAIT``; then this raises, and the next hour tries again."""
    with db.connection() as conn:
        maintain_in(conn, days, now)


class ActivityLog:
    """The queue, the writer thread and the counters of one server.

    ``written`` and ``dropped`` count rows for tests and the log: ``dropped``
    are rows that found the queue full or whose batch could not be written.
    """

    def __init__(
        self,
        db: Database,
        days: int,
        *,
        queue_size: int | None = None,
        batch_rows: int | None = None,
        flush_seconds: float | None = None,
        maintain_seconds: float | None = None,
    ) -> None:
        # the module's values are read now, not when the class was defined
        self.db = db
        self.days = days
        self.batch_rows = batch_rows or BATCH_ROWS
        self.flush_seconds = FLUSH_SECONDS if flush_seconds is None else flush_seconds
        self.maintain_seconds = (
            MAINTAIN_SECONDS if maintain_seconds is None else maintain_seconds
        )
        self._queue: queue.Queue[Entry | None] = queue.Queue(
            maxsize=queue_size or QUEUE_SIZE
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._count = threading.Lock()
        self._accepted = 0  # rows queued
        self._settled = 0  # queued rows written, skipped or dropped since
        self._last_warning = -WARN_SECONDS
        self._unreported = 0  # rows dropped since the last warning line
        self.written = 0
        self.dropped = 0

    # ---------------------------------------------------------------- call side

    def record(
        self,
        account_id: int,
        conn_key: str | None,
        client_id: str | None,
        tool: str,
        outcome: str,
        refs: tuple[str, ...],
    ) -> None:
        """Queue one row. Never blocks and never raises."""
        if not self._running or account_id in _erased:
            return
        try:
            self._queue.put_nowait(
                (
                    datetime.now(UTC),
                    account_id,
                    conn_key,
                    client_id,
                    tool,
                    outcome,
                    refs,
                )
            )
            with self._count:
                self._accepted += 1
        except queue.Full:
            with self._count:
                self.dropped += 1
            self._warn("the activity queue is full", 1)
        except Exception:  # a log must never fail a call
            with self._count:
                self.dropped += 1

    # -------------------------------------------------------------- thread side

    def start(self) -> None:
        """Make today's partitions, then start the thread. Never raises."""
        if self._thread is not None:
            return
        self._maintain()
        self._stop.clear()
        self._running = True
        _set_active(self)
        self._thread = threading.Thread(
            target=self._run, name="reticula-activity-writer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop taking rows, write what is queued, end the thread."""
        self._running = False
        _set_active(None, only=self)
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # wakes the thread
        except queue.Full:
            pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until everything queued so far has been written or dropped
        (for tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._settled >= self._accepted:
                return True
            time.sleep(0.01)
        return False

    def _maintain(self) -> None:
        try:
            maintain(self.db, self.days)
        except Exception as exc:
            self._warn(f"partitions not kept ({type(exc).__name__}: {exc})", 0)

    def _collect(self) -> list[Entry]:
        """Up to ``batch_rows`` entries, waiting at most ``flush_seconds``."""
        rows: list[Entry] = []
        end = time.monotonic() + self.flush_seconds
        while len(rows) < self.batch_rows and not self._stop.is_set():
            wait = end - time.monotonic()
            if wait <= 0:
                break
            try:
                item = self._queue.get(timeout=wait)
            except queue.Empty:
                break
            if item is not None:
                rows.append(item)
        return rows

    def _drain(self) -> list[Entry]:
        rows: list[Entry] = []
        while len(rows) < self.batch_rows:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                rows.append(item)
        return rows

    def _write(self, rows: list[Entry]) -> None:
        if not rows:
            return
        queued = len(rows)
        try:
            rows = [r for r in rows if r[1] not in _erased]
            if rows:
                with self.db.connection() as conn:
                    self.written += write_batch(conn, rows)
        except Exception as exc:
            with self._count:
                self.dropped += len(rows)
            self._warn(
                f"a batch was not written ({type(exc).__name__}: {exc})", len(rows)
            )
        finally:
            self._settled += queued

    def _run(self) -> None:
        next_maintenance = time.monotonic() + self.maintain_seconds
        while not self._stop.is_set():
            self._write(self._collect())
            if time.monotonic() >= next_maintenance:
                self._maintain()
                next_maintenance = time.monotonic() + self.maintain_seconds
        while True:  # stopped: what is queued is still written
            rows = self._drain()
            if not rows:
                break
            self._write(rows)

    def _warn(self, what: str, rows: int) -> None:
        self._unreported += rows
        now = time.monotonic()
        if now - self._last_warning < WARN_SECONDS:
            return
        self._last_warning = now
        log.warning(
            "activity log: %s; %d rows dropped since the last warning, %d written,"
            " %d dropped in all",
            what,
            self._unreported,
            self.written,
            self.dropped,
        )
        self._unreported = 0


# The log of each Database that a server was built on, so that the app's
# lifespan finds the one ``create_mcp`` made (and a test finds its counters).
_LOGS: weakref.WeakKeyDictionary[Database, ActivityLog] = weakref.WeakKeyDictionary()


_active: ActivityLog | None = None


def _set_active(found: ActivityLog | None, only: ActivityLog | None = None) -> None:
    """The log that ``record`` (the module's function) queues into: the one
    started last in this process; cleared when that one stops."""
    global _active
    if found is not None or only is None or _active is only:
        _active = found


def record(account_id: int, event: str, *, outcome: str = "ok") -> None:
    """Queue one row for an event on the web page, not through a tool: the
    event text (cut to 40 characters) is the tool, the connection is ``web``,
    there is no client and no ref. Does nothing when the log is off or not
    started; never raises."""
    try:
        found = _active
        if found is not None:
            found.record(account_id, WEB, None, event[:EVENT_MAX], outcome, ())
    except Exception:
        pass


def create(db: Database, days: int) -> ActivityLog | None:
    """The log for ``db``, or None when ``days`` is 0 (the log is off)."""
    if days <= 0:
        _LOGS.pop(db, None)
        return None
    found = _LOGS.get(db)
    if found is not None and found.days == days:
        return found
    found = ActivityLog(db, days)
    _LOGS[db] = found
    return found


def log_of(db: Database) -> ActivityLog | None:
    return _LOGS.get(db)


def start(db: Database) -> None:
    """Start the log of ``db``, if it has one. Never raises."""
    found = _LOGS.get(db)
    if found is not None:
        try:
            found.start()
        except Exception:
            log.warning("activity log did not start", exc_info=True)


def stop(db: Database) -> None:
    """Flush and stop the log of ``db``, if it has one. Never raises."""
    found = _LOGS.get(db)
    if found is not None:
        try:
            found.stop()
        except Exception:
            log.warning("activity log did not stop cleanly", exc_info=True)


# ------------------------------------------------------------------------ reading


def lines(
    conn: Conn,
    account_id: int,
    *,
    limit: int = 50,
    before: datetime | None = None,
) -> list[str]:
    """The account's own rows, newest first, as plain text lines::

        2026-11-26 12:03 UTC · Claude (c_4f2a9e1b) · put · ok · 542b1dbc

    ``limit`` is clamped to 1..500; ``before`` keeps rows that ended before it.
    The ref part is left out when there is none, the client part reads
    ``unknown client`` when it is unknown (an erased account has none); an event of the web page (see
    :func:`record`) shows ``web`` there. Plain text: whatever shows it
    escapes it.
    """
    if account_id in _erased:
        return []
    limit = max(1, min(int(limit), LIMIT_MAX))
    columns = (
        "SELECT to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') AS moment,"
        " client, connection, tool, outcome, ref FROM activity"
        " WHERE account_id = %s"
    )
    if before is None:
        rows = conn.execute(
            columns + " ORDER BY at DESC LIMIT %s", (account_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            columns + " AND at < %s ORDER BY at DESC LIMIT %s",
            (account_id, before, limit),
        ).fetchall()
    out = []
    for row in rows:
        parts = [
            f"{row['moment']} UTC",
            WEB if row["connection"] == WEB else row["client"] or "unknown client",
            row["tool"],
            row["outcome"],
        ]
        if row["ref"]:
            parts.append(row["ref"])
        out.append(" · ".join(parts))
    return out
