"""The test harness itself: what keeps one slow test from stopping a run."""

from __future__ import annotations

import time

import psycopg

from reticula_mcp.db import connect
from tests.conftest import end_connection


def test_no_test_database_is_analysed_behind_a_test(conn, fresh_db_url):
    """See ``conftest._no_autovacuum`` for what happened when one was."""
    query = (
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n"
        " ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname = 'public'"
        " AND NOT coalesce(c.reloptions @> ARRAY['autovacuum_enabled=false'], false)"
    )
    assert conn.execute(query).fetchall() == []
    with psycopg.connect(fresh_db_url) as other:
        assert other.execute(query).fetchall() == []
    assert (
        conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "1min"
    )


def test_a_connection_stopped_inside_a_statement_gives_its_rows_back(session_db_url):
    """A test that runs out of time inside a statement leaves the statement
    running. Its connection is ended all the same, and the next test does not
    wait for the rows it holds."""
    marker = "harness-probe@example.com"
    c = connect(session_db_url)
    c.execute("CREATE TABLE IF NOT EXISTS harness_probe (email text PRIMARY KEY)")
    c.commit()
    try:
        c.execute("INSERT INTO harness_probe VALUES (%s)", (marker,))
        # what the time limit leaves behind: a statement sent and not read
        c.pgconn.send_query(b"SELECT pg_sleep(60)")
        started = time.monotonic()
        end_connection(c)
        assert c.closed
        with connect(session_db_url, options="-c lock_timeout=10s") as other:
            # waits on the primary key while the first transaction is open
            other.execute("INSERT INTO harness_probe VALUES (%s)", (marker,))
            other.rollback()
        assert time.monotonic() - started < 10
    finally:
        with connect(session_db_url) as cleanup:
            cleanup.execute("DROP TABLE IF EXISTS harness_probe")
