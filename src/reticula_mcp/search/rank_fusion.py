"""Reciprocal-rank fusion of ranked candidate lists (lifted from precis-mcp)."""

from collections.abc import Hashable, Iterable, Mapping


def reciprocal_rank_fusion[Label: Hashable, Key: Hashable](
    legs: Iterable[tuple[Label, Iterable[tuple[Key, float]]]],
    *,
    k: int = 60,
    weights: Mapping[Label, float] | None = None,
) -> tuple[dict[Key, float], dict[Key, dict[Label, tuple[int, float]]]]:
    """Return fused scores and each hit's zero-based rank and score per leg.

    A hit at zero-based rank ``r`` of a leg adds ``weight / (k + r + 1)``;
    ``weights`` maps a leg's label to its weight (1.0 when absent)."""
    totals: dict[Key, float] = {}
    provenance: dict[Key, dict[Label, tuple[int, float]]] = {}
    for label, rows in legs:
        weight = 1.0 if weights is None else weights.get(label, 1.0)
        for rank, (key, score) in enumerate(rows):
            sources = provenance.setdefault(key, {})
            if label in sources:
                continue
            totals[key] = totals.get(key, 0.0) + weight / (k + rank + 1)
            sources[label] = (rank, score)
    return totals, provenance
