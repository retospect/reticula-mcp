"""External paper ids: normalisation into the ``ext_key`` used to dedupe papers."""

from __future__ import annotations

import re
from typing import Any

from reticula_mcp.errors import BAD_INPUT, ReticulaError

_DOI_URLS = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
)
_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)


def _strip_doi_url(text: str) -> str | None:
    lowered = text.lower()
    for prefix in _DOI_URLS:
        if lowered.startswith(prefix):
            return text[len(prefix) :]
    return None


def normalize_ext_id(ext_id: str) -> tuple[str, dict[str, str]]:
    """Return ``(ext_key, ext)`` for an external id.

    * ``doi:`` ids are lowercased, any ``https://doi.org/`` prefix removed;
    * ``arxiv:`` ids lose a trailing version suffix (``v2``);
    * anything else is kept as given, trimmed.

    ``ext`` is the ``ext`` member of the atom payload (``key`` plus ``doi`` or
    ``arxiv`` when known).
    """
    text = ext_id.strip()
    if not text:
        raise ReticulaError(BAD_INPUT, "ext_id is empty")
    lowered = text.lower()
    body: str | None
    if lowered.startswith("doi:"):
        body = text[4:].strip()
        body = _strip_doi_url(body) or body
    else:
        body = _strip_doi_url(text)
    if body is not None:
        body = body.strip().lower()
        if not body:
            raise ReticulaError(BAD_INPUT, "ext_id has no DOI after the prefix")
        return f"doi:{body}", {"key": f"doi:{body}", "doi": body}
    if lowered.startswith("arxiv:"):
        body = _ARXIV_VERSION.sub("", text[6:].strip())
        if not body:
            raise ReticulaError(BAD_INPUT, "ext_id has no arXiv id after the prefix")
        return f"arxiv:{body}", {"key": f"arxiv:{body}", "arxiv": body}
    return text, {"key": text}


def ext_key_from_payload(payload: dict[str, Any]) -> str | None:
    """The ``ext_key`` of an atom payload (used when materialising)."""
    ext = payload.get("ext")
    if not isinstance(ext, dict):
        return None
    key = ext.get("key")
    if isinstance(key, str) and key:
        return key
    for field, prefix in (("doi", "doi:"), ("arxiv", "arxiv:")):
        value = ext.get(field)
        if isinstance(value, str) and value:
            return prefix + value
    return None
