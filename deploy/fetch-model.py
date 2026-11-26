#!/usr/bin/env python3
"""Fetch an embedding model for the server image, at build time.

    python fetch-model.py MODEL TARGET_DIR

MODEL is a Hugging Face model name the server knows (see MODELS). The files
come from a fixed revision of the repository that fastembed reads for that
model, and every file is checked against a sha256 pinned here. A file that
differs, a file that is missing and a model not listed stop the build with a
message and a non-zero exit status. Nothing is downloaded when the server runs.

TARGET_DIR gets the layout that fastembed finds again without a network, with
FASTEMBED_CACHE_PATH=TARGET_DIR and HF_HUB_OFFLINE=1 (huggingface_hub's cache
layout, with plain files in place of its links into a blob store):

    models--<org>--<name>/refs/main              the revision, no newline
    models--<org>--<name>/snapshots/<revision>/  the files

The files are the ones fastembed 0.9 asks the repository for: the model file
and those of config.json, tokenizer.json, tokenizer_config.json and
special_tokens_map.json that the repository has.

To move a model to a newer revision, put the new commit sha into MODELS and
run `python deploy/fetch-model.py --hash MODEL`. It downloads the files at that
revision and prints their sha256 without checking anything; read the files
before you copy the values into the table.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Model:
    """Where a model comes from: ``repo`` at ``revision`` (a full commit sha),
    and the sha256 of each file fastembed needs, by its path in the repository."""

    repo: str
    revision: str
    files: dict[str, str]


# Keep the keys in step with KNOWN_MODELS in reticula_mcp/search/embedder.py;
# tests/test_fetch_model.py fails when they differ. fastembed 0.9.1 names the
# repository and the model file of each (fastembed/text/*.py).
MODELS: dict[str, Model] = {
    "BAAI/bge-small-en-v1.5": Model(
        repo="Qdrant/bge-small-en-v1.5-onnx-Q",
        revision="aa8f8b060edb00e03bfdd08813a2949946c8ba55",
        files={
            "config.json": "13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd",
            "special_tokens_map.json": "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
            "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            "tokenizer_config.json": "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53",
            "model_optimized.onnx": "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
        },
    ),
    "BAAI/bge-base-en-v1.5": Model(
        repo="Qdrant/bge-base-en-v1.5-onnx-Q",
        revision="199291fdd1aa89faf9c20b722dc72ad5e17aa0d0",
        files={
            "config.json": "86f84a5285de7f1ee673f712387219ef1e261ec27dcd870e793a80f9da1aaa3b",
            "special_tokens_map.json": "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
            "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            "tokenizer_config.json": "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53",
            "model_optimized.onnx": "4e556722bc4f65716c544c8a931f1e90fb3f866e5741fd93a96f051d673339c7",
        },
    ),
    "mixedbread-ai/mxbai-embed-large-v1": Model(
        repo="mixedbread-ai/mxbai-embed-large-v1",
        revision="b33106f585b9ce46904ad7443a3b52b7a63e231c",
        files={
            "config.json": "ce13f118fc183c005236901d2662ced752de632ebb161e6af800aadda2d176f7",
            "special_tokens_map.json": "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
            "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            "tokenizer_config.json": "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53",
            "onnx/model.onnx": "adb53ed475faa339bfad3bd2bdb7e6a30b4f47280ade9811f81bef7953f9ab77",
        },
    ),
    "sentence-transformers/all-MiniLM-L6-v2": Model(
        repo="Qdrant/all-MiniLM-L6-v2-onnx",
        revision="d13954661f83248295ba75c1ed411eef3b7b936e",
        files={
            "config.json": "1b4d8e2a3988377ed8b519a31d8d31025a25f1c5f8606998e8014111438efcd7",
            "special_tokens_map.json": "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
            "tokenizer.json": "851ca67100d372ca3ae031a6abd168f53489eebfd7d89523f35c5c9b4d372c3c",
            "tokenizer_config.json": "abda01c8c14c5151ae498aceb30db406d6b91242c394fd88d3b6fd5a63a101e6",
            "model.onnx": "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5",
        },
    ),
}

# Fetches one file of a repository at a revision into a scratch directory and
# returns the path of the copy: (repo, revision, filename, scratch) -> path.
Download = Callable[[str, str, str, Path], Path]


class FetchError(Exception):
    """The model cannot be put into the image; the message says why."""


def hub_download(repo: str, revision: str, filename: str, scratch: Path) -> Path:
    from huggingface_hub import hf_hub_download

    return Path(
        hf_hub_download(
            repo_id=repo, filename=filename, revision=revision, cache_dir=scratch
        )
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_folder(repo: str) -> str:
    """The name huggingface_hub gives the cache folder of a repository."""
    return "models--" + repo.replace("/", "--")


def fetch(model_name: str, target: Path, download: Download = hub_download) -> Path:
    """Put the pinned files of ``model_name`` under ``target``; returns the
    snapshot directory. Raises FetchError."""
    model = MODELS.get(model_name)
    if model is None:
        raise FetchError(
            f"unknown model {model_name!r}. The image carries only these models: "
            + ", ".join(MODELS)
            + ". Set RETICULA_EMBED_MODEL to one of them."
        )
    repo_dir = target / cache_folder(model.repo)
    snapshot = repo_dir / "snapshots" / model.revision
    with tempfile.TemporaryDirectory(prefix="fetch-model-") as scratch:
        for filename, expected in model.files.items():
            try:
                fetched = download(model.repo, model.revision, filename, Path(scratch))
            except Exception as exc:
                raise FetchError(
                    f"{model.repo}@{model.revision[:12]}: {filename} could not be"
                    f" downloaded ({type(exc).__name__}: {exc})"
                ) from exc
            if not fetched.is_file():
                raise FetchError(
                    f"{model.repo}@{model.revision[:12]}: {filename} is missing"
                )
            final = snapshot / filename
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fetched, final)
            fetched.unlink()
            actual = sha256_of(final)
            if actual != expected:
                raise FetchError(
                    f"{model.repo}@{model.revision[:12]}: {filename} has sha256"
                    f" {actual}, expected {expected}"
                )
    # huggingface_hub reads this file without stripping it
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text(model.revision, encoding="ascii")
    _set_modes(target)
    return snapshot


def _set_modes(target: Path) -> None:
    """Directories 755 and files 644, whatever the umask of the build was."""
    for directory, _dirs, names in os.walk(target):
        os.chmod(directory, 0o755)
        for name in names:
            os.chmod(os.path.join(directory, name), 0o644)


def hash_files(model_name: str, download: Download = hub_download) -> dict[str, str]:
    """The sha256 of each pinned file as the repository serves it now. For
    updating the table; ``fetch`` is what checks."""
    model = MODELS.get(model_name)
    if model is None:
        raise FetchError(f"unknown model {model_name!r}")
    result: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="fetch-model-") as scratch:
        for filename in model.files:
            path = download(model.repo, model.revision, filename, Path(scratch))
            result[filename] = sha256_of(path)
            path.unlink()
    return result


def main(argv: list[str], download: Download = hub_download) -> int:
    try:
        if len(argv) == 3 and argv[1] == "--hash":
            for filename, digest in hash_files(argv[2], download).items():
                print(f"{digest}  {filename}")
            return 0
        if len(argv) != 3:
            print("usage: fetch-model.py MODEL TARGET_DIR", file=sys.stderr)
            return 2
        snapshot = fetch(argv[1], Path(argv[2]), download)
    except FetchError as exc:
        print(f"fetch-model: {exc}", file=sys.stderr)
        return 1
    count = sum(1 for p in snapshot.rglob("*") if p.is_file())
    print(f"fetch-model: {argv[1]}: {count} files checked, in {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
