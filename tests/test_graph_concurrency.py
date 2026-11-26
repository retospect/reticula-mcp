"""Concurrent writers on real, separate connections (real commits)."""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

import pytest

from reticula_mcp import accounts, records
from reticula_mcp.db import connect
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, handlers


def _in_thread(fn) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["value"] = fn()
        except BaseException as exc:
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _add_and_commit(conn, actor, node_id, hub_id) -> None:
    api.add_membership(conn, actor, node_id, hub_id)
    conn.commit()


@pytest.fixture
def two_conns(fresh_db_url):
    c1, c2 = connect(fresh_db_url), connect(fresh_db_url)
    try:
        yield c1, c2
    finally:
        c1.rollback()
        c2.rollback()
        c1.close()
        c2.close()


def test_concurrent_edits_form_one_chain(two_conns):
    c1, c2 = two_conns
    alice = accounts.create_account(c1, "a@example.com", "pw-correct-horse-1", None)
    node = api.create_atom(c1, alice, kind="note", text="v1")
    c1.commit()
    # c1 edits and holds the row lock; c2's edit must wait, then build on c1's
    api.update_atom(c1, alice, node["id"], text="from c1")
    t, out = _in_thread(
        lambda: (
            api.update_atom(c2, alice, node["id"], text="from c2"),
            c2.commit(),
        )
    )
    time.sleep(0.6)
    assert t.is_alive(), "the second edit should wait for the row lock"
    c1.commit()
    t.join(10)
    assert "error" not in out, out
    c1.rollback()
    versions = records.versions(c1, node["address"])
    assert len(versions) == 3
    for older, newer in itertools.pairwise(versions):
        assert newer.parents == (older.version_hash,)
    head = c1.execute(
        "SELECT text, head_version FROM node WHERE id = %s", (node["id"],)
    ).fetchone()
    assert (
        head["text"] == "from c2" and head["head_version"] == versions[-1].version_hash
    )


def test_concurrent_filings_cannot_close_a_cycle_together(two_conns):
    c1, c2 = two_conns
    alice = accounts.create_account(c1, "a@example.com", "pw-correct-horse-1", None)
    a = api.create_atom(c1, alice, kind="hub", text="", title="A")
    b = api.create_atom(c1, alice, kind="hub", text="", title="B")
    c1.commit()
    api.add_membership(c1, alice, a["id"], b["id"])  # A under B, not yet committed
    t, out = _in_thread(
        lambda: _add_and_commit(c2, alice, b["id"], a["id"])
    )  # B under A: would close the cycle
    time.sleep(0.6)
    assert t.is_alive(), "the second filing should wait for the hierarchy lock"
    c1.commit()
    t.join(10)
    error = out.get("error")
    assert isinstance(error, ReticulaError) and error.code == "Conflict", out
    c2.rollback()
    live = c1.execute("SELECT count(*) AS n FROM membership WHERE NOT withdrawn")
    assert live.fetchone()["n"] == 1


def test_concurrent_duplicate_filing_is_idempotent(two_conns):
    c1, c2 = two_conns
    alice = accounts.create_account(c1, "a@example.com", "pw-correct-horse-1", None)
    hub = api.create_atom(c1, alice, kind="hub", text="", title="H")
    note = api.create_atom(c1, alice, kind="note", text="n")
    c1.commit()
    api.add_membership(c1, alice, note["id"], hub["id"])
    t, out = _in_thread(lambda: _add_and_commit(c2, alice, note["id"], hub["id"]))
    time.sleep(0.6)
    assert t.is_alive()
    c1.commit()
    t.join(10)
    assert "error" not in out, out
    n = c1.execute("SELECT count(*) AS n FROM membership").fetchone()["n"]
    assert n == 1


def test_handlers_commit_cleanly_through_a_real_transaction(two_conns):
    c1, c2 = two_conns
    alice = accounts.create_account(c1, "a@example.com", "pw-correct-horse-1", None)
    bob = accounts.create_account(c1, "b@example.com", "pw-correct-horse-1", None)
    c1.commit()
    hub = handlers.put(c1, alice, text="", kind="hub", title="H")
    c1.commit()
    address = hub.split()[2]
    handlers.share(c2, alice, hub=address, person=f"@{bob.handle}")
    c2.commit()
    # they share no hub yet: bob's yes (0304)
    handlers.share(c2, bob, hub=address[-8:], person=f"@{bob.handle}", mode="add")
    c2.commit()
    out = handlers.put(c1, bob, text="hi", hub=address)
    c1.commit()
    assert "saved note" in out
