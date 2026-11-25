"""Test harness: a REAL Postgres, cheap per-test isolation.

Needs ``RETICULA_TEST_DATABASE_URL`` (a superuser URL on a pgvector-enabled
server; ``scripts/test`` starts one). Per session we build one migrated
TEMPLATE database; then:

* ``conn``: a connection to a session clone, inside a transaction that is
  ROLLED BACK after the test. Cheapest; code under test must not commit.
* ``fresh_db_url``: a private per-test clone of the template (``CREATE
  DATABASE ... TEMPLATE``), for tests that need real commits (the server,
  migrations, several connections).
* ``blank_db_url``: an empty database.

Pure-format tests need none of this and run without a database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reticula_mcp import accounts
from reticula_mcp.accounts import Account
from reticula_mcp.db import connect
from reticula_mcp.db.migrate import migrate
from reticula_mcp.db.pool import Conn

# The suite runs on the deterministic embedder whatever is installed; tests of
# the real model opt in by setting RETICULA_EMBEDDER themselves.
os.environ.setdefault("RETICULA_EMBEDDER", "hash")

# Cheap argon2 parameters for the suite (production uses the library defaults).
accounts.PASSWORD_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
PASSWORD = "correct horse battery staple"


def _admin_url() -> str:
    url = os.environ.get("RETICULA_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "RETICULA_TEST_DATABASE_URL is not set: run the suite with scripts/test",
            pytrace=False,
        )
    return url


def _create_db(admin: str, name: str, template: str | None = None) -> str:
    with psycopg.connect(admin, autocommit=True) as c:
        stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if template:
            stmt += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
        c.execute(stmt)
    return make_conninfo(admin, dbname=name)


def _drop_db(admin: str, name: str) -> None:
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def _no_autovacuum(c: Conn) -> None:
    """Switch autovacuum off for every table of the template, and so for every
    database cloned from it.

    Autovacuum also refreshes the planner's statistics, about once a minute and
    from its own connection. That connection does not see the rows of a test,
    which are never committed, so it records "no rows" for a table that the
    test has filled. The planner then expects one row everywhere and picks
    plans that are slow for what is there. Measured: building the 585 hubs of
    ``test_a_wide_deep_tree_is_read_in_bounded_time`` took 19.5 s before such a
    refresh and 77.5 s after it, and in the Check run it went past the 120 s
    limit whenever the refresh happened to fall into that test. A server in use
    commits its rows, so its statistics describe them."""
    tables = c.execute(
        "SELECT c.oid::regclass::text AS name FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE c.relkind = 'r' AND n.nspname = 'public'"
    ).fetchall()
    for table in tables:
        c.execute(
            sql.SQL("ALTER TABLE {} SET (autovacuum_enabled = false)").format(
                sql.Identifier(table["name"])
            )
        )


def end_connection(c: Conn) -> None:
    """Roll back and close the connection of a test, also when the test was
    stopped by its time limit in the middle of a statement.

    Then the statement is still running on the server, ``rollback`` is refused
    ("another command is already in progress"), and without the cancel the rows
    of that test stay locked until the statement ends by itself: every later
    test that creates ``user1@example.com`` waits for them until its own time
    limit, which made one slow test look like a run that stands still."""
    try:
        c.rollback()
    except psycopg.OperationalError:
        c.cancel()
    finally:
        c.close()


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    admin = _admin_url()
    name = f"reticula_tpl_{uuid.uuid4().hex[:12]}"
    url = _create_db(admin, name)
    with connect(url) as c:
        migrate(c)
        _no_autovacuum(c)
    yield name
    _drop_db(admin, name)


@pytest.fixture(scope="session")
def session_db_url(template_db: str) -> Iterator[str]:
    admin = _admin_url()
    name = f"reticula_sess_{uuid.uuid4().hex[:12]}"
    url = _create_db(admin, name, template_db)
    yield url
    _drop_db(admin, name)


@pytest.fixture
def conn(session_db_url: str) -> Iterator[Conn]:
    """A configured connection inside a transaction that is rolled back. One
    statement may run for a minute: under the time limit of a test, so that a
    statement that does not end fails with its SQL in the message."""
    c = connect(session_db_url, options="-c statement_timeout=60s")
    try:
        yield c
    finally:
        end_connection(c)


@pytest.fixture
def fresh_db_url(template_db: str) -> Iterator[str]:
    """A private migrated database for this test (commits are real)."""
    admin = _admin_url()
    name = f"reticula_t_{uuid.uuid4().hex[:12]}"
    yield _create_db(admin, name, template_db)
    _drop_db(admin, name)


@pytest.fixture
def blank_db_url() -> Iterator[str]:
    admin = _admin_url()
    name = f"reticula_b_{uuid.uuid4().hex[:12]}"
    yield _create_db(admin, name)
    _drop_db(admin, name)


@pytest.fixture
def make_account(conn: Conn) -> Callable[..., Account]:
    counter = iter(range(1, 10_000))

    def make(email: str | None = None, handle: str | None = None) -> Account:
        n = next(counter)
        return accounts.create_account(
            conn, email or f"user{n}@example.com", PASSWORD, handle
        )

    return make
