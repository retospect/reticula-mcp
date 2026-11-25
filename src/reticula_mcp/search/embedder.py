"""Embedder interface. The search lane owns this file and may extend it.

Embeddings are local, rebuildable and never shared. ``RETICULA_EMBEDDER``:
``hash`` = the dependency-free deterministic embedder below (tests, CI);
``fastembed`` = a real local model (search lane, optional extra);
``auto`` = fastembed when installed, else hash.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol, runtime_checkable

from reticula_mcp.config import Settings
from reticula_mcp.errors import BAD_INPUT, ReticulaError


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into L2-normalised float vectors."""

    @property
    def model(self) -> str:
        """Stable name; stored in ``embedding.model``."""

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic, dependency-free pseudo-embeddings (not semantic)."""

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

    def _one(self, text: str) -> list[float]:
        floats: list[float] = []
        counter = 0
        seed = text.encode("utf-8")
        while len(floats) < self._dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
            counter += 1
            for i in range(0, 32, 4):
                if len(floats) >= self._dim:
                    break
                (n,) = struct.unpack("<I", digest[i : i + 4])
                floats.append(n / 2**31 - 1.0)
        norm = math.sqrt(sum(f * f for f in floats)) or 1.0
        return [f / norm for f in floats]


def get_embedder(settings: Settings) -> Embedder:
    """The configured embedder. Foundation: hash only; the search lane adds fastembed."""
    if settings.embedder in ("auto", "hash"):
        return HashEmbedder()
    raise ReticulaError(
        BAD_INPUT,
        f"embedder {settings.embedder!r} is not available yet",
        hint="owned by the search lane; use RETICULA_EMBEDDER=hash",
    )
