"""External paper ids: normalisation into the ``ext_key`` used to dedupe papers.

Recognised (case-insensitive), all reduced to one key per paper:

* DOIs: ``doi:10.x/y``, a bare ``10.x/y``, and ``doi.org`` / ``dx.doi.org`` URLs
  with or without scheme. Key ``doi:<lowercased doi>``.
* arXiv: ``arXiv:2101.00001``, a bare ``2101.00001`` or old-style
  ``hep-th/9901001`` (each with or without a ``vN`` suffix), and
  ``arxiv.org/abs|pdf/...`` URLs. Key ``arxiv:<lowercased id without version>``.

Anything else is kept exactly as given, trimmed (so ``ISBN:...`` and ``isbn:...``
are different keys): we do not guess at identifier schemes we do not know.
"""

from __future__ import annotations

import re
from typing import Any

from reticula_mcp.errors import BAD_INPUT, ReticulaError

_DOI_URL = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)
_BARE_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_ARXIV_URL = re.compile(r"^(?:https?://)?(?:www\.)?arxiv\.org/(?:abs|pdf)/", re.I)
_ARXIV_NEW = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$", re.IGNORECASE)
_ARXIV_OLD = re.compile(r"^[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7}(?:v\d+)?$", re.IGNORECASE)
_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)
_PDF = re.compile(r"\.pdf$", re.IGNORECASE)


def _doi(body: str) -> tuple[str, dict[str, str]]:
    body = body.strip().lower()
    if not body:
        raise ReticulaError(BAD_INPUT, "ext_id has no DOI after the prefix")
    return f"doi:{body}", {"key": f"doi:{body}", "doi": body}


def _arxiv(body: str) -> tuple[str, dict[str, str]]:
    body = _ARXIV_VERSION.sub("", _PDF.sub("", body.strip())).lower()
    if not body:
        raise ReticulaError(BAD_INPUT, "ext_id has no arXiv id after the prefix")
    return f"arxiv:{body}", {"key": f"arxiv:{body}", "arxiv": body}


def normalize_ext_id(ext_id: str) -> tuple[str, dict[str, str]]:
    """Return ``(ext_key, ext)`` for an external id (see the module docstring).

    ``ext`` is the ``ext`` member of the atom payload (``key`` plus ``doi`` or
    ``arxiv`` when known). Idempotent: normalising a key gives the same key.
    """
    text = ext_id.strip()
    if not text:
        raise ReticulaError(BAD_INPUT, "ext_id is empty")
    lowered = text.lower()
    if lowered.startswith("doi:"):
        body = text[4:].strip()
        return _doi(_DOI_URL.sub("", body))
    if _DOI_URL.match(text):
        return _doi(_DOI_URL.sub("", text))
    if _BARE_DOI.match(text):
        return _doi(text)
    if lowered.startswith("arxiv:"):
        body = text[6:].strip()
        return _arxiv(_ARXIV_URL.sub("", body))
    if _ARXIV_URL.match(text):
        return _arxiv(_ARXIV_URL.sub("", text))
    if _ARXIV_NEW.match(text) or _ARXIV_OLD.match(text):
        return _arxiv(text)
    return text, {"key": text}


def ext_key_from_payload(payload: dict[str, Any]) -> str | None:
    """The ``ext_key`` of an atom payload (used when materialising).

    Derived by normalising what the signed payload says, not copied from it, so
    a rebuild re-keys stubs written under an older normalisation.
    """
    ext = payload.get("ext")
    if not isinstance(ext, dict):
        return None
    candidates: list[str] = []
    for field, prefix in (("key", ""), ("doi", "doi:"), ("arxiv", "arxiv:")):
        value = ext.get(field)
        if isinstance(value, str) and value.strip():
            candidates.append(prefix + value)
    for candidate in candidates:
        try:
            return normalize_ext_id(candidate)[0]
        except ReticulaError:
            continue
    return None
