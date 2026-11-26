"""A tool call that writes embeds its note after its transaction commits and
before it replies (Reto's choice, debug note baada5c5): no row lock is held
while the model runs, and the note is findable by meaning when the reply
arrives. Outside a tool call (a command, a test calling a handler) the write
embeds in its own transaction, as tests/test_search_embed_on_write.py
shows."""

from __future__ import annotations

from typing import Any

import pytest

from reticula_mcp import accounts
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Database
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import hooks, index
from tests.auth_support import run_mcp, start_live
from tests.conftest import PASSWORD
from tests.search_support import SlowEmbedder, use_embedder


def call(live: Any, token: str, tool: str, **args: Any) -> str:
    async def work(session: Any, _init: Any) -> str:
        result = await session.call_tool(tool, args)
        return "\n".join(c.text for c in result.content if hasattr(c, "text"))

    out: str = run_mcp(live, token, work)
    return out


class Watching(SlowEmbedder):
    """Loaded; records, at each embed, whether the note is already committed
    (seen from a connection of its own)."""

    url = ""
    seen_committed: list[bool]

    def embed(self, texts: list[str]) -> list[list[float]]:
        with connect(self.url) as other:
            n = other.execute(
                "SELECT count(*) AS n FROM node WHERE kind = 'note'"
            ).fetchone()["n"]
        self.seen_committed.append(n > 0)
        return super().embed(texts)


@pytest.fixture
def ada(fresh_db_url):
    with connect(fresh_db_url) as c:
        ada = accounts.create_account(c, "ada@example.com", PASSWORD, "ada")
        token = accounts.mint_token(c, ada, "test")
        c.commit()
    return ada, token


def vectors_for(url: str, model: str) -> list[dict[str, Any]]:
    with connect(url) as c:
        return c.execute(
            "SELECT e.version_hash = n.head_version AS fresh FROM embedding e"
            " JOIN node n ON n.id = e.node_id WHERE n.kind = 'note' AND e.model = %s",
            (model,),
        ).fetchall()


def test_a_put_through_the_server_embeds_after_its_commit(
    fresh_db_url, monkeypatch, ada
):
    _ada, token = ada
    model = Watching(delay=0.0)
    model.url, model.seen_committed = fresh_db_url, []
    model.load()
    use_embedder(monkeypatch, model)
    with start_live(fresh_db_url) as live:
        out = call(live, token, "put", text="kiln shelf wash notes", title="Wash")
        assert "Wash" in out
        # findable by meaning when the reply is here: the vector is committed
        assert vectors_for(fresh_db_url, model.model) == [{"fresh": True}]
        assert model.seen_committed == [True]  # the model ran after the commit
        with connect(fresh_db_url) as c:
            assert (
                c.execute("SELECT count(*) AS n FROM embedding_backlog").fetchone()["n"]
                == 0
            )


def test_a_failing_model_after_the_commit_leaves_the_note_to_the_worker(
    fresh_db_url, monkeypatch, ada
):
    _ada, token = ada

    class Broken(SlowEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("the model fell over")

    model = Broken(delay=0.0)
    model.load()
    use_embedder(monkeypatch, model)
    with start_live(fresh_db_url) as live:
        out = call(live, token, "put", text="okapi notes", title="Okapi")
    assert "Okapi" in out and "[error" not in out  # the write's answer
    assert vectors_for(fresh_db_url, model.model) == []
    with connect(fresh_db_url) as c:
        assert (
            c.execute("SELECT count(*) AS n FROM embedding_backlog").fetchone()["n"]
            == 1
        )


def test_a_call_that_fails_embeds_nothing(fresh_db_url, monkeypatch, ada):
    viewer, _token = ada
    model = SlowEmbedder(delay=0.0)
    model.load()
    use_embedder(monkeypatch, model)
    attempts: list[list[int]] = []
    real = hooks.embed_written

    def counted(db: Database, node_ids: list[int]) -> None:
        attempts.append(list(node_ids))
        real(db, node_ids)

    monkeypatch.setattr(hooks, "embed_written", counted)
    db = Database(fresh_db_url)
    db.open()
    try:
        with pytest.raises(RuntimeError):
            with hooks.after_commit(db), db.connection() as conn:
                graph.put(conn, viewer, text="a note that is rolled back")
                raise RuntimeError("the call fails after its write")
        assert model.embed_calls == 0 and attempts == []  # not even tried
        # and with no error the same block embeds after the commit
        with hooks.after_commit(db), db.connection() as conn:
            graph.put(conn, viewer, text="a note that is kept", title="Kept")
            assert model.embed_calls == 0  # not inside the transaction
        assert model.embed_calls == 1 and len(attempts) == 1
    finally:
        db.close()


def test_outside_a_call_the_write_embeds_in_its_own_transaction(
    conn, make_account, monkeypatch
):
    model = SlowEmbedder(delay=0.0)
    model.load()
    use_embedder(monkeypatch, model)
    ada = make_account()
    graph.put(conn, ada, text="a note written by a command", title="Command")
    assert model.embed_calls == 1
    assert index.pending_ids(conn, ada.id, 10) == []
