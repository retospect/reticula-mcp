"""Test support for the search lane.

Cheap state for read-side tests: atoms through the frozen foundation
(``make_atom``) and direct SQL on the frozen graph tables, read back through the
real ``readable_nodes`` (migration 0200). Tests of what a write does to
visibility use the graph handlers instead (``tests/test_search_access.py``).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg
import pytest

from reticula_mcp.accounts import Account
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn
from reticula_mcp.search import embedder as embedder_module
from reticula_mcp.search.embedder import Embedder, HashEmbedder
from tests.helpers import make_atom


def use_hash_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETICULA_EMBEDDER", "hash")
    monkeypatch.delenv("RETICULA_EMBED_MODEL", raising=False)
    embedder_module._build.cache_clear()


def use_embedder(monkeypatch: pytest.MonkeyPatch, embedder: Embedder) -> None:
    """Make ``get_embedder`` return ``embedder`` everywhere (search, the write
    hook, the worker, the command line) for the rest of the test."""
    monkeypatch.setenv("RETICULA_EMBEDDER", "hash")

    def build(kind: str, model_name: str | None) -> Embedder:
        return embedder

    build.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(embedder_module, "_build", build)


def use_real_model(monkeypatch: pytest.MonkeyPatch) -> Embedder:
    """Switch to the real fastembed model, NOT loaded yet. Skips the test when
    the extra is not installed. Model files are read from (and on first use
    downloaded to) fastembed's own default cache unless the environment names
    another, so a test run never fills the home directory."""
    pytest.importorskip("fastembed")
    monkeypatch.setenv("RETICULA_EMBEDDER", "fastembed")
    monkeypatch.delenv("RETICULA_EMBED_MODEL", raising=False)
    if not os.environ.get("FASTEMBED_CACHE_PATH"):
        monkeypatch.setenv(
            "FASTEMBED_CACHE_PATH",
            os.path.join(tempfile.gettempdir(), "fastembed_cache"),
        )
    embedder_module._build.cache_clear()
    monkeypatch.setattr(
        embedder_module, "_build", embedder_module._build
    )  # restored, then cleared, after the test
    return embedder_module.get_embedder(Settings.from_env())


def load_real_model(monkeypatch: pytest.MonkeyPatch) -> Embedder:
    """:func:`use_real_model`, loaded. Skips when the model cannot be loaded
    (the first download needs the network)."""
    embedder = use_real_model(monkeypatch)
    try:
        embedder.load()  # type: ignore[attr-defined]
    except Exception as exc:
        pytest.skip(f"real embedding model unavailable: {exc}")
    return embedder


def wait_until(
    condition: Callable[[], Any], seconds: float = 5.0, step: float = 0.02
) -> bool:
    """Poll ``condition`` until it is true; False when ``seconds`` ran out."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return bool(condition())


class SqlSpy:
    """What the code under test asked the database (``statements``) and how
    many rows it read back (``rows``), since the spy was installed or last
    :meth:`reset`. A count stands for the time a call takes where the wall
    clock would make a test fail on a busy machine."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rows = 0

    def reset(self) -> None:
        self.statements.clear()
        self.rows = 0


def spy_on_sql(monkeypatch: pytest.MonkeyPatch) -> SqlSpy:
    """Count the statements run, and the rows fetched, on every psycopg cursor
    (so on every connection) for the rest of the test."""
    spy = SqlSpy()
    execute = psycopg.Cursor.execute
    fetchone = psycopg.Cursor.fetchone
    fetchmany = psycopg.Cursor.fetchmany
    fetchall = psycopg.Cursor.fetchall
    iterate = psycopg.Cursor.__iter__

    def counted_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        spy.statements.append(str(query))
        return execute(self, query, *args, **kwargs)

    def counted_fetchone(self: Any) -> Any:
        row = fetchone(self)
        spy.rows += row is not None
        return row

    def counted_fetchmany(self: Any, *args: Any, **kwargs: Any) -> Any:
        rows = fetchmany(self, *args, **kwargs)
        spy.rows += len(rows)
        return rows

    def counted_fetchall(self: Any) -> Any:
        rows = fetchall(self)
        spy.rows += len(rows)
        return rows

    def counted_iter(self: Any) -> Any:
        for row in iterate(self):
            spy.rows += 1
            yield row

    monkeypatch.setattr(psycopg.Cursor, "execute", counted_execute)
    monkeypatch.setattr(psycopg.Cursor, "fetchone", counted_fetchone)
    monkeypatch.setattr(psycopg.Cursor, "fetchmany", counted_fetchmany)
    monkeypatch.setattr(psycopg.Cursor, "fetchall", counted_fetchall)
    monkeypatch.setattr(psycopg.Cursor, "__iter__", counted_iter)
    return spy


def record_calls(
    monkeypatch: pytest.MonkeyPatch, target: Any, *names: str
) -> list[str]:
    """Note each call of the named methods of ``target`` (an instance), in
    order, and let the call through."""
    calls: list[str] = []

    def wrap(name: str) -> None:
        real = getattr(target, name)

        def recorded(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(target, name, recorded)

    for name in names:
        wrap(name)
    return calls


class SlowEmbedder:
    """Stands in for a real model where the suite has none: not cheap, takes
    ``delay`` seconds to load, fails its first ``failures`` loads, and counts
    what was asked of it. Vectors come from the hash embedder.

    With a ``gate`` the load also waits until the test sets the event (or
    ``gate_wait`` seconds have passed). A test that must show "the caller did
    not wait for the load" holds the gate shut, checks ``ready`` is still
    false, and opens it afterwards: no clock decides the outcome."""

    cheap = False
    max_distance = 0.8
    gate_wait = 30.0  # only so that a broken test ends

    def __init__(
        self,
        delay: float = 0.3,
        failures: int = 0,
        dim: int = 64,
        model: str = "slow",
        embed_delay: float = 0.0,
        gate: threading.Event | None = None,
    ) -> None:
        self._inner = HashEmbedder(dim=dim, model=model)
        self.gate = gate
        self.delay = delay
        self.failures = failures
        self.embed_delay = embed_delay
        self.ready = False
        self.load_error: str | None = None
        self.loads = 0  # load attempts
        self.embed_calls = 0
        self.embedded = 0  # texts embedded
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dim(self) -> int:
        return self._inner.dim

    def load(self) -> None:
        with self._lock:
            if self.ready:
                return
            self.loads += 1
            time.sleep(self.delay)
            if self.gate is not None:
                self.gate.wait(self.gate_wait)
            if self.loads <= self.failures:
                self.load_error = "RuntimeError: no network"
                raise RuntimeError("no network")
            self.load_error = None
            self.ready = True

    def _quiet(self) -> None:
        try:
            self.load()
        except RuntimeError:
            pass

    def warm(self) -> None:
        if self.ready or (self._thread is not None and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._quiet, daemon=True)
        self._thread.start()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.load()  # like the real one: an unloaded model makes the caller wait
        self.embed_calls += 1
        self.embedded += len(texts)
        if self.embed_delay:
            time.sleep(self.embed_delay)
        return self._inner.embed(texts)


def file_under(
    conn: Conn,
    atom: dict[str, Any],
    hub: dict[str, Any],
    *,
    by: Account | None = None,
    audience: str = "hub",
    withdrawn: bool = False,
) -> None:
    """A membership row (the real graph lane signs a `member` record; the search
    lane only reads the materialised table)."""
    author = by.person_id if by else atom["author"]
    conn.execute(
        "INSERT INTO membership (address, node_id, hub_node_id, author, audience,"
        " withdrawn, record_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            f"ret:{author}/{uuid.uuid4()}",
            atom["id"],
            hub["id"],
            author,
            audience,
            withdrawn,
            atom["head_record_id"],
        ),
    )


def grant(conn: Conn, hub: dict[str, Any], grantee: Account, mode: str = "add") -> None:
    conn.execute(
        "INSERT INTO hub_grant (hub_node_id, grantee, grantee_account_id, mode,"
        " granted_by, address, record_id) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (hub_node_id, grantee) DO UPDATE SET mode = EXCLUDED.mode",
        (
            hub["id"],
            grantee.person_id,
            grantee.id,
            mode,
            hub["author"],
            f"ret:{hub['author']}/{uuid.uuid4()}",
            hub["head_record_id"],
        ),
    )


def supersede(conn: Conn, new: dict[str, Any], old: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO supersedes (new_node_id, old_node_id) VALUES (%s, %s)",
        (new["id"], old["id"]),
    )


def link(conn: Conn, src: dict[str, Any], rel: str, dst: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO link (src_node_id, rel, dst_address, dst_node_id)"
        " VALUES (%s, %s, %s, %s)",
        (src["id"], rel, dst["address"], dst["id"]),
    )


def tombstone(conn: Conn, atom: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE node SET tombstoned_at = now(), text = '', title = NULL WHERE id = %s",
        (atom["id"],),
    )


def set_created(conn: Conn, atom: dict[str, Any], when: datetime) -> None:
    conn.execute(
        "UPDATE node SET created_at = %s, updated_at = %s WHERE id = %s",
        (when, when, atom["id"]),
    )


def hub_of(
    conn: Conn, owner: Account, title: str, text: str = "", **kw: Any
) -> dict[str, Any]:
    """A hub atom. Like the graph lane, give its creator an ``add`` grant so the
    owner reads what members file in it."""
    hub = make_atom(conn, owner, text or title, kind="hub", title=title, **kw)
    grant(conn, hub, owner, "add")
    return hub
