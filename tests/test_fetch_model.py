"""deploy/fetch-model.py: the pinned table and the checks, without a network.

The script puts the embedding model into the image at build time. These tests
call it with a download function that serves made-up bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import stat
import sys
from pathlib import Path

import pytest

from reticula_mcp.search.embedder import KNOWN_MODELS

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "fetch-model.py"


def _load():
    spec = importlib.util.spec_from_file_location("fetch_model", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_model"] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


fm = _load()


def _content(name: str, filename: str) -> bytes:
    return f"{name} {filename}".encode()


@pytest.fixture
def fake(monkeypatch):
    """A table of one model whose files are served by a fake download."""
    name = "test/model"
    files = {
        "config.json": _content(name, "config.json"),
        "onnx/model.onnx": _content(name, "onnx/model.onnx"),
    }
    model = fm.Model(
        repo="Org/repo-onnx",
        revision="a" * 40,
        files={f: hashlib.sha256(b).hexdigest() for f, b in files.items()},
    )
    monkeypatch.setitem(fm.MODELS, name, model)
    calls: list[tuple[str, str, str]] = []
    served = dict(files)

    def download(repo: str, revision: str, filename: str, scratch: Path) -> Path:
        calls.append((repo, revision, filename))
        if filename not in served:
            raise FileNotFoundError(filename)
        path = scratch / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(served[filename])
        return path

    return name, model, served, calls, download


def test_table_has_every_known_model():
    assert set(fm.MODELS) == set(KNOWN_MODELS)


def test_table_entries_are_pinned():
    for name, model in fm.MODELS.items():
        assert re.fullmatch(r"[0-9a-f]{40}", model.revision), name
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", model.repo), name
        assert model.files, name
        for filename, digest in model.files.items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest), (name, filename)
        # fastembed needs a tokenizer and a model file for every model
        assert "tokenizer.json" in model.files, name
        assert any(f.endswith(".onnx") for f in model.files), name


def test_fetch_writes_the_layout_fastembed_reads(fake, tmp_path):
    name, model, served, calls, download = fake
    snapshot = fm.fetch(name, tmp_path, download)

    repo_dir = tmp_path / "models--Org--repo-onnx"
    assert snapshot == repo_dir / "snapshots" / model.revision
    assert (repo_dir / "refs" / "main").read_bytes() == model.revision.encode()
    for filename, data in served.items():
        assert (snapshot / filename).read_bytes() == data
    assert {c[2] for c in calls} == set(served)
    assert {(c[0], c[1]) for c in calls} == {(model.repo, model.revision)}


def test_fetch_sets_readable_modes(fake, tmp_path):
    name, _model, _served, _calls, download = fake
    fm.fetch(name, tmp_path, download)
    for path in tmp_path.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o755 if path.is_dir() else 0o644), path


def test_wrong_content_fails(fake, tmp_path):
    name, _model, served, _calls, download = fake
    served["onnx/model.onnx"] = b"something else"
    with pytest.raises(fm.FetchError, match=r"onnx/model\.onnx has sha256"):
        fm.fetch(name, tmp_path, download)
    # a failed fetch leaves no reference that would make the server use it
    assert not list(tmp_path.rglob("main"))


def test_missing_file_fails(fake, tmp_path):
    name, _model, served, _calls, download = fake
    del served["config.json"]
    with pytest.raises(fm.FetchError, match=r"config\.json could not be downloaded"):
        fm.fetch(name, tmp_path, download)


def test_unknown_model_names_the_known_ones(tmp_path):
    def never(*_args):
        raise AssertionError("nothing may be downloaded for an unknown model")

    with pytest.raises(fm.FetchError) as caught:
        fm.fetch("someone/else-model", tmp_path, never)
    message = str(caught.value)
    assert "someone/else-model" in message
    assert "only these models" in message
    for known in fm.MODELS:
        assert known in message
    assert not list(tmp_path.iterdir())


def test_main_exit_status(fake, tmp_path, capsys):
    name, _model, served, _calls, download = fake
    assert fm.main(["fetch-model.py", name, str(tmp_path / "ok")], download) == 0
    served["config.json"] = b"changed"
    assert fm.main(["fetch-model.py", name, str(tmp_path / "bad")], download) == 1
    assert "fetch-model: " in capsys.readouterr().err
    assert fm.main(["fetch-model.py", "no/such", str(tmp_path / "x")], download) == 1
    assert fm.main(["fetch-model.py"], download) == 2
