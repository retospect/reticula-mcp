"""The record envelope: canonical bytes, hashes, construction, lenient parsing.

Rules that matter more than any code here:

* The envelope is ONE byte string. Hashes and signatures are over those bytes.
* Producers serialise canonically (:func:`canonical_json`). Consumers NEVER
  re-serialise: they store, hash, verify and re-serve the bytes they were
  given, even when they contain fields or types this code does not know.
* ``parse_envelope`` is deliberately lenient (unknown fields, unknown types,
  unknown audiences are fine). ``check_envelope`` reports only problems that
  make a v0 record unusable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reticula_mcp.format.address import (
    is_hub_id,
    is_person_id,
    is_valid_address,
    parse_address,
)

FORMAT_VERSION = 0

#: Record types v0 defines. Anything else is tolerated on read.
KNOWN_TYPES = ("atom", "member", "grant", "tombstone", "identity")
#: Audience values v0 defines. Anything else is tolerated on read.
KNOWN_AUDIENCES = ("hub", "private", "public")
DEFAULT_ALG: dict[str, str] = {"hash": "sha256", "sig": "ed25519", "enc": "none"}
SALT_NONE = "none"

#: Keys every v0 envelope carries.
REQUIRED_FIELDS = (
    "v",
    "type",
    "address",
    "author",
    "key",
    "created",
    "parents",
    "hub",
    "audience",
    "alg",
    "salt",
    "content_hash",
    "payload",
)

_CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("floats are not allowed in canonical JSON")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("object keys must be strings")
            _reject_floats(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_floats(v)


def canonical_text(value: Any) -> str:
    """Canonical JSON text: sorted keys, ``,`` and ``:`` separators, raw UTF-8.

    No floats, no NaN. Keys sort by Unicode code point (Python's default).
    """
    _reject_floats(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json(value: Any) -> bytes:
    """:func:`canonical_text` encoded as UTF-8 (lone surrogates are an error)."""
    return canonical_text(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(payload: bytes, salt: bytes = b"") -> str:
    """``"sha256:" + hex(sha256(salt_bytes + payload_bytes))``."""
    return "sha256:" + sha256_hex(salt + payload)


def version_hash(envelope: bytes) -> str:
    """``"sha256:" + hex(sha256(envelope_bytes))``; covers ``parents``."""
    return "sha256:" + sha256_hex(envelope)


def format_created(moment: datetime | int | None = None) -> str:
    """RFC 3339 UTC with milliseconds: ``2026-11-25T07:14:38.123Z``.

    Accepts a datetime, Unix milliseconds, or ``None`` for now.
    """
    if moment is None:
        moment = datetime.now(UTC)
    elif isinstance(moment, int):
        moment = datetime.fromtimestamp(moment / 1000, UTC)
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_created(text: str) -> datetime:
    """Parse the ``created`` field strictly; ``ValueError`` when malformed."""
    if not _CREATED_RE.match(text):
        raise ValueError(f"created must look like 2026-11-25T07:14:38.123Z: {text!r}")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def encode_payload(payload: Mapping[str, Any]) -> str:
    """The payload's own JSON text (canonical), as stored in ``payload``."""
    return canonical_text(payload)


def build_envelope(
    *,
    type: str,
    address: str,
    author: str,
    key: str,
    created: str,
    payload: Mapping[str, Any] | str,
    parents: Sequence[str] = (),
    hub: str | None = None,
    audience: str = "hub",
    salt: str = SALT_NONE,
    alg: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bytes:
    """Produce the canonical envelope bytes for a v0 record.

    ``payload`` is a dict (serialised canonically) or an already-serialised
    JSON string (kept byte for byte). ``content_hash`` is computed here
    (salt scheme ``"none"`` only). ``extra`` adds fields this version does not
    define; it cannot override a defined one.
    """
    if salt != SALT_NONE:
        raise ValueError("only salt scheme 'none' exists in v0")
    payload_text = payload if isinstance(payload, str) else encode_payload(payload)
    envelope: dict[str, Any] = dict(extra or {})
    envelope.update(
        v=FORMAT_VERSION,
        type=type,
        address=address,
        author=author,
        key=key,
        created=created,
        parents=list(parents),
        hub=hub,
        audience=audience,
        alg=dict(alg or DEFAULT_ALG),
        salt=salt,
        content_hash=content_hash(payload_text.encode("utf-8")),
        payload=payload_text,
    )
    return canonical_json(envelope)


def parse_envelope(envelope: bytes) -> dict[str, Any]:
    """Decode envelope bytes into a dict for READING. Never re-serialise it."""
    try:
        value = json.loads(envelope.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"envelope is not UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("envelope must be a JSON object")
    return value


def payload_bytes(envelope: Mapping[str, Any]) -> bytes:
    """The payload bytes the content hash covers (``enc`` none: UTF-8 text)."""
    return str(envelope["payload"]).encode("utf-8")


def payload_json(envelope: Mapping[str, Any]) -> Any:
    """Parse the payload text (``enc`` none). ``ValueError`` if it is not JSON."""
    if (envelope.get("alg") or {}).get("enc") != "none":
        raise ValueError("payload is encrypted")
    try:
        return json.loads(envelope["payload"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"payload is not JSON: {exc}") from exc


def check_envelope(envelope: Mapping[str, Any]) -> list[str]:
    """Return the problems that make this a non-usable v0 record (empty = ok).

    Unknown types, audiences and extra fields are NOT problems.
    Checks: shape and types of the defined fields, address/author agreement,
    algorithm names this implementation can verify, ``created`` shape, and
    that ``content_hash`` matches the payload.
    """
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        if name not in envelope:
            problems.append(f"missing field {name!r}")
    if problems:
        return problems
    if envelope["v"] != FORMAT_VERSION or isinstance(envelope["v"], bool):
        problems.append(f"unsupported format version {envelope['v']!r}")
    for name in ("type", "key", "audience", "salt", "payload"):
        if not isinstance(envelope[name], str):
            problems.append(f"{name} must be a string")
    if not is_valid_address(envelope["address"]):
        problems.append("address is not a valid address")
    if not is_person_id(envelope["author"]):
        problems.append("author is not a person ID")
    elif is_valid_address(envelope["address"]) and (
        parse_address(envelope["address"]).person_id != envelope["author"]
    ):
        problems.append("author must equal the person part of address in v0")
    if not isinstance(envelope["created"], str) or not _CREATED_RE.match(
        envelope["created"]
    ):
        problems.append("created is not RFC 3339 UTC with milliseconds")
    parents = envelope["parents"]
    if not isinstance(parents, list) or not all(
        isinstance(p, str) and _HASH_RE.match(p) for p in parents
    ):
        problems.append("parents must be a list of version hashes")
    hub = envelope["hub"]
    if hub is not None and not is_hub_id(hub):
        problems.append("hub must be null or a hub ID")
    alg = envelope["alg"]
    if alg != DEFAULT_ALG:
        problems.append(f"unsupported alg {alg!r} (v0 verifies {DEFAULT_ALG!r} only)")
    if envelope["salt"] != SALT_NONE:
        problems.append("unsupported salt scheme")
    if isinstance(envelope["payload"], str) and isinstance(
        envelope["content_hash"], str
    ):
        if envelope["content_hash"] != content_hash(payload_bytes(envelope)):
            problems.append("content_hash does not match payload")
    else:
        problems.append("content_hash must be a string")
    return problems
