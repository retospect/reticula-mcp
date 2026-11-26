"""Embedders: turn text into L2-normalised float vectors for the local index.

Embeddings are local, rebuildable and never shared. ``RETICULA_EMBEDDER``:
``hash`` = the dependency-free deterministic embedder below (tests, CI; it
captures word overlap, not meaning); ``fastembed`` = a small real model on CPU
(optional extra ``reticula-mcp[fastembed]``); ``auto`` = fastembed when the
package is installed, else hash.

Loading the real model costs time (measured on a laptop: import 0.3 s, load
0.1 s from the local cache, about 3 s plus the 64 MB download the first time)
and can fail (no network on first use). So nothing here loads it on the calling
thread unless the caller asks: ``get_embedder`` imports nothing heavy, ``warm``
starts the load in a background thread and returns at once, ``ready`` says
whether vectors can be computed without waiting. A search made while the model
is not ready answers from keywords; a write never touches the model at all
(see ``hooks.py`` and ``worker.py``). One instance is shared by the process.

The model files live in ``FASTEMBED_CACHE_PATH`` when that is set, else in
``~/.cache/reticula/fastembed`` (not the system temp directory, fastembed's
default, which a container loses when it is recreated).
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import math
import os
import re
import struct
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from reticula_mcp.config import Settings
from reticula_mcp.errors import BAD_INPUT, ReticulaError

# Reto chose bge-base with its query instruction (debug note baada5c5): it
# finds paraphrases that bge-small cuts. bge-small stays a choice
# (RETICULA_EMBED_MODEL), for a small machine.
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-base-en-v1.5"


@dataclass(frozen=True)
class ModelSettings:
    """What the search needs to know of a model besides its name.

    ``dim``: its output size, so that ``dim`` never forces a model load.
    ``max_distance``: the cosine distance beyond which a vector match is
    noise (a cut at similarity 1 - max_distance). Measured per model on the
    relevance set of tests/test_search_relevance.py padded with 6,000 notes:
    the lowest cut at which no nonsense query is answered. The margin is a
    hundredth or so, set on six nonsense queries; an account of many more
    notes can bring nonsense higher. ``query_prefix``: put before every
    query, never before a note."""

    dim: int
    max_distance: float
    query_prefix: str = ""


# bge's instruction for queries that look for passages
_BGE_QUERY = "Represent this sentence for searching relevant passages: "
# a model whose cut was not measured here gets the strictest measured one,
# bge-small's, and the log says so
UNMEASURED_MAX_DISTANCE = 0.35
KNOWN_MODELS: dict[str, ModelSettings] = {
    # similarity 0.65: nonsense reaches 0.63 on the relevance set (0.68
    # among 6,000 notes); 4 of 18 paraphrases are cut there (see the class)
    "BAAI/bge-small-en-v1.5": ModelSettings(384, 0.35),
    # with its query instruction, similarity 0.50: nonsense reaches 0.451 on
    # the relevance set and 0.491 among 6,000 notes; 18 of 23 paraphrases
    # and 3 of the 5 of debug note baada5c5 are above it there
    "BAAI/bge-base-en-v1.5": ModelSettings(768, 0.50, _BGE_QUERY),
    # plain queries, similarity 0.59: nonsense reaches 0.582 among 6,000
    # notes; 18 of 23 paraphrases and 4 of the 5 of baada5c5 are above it
    "mixedbread-ai/mxbai-embed-large-v1": ModelSettings(1024, 0.41),
    # its size is known; its cut was not measured
    "sentence-transformers/all-MiniLM-L6-v2": ModelSettings(
        384, UNMEASURED_MAX_DISTANCE
    ),
}
_WORD = re.compile(r"\w+", re.UNICODE)
# After a failed load, ``warm`` waits this long before it tries again; the wait
# doubles with every failure in a row, up to the maximum.
RETRY_FIRST = 30.0
RETRY_MAX = 900.0

log = logging.getLogger("reticula_mcp.search")


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into L2-normalised float vectors.

    Optional attributes the search code reads with ``getattr``:
    ``cheap`` (True when embedding is fast enough to do inside a write),
    ``max_distance`` (cosine distance beyond which a semantic hit is noise),
    ``embed_query`` (asymmetric models embed a query differently),
    ``ready`` / ``warm`` / ``load`` / ``load_error`` (models that take time to
    load; see :func:`is_ready`, :func:`warm`).
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
    # Measured on tests/test_search_relevance.py: at 0.65 a nonsense query gets
    # nothing and a question on a subject no note covers gets 0.2 notes on
    # average (5.2 at 0.8); exact titles and keywords are found either way.
    max_distance = 0.65

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


def model_cache_dir() -> str | None:
    """Where model files are kept: ``FASTEMBED_CACHE_PATH``, else
    ``~/.cache/reticula/fastembed`` (``XDG_CACHE_HOME`` honoured). None when that
    directory cannot be created or written: fastembed then uses its own default
    in the system temp directory, and the model is fetched again whenever that
    is emptied."""
    configured = os.environ.get("FASTEMBED_CACHE_PATH", "").strip()
    if configured:
        return configured
    base = os.environ.get("XDG_CACHE_HOME", "").strip() or str(Path.home() / ".cache")
    path = Path(base) / "reticula" / "fastembed"
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise PermissionError(str(path))
    except OSError as exc:
        log.warning(
            "model cache %s is not writable (%s); using the system temp directory",
            path,
            exc,
        )
        return None
    return str(path)


class FastEmbedEmbedder:
    """A local model (default BAAI/bge-base-en-v1.5, 768 dimensions)
    through ``fastembed`` (ONNX runtime, CPU). Loaded once per process, by
    :meth:`load` (blocking) or :meth:`warm` (background)."""

    cheap = False
    # The cut and the query prefix come with the model (KNOWN_MODELS).
    # Measured for the default model on tests/test_search_relevance.py (run it
    # with RETICULA_SEARCH_MEASURE=1). bge models compress cosine similarity
    # into about 0.4 to 1.0: the nearest note to a nonsense query is at
    # distance 0.37 to 0.44, so 0.35 is the largest value at which nonsense
    # returns nothing. It costs the paraphrases whose note is further away
    # (4 of 18 in the set). Another model needs its own measurement.
    max_distance = 0.35

    def __init__(self, model_name: str | None = None) -> None:
        self._name = model_name or DEFAULT_FASTEMBED_MODEL
        known = KNOWN_MODELS.get(self._name)
        if known is None:
            log.warning(
                "embedding model %s has not been measured here; its matches by"
                " meaning are cut at distance %.2f, the strictest cut measured",
                self._name,
                UNMEASURED_MAX_DISTANCE,
            )
        self.max_distance = known.max_distance if known else UNMEASURED_MAX_DISTANCE
        self.query_prefix = known.query_prefix if known else ""
        self._engine: Any = None
        self._dim: int | None = known.dim if known else None
        self._load_lock = threading.Lock()  # one load at a time
        self._state_lock = threading.Lock()  # guards the fields below
        self._run_lock = threading.Lock()  # one inference at a time
        self._loader: threading.Thread | None = None
        self._failures = 0
        self._retry_at = 0.0
        self.load_error: str | None = None
        self.load_seconds: float | None = None

    @property
    def model(self) -> str:
        return f"fastembed:{self._name}"

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    @property
    def ready(self) -> bool:
        """True once the model is in memory: ``embed`` will not wait for a load."""
        return self._engine is not None

    def load(self) -> None:
        """Load the model on the calling thread (seconds; a download the first
        time). Raises when it cannot be loaded. Safe to call from any thread."""
        self._load()

    def warm(self) -> None:
        """Start loading in a background thread and return at once. Does nothing
        when the model is loaded, a load is running, or the last attempt failed
        less than the retry wait ago."""
        with self._state_lock:
            if self._engine is not None:
                return
            if self._loader is not None and self._loader.is_alive():
                return
            if time.monotonic() < self._retry_at:
                return
            self._loader = threading.Thread(
                target=self._load_quietly, name="reticula-embedder-load", daemon=True
            )
            self._loader.start()

    def _load_quietly(self) -> None:
        try:
            self._load()
        except Exception:
            log.warning(
                "embedding model %s did not load; search answers from keywords"
                " and the load is retried later",
                self._name,
                exc_info=True,
            )

    def _load(self) -> Any:
        if self._engine is not None:
            return self._engine
        with self._load_lock:
            if self._engine is not None:
                return self._engine
            started = time.monotonic()
            try:
                try:
                    from fastembed import TextEmbedding
                except ImportError as exc:  # pragma: no cover - extra missing
                    raise ReticulaError(
                        BAD_INPUT,
                        "RETICULA_EMBEDDER=fastembed but fastembed is not installed",
                        hint="pip install 'reticula-mcp[fastembed]'",
                    ) from exc
                engine = TextEmbedding(
                    model_name=self._name, threads=2, cache_dir=model_cache_dir()
                )
                # one real inference: the first one pays for graph optimisation
                list(engine.embed(["warm up"]))
            except Exception as exc:
                with self._state_lock:
                    self._failures += 1
                    wait = min(RETRY_FIRST * 2 ** (self._failures - 1), RETRY_MAX)
                    self._retry_at = time.monotonic() + wait
                    self.load_error = f"{type(exc).__name__}: {exc}"
                raise
            with self._state_lock:
                self._failures = 0
                self._retry_at = 0.0
                self.load_error = None
                self.load_seconds = time.monotonic() - started
                self._engine = engine
            log.info(
                "embedding model %s loaded in %.1fs", self._name, self.load_seconds
            )
            return engine

    @staticmethod
    def _floats(vectors: Any) -> list[list[float]]:
        return [_unit([float(x) for x in v]) for v in vectors]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        engine = self._load()
        with self._run_lock:
            return self._floats(engine.embed(texts))

    def embed_query(self, text: str) -> list[float]:
        # fastembed adds no instruction of its own. For bge-small none is
        # added: on the relevance set the recommended one moved a single query
        # and made no cutoff safer. bge-base gains from it (KNOWN_MODELS).
        engine = self._load()
        with self._run_lock:
            return self._floats(engine.query_embed(self.query_prefix + text))[0]


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


def is_ready(embedder: Embedder) -> bool:
    """Can ``embedder`` compute vectors now without a long wait? Embedders with
    no ``ready`` attribute (the hash embedder) always can."""
    return bool(getattr(embedder, "ready", True))


def warm(embedder: Embedder) -> None:
    """Ask a slow embedder to start loading in the background; returns at once."""
    start = getattr(embedder, "warm", None)
    if start is not None:
        start()


def load_error(embedder: Embedder) -> str | None:
    """Why the last load failed, None when it did not (or has not been tried)."""
    error: str | None = getattr(embedder, "load_error", None)
    return error
