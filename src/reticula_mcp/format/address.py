"""Identifiers: person ID, node UUID (UUIDv7), address, hub ID.

All derivations are pure functions of their inputs; the two sources of
nondeterminism in UUIDv7 (clock, randomness) are injectable so the golden
vectors are reproducible.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass

from reticula_mcp.format.encoding import b32

PERSON_DOMAIN = b"reticula/person/v0\x00"
HUB_DOMAIN = b"reticula/hub/v0\x00"
ADDRESS_SCHEME = "ret:"

_PERSON_RE = re.compile(r"^p[a-z2-7]{26}$")
_HUB_RE = re.compile(r"^h[a-z2-7]{26}$")
_UUID7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def person_id_from_root_key(root_public_key: bytes) -> str:
    """``"p" + b32(sha256(domain + raw 32-byte root public key))[:26]``."""
    if len(root_public_key) != 32:
        raise ValueError("root public key must be 32 raw bytes")
    return "p" + b32(hashlib.sha256(PERSON_DOMAIN + root_public_key).digest())[:26]


def hub_id_from_address(hub_address: str) -> str:
    """Opaque hub ID: ``"h" + b32(sha256(domain + utf8(hub address)))[:26]``."""
    digest = hashlib.sha256(HUB_DOMAIN + hub_address.encode("utf-8")).digest()
    return "h" + b32(digest)[:26]


def uuid7(timestamp_ms: int | None = None, random_bytes: bytes | None = None) -> str:
    """Return a UUIDv7 (RFC 9562) as lowercase hyphenated text.

    ``timestamp_ms``: Unix milliseconds (default: now), 48 bits.
    ``random_bytes``: exactly 10 bytes (default: ``os.urandom``). Layout:
    ``rand_a`` (12 bits) = low nibble of byte 0 and byte 1; ``rand_b``
    (62 bits) = low 6 bits of byte 2 and bytes 3..9.
    """
    ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("timestamp_ms must fit in 48 bits")
    rnd = os.urandom(10) if random_bytes is None else random_bytes
    if len(rnd) != 10:
        raise ValueError("random_bytes must be exactly 10 bytes")
    raw = bytearray(ms.to_bytes(6, "big") + rnd[:2] + rnd[2:])
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    h = raw.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def is_person_id(value: object) -> bool:
    return isinstance(value, str) and _PERSON_RE.fullmatch(value) is not None


def is_hub_id(value: object) -> bool:
    return isinstance(value, str) and _HUB_RE.fullmatch(value) is not None


def is_uuid7(value: object) -> bool:
    return isinstance(value, str) and _UUID7_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class Address:
    """``ret:<person_id>/<uuid>``: fixed at creation, never reused."""

    person_id: str
    uuid: str

    def __str__(self) -> str:
        return f"{ADDRESS_SCHEME}{self.person_id}/{self.uuid}"


def format_address(person_id: str, uuid: str) -> str:
    """Build and validate an address string."""
    addr = Address(person_id, uuid)
    parse_address(str(addr))
    return str(addr)


def parse_address(text: str) -> Address:
    """Parse and validate an address; ``ValueError`` when malformed."""
    if not isinstance(text, str) or not text.startswith(ADDRESS_SCHEME):
        raise ValueError(f"address must start with {ADDRESS_SCHEME!r}")
    person_id, sep, uuid = text[len(ADDRESS_SCHEME) :].partition("/")
    if not sep or not is_person_id(person_id) or not is_uuid7(uuid):
        raise ValueError(f"malformed address: {text!r}")
    return Address(person_id, uuid)


def is_valid_address(text: object) -> bool:
    if not isinstance(text, str):
        return False
    try:
        parse_address(text)
    except ValueError:
        return False
    return True


def new_address(
    person_id: str,
    *,
    timestamp_ms: int | None = None,
    random_bytes: bytes | None = None,
) -> str:
    """Mint a fresh address under ``person_id``."""
    return format_address(person_id, uuid7(timestamp_ms, random_bytes))
