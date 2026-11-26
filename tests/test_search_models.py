"""The embedding model is one setting (RETICULA_EMBED_MODEL): each known model
brings its vector size, its cut and its query prefix. The default is
bge-base-en-v1.5 with its query instruction (Reto, debug note baada5c5).
After a switch the worker embeds every note under the new model; once it has
caught up, ``reticula-mcp reindex --drop-other-models`` removes the vectors
and indexes of the old ones."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from reticula_mcp import accounts
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.search import cli, embedder, index
from reticula_mcp.search.embedder import (
    DEFAULT_FASTEMBED_MODEL,
    KNOWN_MODELS,
    UNMEASURED_MAX_DISTANCE,
    FastEmbedEmbedder,
)
from tests.conftest import PASSWORD
from tests.helpers import make_atom
from tests.search_support import SlowEmbedder


class FakeEngine:
    """Stands in for fastembed's TextEmbedding: records what it is given."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.queries: list[str] = []
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[1.0] * self.dim for _ in texts]

    def query_embed(self, text: str) -> list[list[float]]:
        self.queries.append(text)
        return [[1.0] * self.dim]


def loaded(name: str | None) -> tuple[FastEmbedEmbedder, FakeEngine]:
    model = FastEmbedEmbedder(name)
    engine = FakeEngine(model.dim)
    model._engine = engine
    return model, engine


def test_the_default_is_bge_base_with_its_query_instruction():
    assert DEFAULT_FASTEMBED_MODEL == "BAAI/bge-base-en-v1.5"
    model, engine = loaded(None)
    assert (model.model, model.dim, model.max_distance) == (
        "fastembed:BAAI/bge-base-en-v1.5",
        768,
        0.50,
    )
    model.embed_query("tall tower on the coast")
    model.embed(["Lighthouses migrate in winter"])
    assert engine.queries == [
        "Represent this sentence for searching relevant passages: tall tower on the coast"
    ]
    assert engine.texts == ["Lighthouses migrate in winter"]  # a note has no prefix


@pytest.mark.parametrize(
    ("name", "dim", "cut"),
    [
        ("BAAI/bge-small-en-v1.5", 384, 0.35),
        ("mixedbread-ai/mxbai-embed-large-v1", 1024, 0.41),
    ],
)
def test_each_model_brings_its_size_cut_and_prefix(name, dim, cut):
    """Both were measured best with plain queries."""
    model, engine = loaded(name)
    assert (model.dim, model.max_distance) == (dim, cut)
    model.embed_query("dromedary")
    assert engine.queries == ["dromedary"]


def test_a_model_not_measured_here_gets_the_strictest_cut_and_says_so(caplog):
    with caplog.at_level("WARNING", logger="reticula_mcp.search"):
        model = FastEmbedEmbedder("some/other-model")
    assert model.max_distance == UNMEASURED_MAX_DISTANCE == 0.35
    assert model.query_prefix == ""
    assert "has not been measured here" in caplog.text
    assert all(m.max_distance >= UNMEASURED_MAX_DISTANCE for m in KNOWN_MODELS.values())


def test_the_setting_names_the_model(monkeypatch):
    pytest.importorskip("fastembed")  # get_embedder refuses fastembed without it
    monkeypatch.setenv("RETICULA_EMBEDDER", "fastembed")
    monkeypatch.setenv("RETICULA_EMBED_MODEL", "mixedbread-ai/mxbai-embed-large-v1")
    embedder._build.cache_clear()
    try:
        built = embedder.get_embedder(Settings.from_env())
        assert built.model == "fastembed:mixedbread-ai/mxbai-embed-large-v1"
        assert built.dim == 1024 and built.max_distance == 0.41  # type: ignore[attr-defined]
    finally:
        embedder._build.cache_clear()


# ------------------------------------------------------- the vectors of before


def run_drop(url: str, model: Any) -> tuple[int, str]:
    with connect(url) as conn:
        return cli.drop_other_models(conn, model), ""


@pytest.fixture
def two_models(fresh_db_url):
    old = SlowEmbedder(delay=0.0, model="old", dim=32)
    new = SlowEmbedder(delay=0.0, model="new", dim=48)
    old.load()
    new.load()
    with connect(fresh_db_url) as c:
        ada = accounts.create_account(c, "ada@example.com", PASSWORD, "ada")
        for i in range(3):
            make_atom(c, ada, f"kiln note {i}", title=f"Kiln {i}")
        index.reindex(c, old)
        c.commit()
    return fresh_db_url, old, new


def counts(url: str) -> dict[str, int]:
    with connect(url) as c:
        rows = c.execute(
            "SELECT model, count(*) AS n FROM embedding GROUP BY model"
        ).fetchall()
    return {r["model"]: r["n"] for r in rows}


def test_the_old_vectors_stay_until_the_new_model_has_caught_up(two_models, capsys):
    url, old, new = two_models
    assert run_drop(url, new)[0] == 1  # the new model has nothing yet
    assert "not removed" in capsys.readouterr().out
    assert counts(url) == {old.model: 3}
    with connect(url) as c:
        index.embed_nodes(
            c, new, [r["id"] for r in c.execute("SELECT id FROM node").fetchall()][:1]
        )
        c.commit()
    assert run_drop(url, new)[0] == 1  # part of the way
    assert counts(url)[old.model] == 3


def test_once_caught_up_the_old_vectors_and_indexes_go(two_models, capsys):
    url, old, new = two_models
    with connect(url) as c:
        index.ensure_vector_index(c, new)
        index.reindex(c, new)  # every note; it records the model as complete
        assert index.is_complete(c, new)
        c.commit()
    assert run_drop(url, new)[0] == 0
    assert "removed 3 vectors" in capsys.readouterr().out
    assert counts(url) == {new.model: 3}
    with connect(url) as c:
        # dropping the old vectors put nothing into the backlog (0303)
        assert (
            c.execute("SELECT count(*) AS n FROM embedding_backlog").fetchone()["n"]
            == 0
        )
        assert index.has_vector_index(c, new)


def test_drop_and_drop_other_models_are_not_given_together():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    cli.register(sub)
    with pytest.raises(SystemExit):
        parser.parse_args(["reindex", "--drop", "--drop-other-models"])
