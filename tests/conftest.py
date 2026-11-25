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


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    admin = _admin_url()
    name = f"reticula_tpl_{uuid.uuid4().hex[:12]}"
    url = _create_db(admin, name)
    with connect(url) as c:
        migrate(c)
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
    """A configured connection inside a transaction that is rolled back."""
    c = connect(session_db_url)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


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
