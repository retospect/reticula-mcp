from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

from reticula_mcp.db import connect
from reticula_mcp.db.migrate import (
    MigrationError,
    applied,
    discover,
    migrate,
    migrations_dir,
    pending,
)

EXPECTED_TABLES = {
    "account", "account_key", "record", "node", "membership", "hub_grant",
    "supersedes", "link", "embedding", "oauth_client", "oauth_pending",
    "oauth_code", "oauth_token", "signup_attempt", "schema_migration",
}  # fmt: skip


def test_migration_files_are_numbered_and_unique():
    found = discover()
    assert [m.version for m in found] == sorted({m.version for m in found})
    # foundation 0001-0099, sign-in 01xx, graph 02xx, search 03xx, content 04xx,
    # integration 09xx
    assert all(m.version < 500 or 900 <= m.version < 1000 for m in found)


def test_fresh_database_gets_everything_and_second_run_is_a_noop(blank_db_url):
    with connect(blank_db_url) as c:
        first = migrate(c)
        assert [m.version for m in first] == [m.version for m in discover()]
        assert migrate(c) == []
        assert pending(c) == []
        tables = {
            r["tablename"]
            for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        assert tables >= EXPECTED_TABLES
        ext = {r["extname"] for r in c.execute("SELECT extname FROM pg_extension")}
        assert {"vector", "citext", "pg_trgm"} <= ext
        assert set(applied(c)) == {m.version for m in discover()}


def test_gaps_are_tolerated_and_late_lower_numbers_still_apply(
    blank_db_url, tmp_path: Path
):
    shutil.copy(migrations_dir() / "0001_accounts_records.sql", tmp_path)
    (tmp_path / "0900_late.sql").write_text("CREATE TABLE late_one (x int);")
    with connect(blank_db_url) as c:
        assert [m.version for m in migrate(c, tmp_path)] == [1, 900]
        # a lane merges a lower number after a higher one was applied
        (tmp_path / "0150_signin.sql").write_text("CREATE TABLE mid_one (x int);")
        assert [m.version for m in migrate(c, tmp_path)] == [150]
        assert migrate(c, tmp_path) == []


def test_edited_sealed_migration_is_refused(blank_db_url, tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (x int);")
    with connect(blank_db_url) as c:
        migrate(c, tmp_path)
        (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (x int, y int);")
        with pytest.raises(MigrationError, match="changed after it was applied"):
            migrate(c, tmp_path)


def test_failing_migration_rolls_back_and_is_reported(blank_db_url, tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (x int); SELECT 1/0;")
    with connect(blank_db_url) as c:
        with pytest.raises(MigrationError, match=r"0001_a\.sql failed"):
            migrate(c, tmp_path)
        assert c.execute("SELECT to_regclass('a')").fetchone()["to_regclass"] is None
        assert applied(c) == {}


def test_duplicate_and_badly_named_files_are_errors(tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("")
    (tmp_path / "0001_b.sql").write_text("")
    with pytest.raises(MigrationError, match="duplicate"):
        discover(tmp_path)
    (tmp_path / "0001_b.sql").unlink()
    (tmp_path / "oops.sql").write_text("")
    with pytest.raises(MigrationError, match="bad migration file name"):
        discover(tmp_path)


def test_record_log_is_append_only(conn):
    from reticula_mcp import accounts

    acct = accounts.create_account(conn, "ao@example.com", "correct horse battery")
    rid = conn.execute(
        "SELECT id FROM record WHERE author = %s", (acct.person_id,)
    ).fetchone()["id"]
    for stmt in (
        "UPDATE record SET audience = 'x' WHERE id = %s",
        "UPDATE record SET envelope = '\\x' WHERE id = %s",
        "DELETE FROM record WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with conn.transaction():
                conn.execute(stmt, (rid,))
    # the sanctioned erasure path refuses a live, non-tombstoned address
    with pytest.raises(psycopg.errors.RaiseException, match="no tombstone"):
        with conn.transaction():
            conn.execute("SELECT blank_record_content(%s)", (rid,))
