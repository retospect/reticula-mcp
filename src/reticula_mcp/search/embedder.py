"""Embedders: turn text into L2-normalised float vectors for the local index.

Embeddings are local, rebuildable and never shared. ``RETICULA_EMBEDDER``:
``hash`` = the dependency-free deterministic embedder below (tests, CI; it
captures word overlap, not meaning); ``fastembed`` = a small real model on CPU
(optional extra ``reticula-mcp[fastembed]``); ``auto`` = fastembed when the
package is installed, else hash.

The heavy library is imported only when a fastembed embedder is *used* (never
at server start-up), the model is loaded on the first ``embed`` call, and one
instance is shared by every session of the process.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import re
import struct
import threading
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from reticula_mcp.config import Settings
from reticula_mcp.errors import BAD_INPUT, ReticulaError

DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
# Output sizes of models we know, so ``dim`` never forces a model load.
_KNOWN_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}
_WORD = re.compile(r"\w+", re.UNICODE)


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into L2-normalised float vectors.

    Optional attributes the search code reads with ``getattr``:
    ``cheap`` (True when embedding is fast enough to do inside a write),
    ``max_distance`` (cosine distance beyond which a semantic hit is noise),
    ``embed_query`` (asymmetric models embed a query differently).
    """

    @property
    def model(self) -> str:
        """Stable name; stored in ``embedding.model``."""

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _unit(floats: list[float]) -> list[float]:
    norm = math.sqrt(sum(f * f for f in floats))
    if norm == 0.0:
        return floats
    return [f / norm for f in floats]


class HashEmbedder:
    """Deterministic, dependency-free embeddings by feature hashing.

    Each word (and the first four letters of longer words, a crude stem) adds a
    signed bump at a hashed position; the result is normalised. Texts that share
    words land close together, unrelated texts are near-orthogonal. Not semantic:
    a paraphrase with no shared word is not close. Same input, same vector, on
    every machine.
    """

    cheap = True
    max_distance = 0.8  # cosine similarity below 0.2 is noise at this size

    def __init__(self, dim: int = 256, model: str = "hash") -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._dim = dim
        self._model = f"{model}-{dim}"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _bump(self, vec: list[float], token: str, weight: float) -> None:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        (n,) = struct.unpack("<Q", digest[:8])
        vec[n % self._dim] += weight if (digest[8] & 1) else -weight

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for w in _WORD.findall(text.lower()):
            self._bump(vec, w, 1.0)
            if len(w) > 4:
                self._bump(vec, "~" + w[:4], 0.5)
        if not any(vec):  # no words: a fixed deterministic direction
            self._bump(vec, "\x00empty", 1.0)
        return _unit(vec)


class FastEmbedEmbedder:
    """A small local model (default BAAI/bge-small-en-v1.5, 384 dimensions)
    through ``fastembed`` (ONNX runtime, CPU). Loaded lazily, once per process."""

    cheap = False
    max_distance = 0.45  # bge models compress cosine similarity into ~0.4-1.0

    def __init__(self, model_name: str | None = None) -> None:
        self._name = model_name or DEFAULT_FASTEMBED_MODEL
        self._engine: Any = None
        self._dim: int | None = _KNOWN_DIMS.get(self._name)
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return f"fastembed:{self._name}"

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def _load(self) -> Any:
        with self._lock:
            if self._engine is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError as exc:  # pragma: no cover - extra missing
                    raise ReticulaError(
                        BAD_INPUT,
                        "RETICULA_EMBEDDER=fastembed but fastembed is not installed",
                        hint="pip install 'reticula-mcp[fastembed]'",
                    ) from exc
                self._engine = TextEmbedding(model_name=self._name, threads=2)
            return self._engine

    @staticmethod
    def _floats(vectors: Any) -> list[list[float]]:
        return [_unit([float(x) for x in v]) for v in vectors]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._floats(self._load().embed(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._floats(self._load().query_embed(text))[0]


def fastembed_installed() -> bool:
    return importlib.util.find_spec("fastembed") is not None


@lru_cache(maxsize=8)
def _build(kind: str, model_name: str | None) -> Embedder:
    if kind == "hash":
        return HashEmbedder()
    return FastEmbedEmbedder(model_name)


def get_embedder(settings: Settings) -> Embedder:
    """The configured embedder; one shared instance per configuration.

    ``auto`` picks fastembed when the package is installed, else hash. Nothing
    heavy is imported here: the model loads on the first ``embed`` call.
    """
    choice = settings.embedder
    if choice == "auto":
        choice = "fastembed" if fastembed_installed() else "hash"
    if choice == "fastembed" and not fastembed_installed():
        raise ReticulaError(
            BAD_INPUT,
            "RETICULA_EMBEDDER=fastembed but fastembed is not installed",
            hint="pip install 'reticula-mcp[fastembed]', or RETICULA_EMBEDDER=hash",
        )
    if choice not in ("hash", "fastembed"):
        raise ReticulaError(BAD_INPUT, f"unknown embedder {settings.embedder!r}")
    return _build(choice, settings.embed_model if choice == "fastembed" else None)


def embed_query(embedder: Embedder, text: str) -> list[float]:
    """Embed one search query (asymmetric models get their query form)."""
    special = getattr(embedder, "embed_query", None)
    if special is not None:
        result: list[float] = special(text)
        return result
    return embedder.embed([text])[0]
