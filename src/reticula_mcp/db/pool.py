"""Connections: psycopg 3, sync, ``dict_row`` rows everywhere.

Contract for code that receives a ``conn``: do NOT call ``conn.commit()`` or
``conn.rollback()``. The owner of the connection (the server, a CLI command or
a test fixture) owns the transaction. Use ``with conn.transaction():`` for a
savepoint when you want partial rollback.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

Conn = psycopg.Connection[dict[str, Any]]

#: Longest one statement may run on a connection the running server took from
#: its pool. The CLI commands (``migrate``, ``rebuild``, embedding upkeep, ...)
#: open their own connections with :func:`connect` and have no limit.
STATEMENT_TIMEOUT_SECONDS = 15.0


def configure_connection(conn: psycopg.Connection[Any]) -> None:
    """Per-connection setup: dict rows; pgvector adapters when the extension exists.

    Leaves the connection idle (no open transaction).
    """
    conn.row_factory = dict_row
    in_tx = conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE
    row = conn.execute(
        "SELECT 1 AS x FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    if row:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    if not in_tx:
        conn.commit()


def connect(url: str, **kwargs: Any) -> Conn:
    """One configured connection (CLI, tests). Caller commits/closes."""
    conn = psycopg.connect(url, **kwargs)
    configure_connection(conn)
    return conn


class Database:
    """A connection pool. ``connection()`` yields one transaction.

    The transaction commits when the ``with`` block exits normally and rolls
    back when it raises.
    """

    def __init__(
        self,
        url: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        statement_timeout: float | None = STATEMENT_TIMEOUT_SECONDS,
    ) -> None:
        self.url = url
        self.statement_timeout = statement_timeout
        kwargs: dict[str, Any] = {}
        if statement_timeout:
            # Set when the connection is opened, so it holds for the connection's
            # whole life in the pool and needs no statement of its own.
            kwargs["options"] = f"-c statement_timeout={int(statement_timeout * 1000)}"
        self._pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            configure=configure_connection,
            kwargs=kwargs,
        )

    def open(self) -> None:
        if self._pool.closed:
            self._pool.open(wait=True, timeout=15)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[Conn]:
        self.open()
        with self._pool.connection() as conn:
            yield conn

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            return False
        return True
