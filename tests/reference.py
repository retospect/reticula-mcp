"""An independent, minimal implementation of format v0 for cross-checking.

Imports NOTHING from reticula_mcp: only the standard library and the
``cryptography`` package. If this and the library disagree with the golden
vectors, one of the three is wrong. Written from docs/format.md, not by
copying the library.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().replace("=", "")


def b32(b: bytes) -> str:
    return base64.b32encode(b).decode().replace("=", "").lower()


def person_id(root_pub: bytes) -> str:
    return "p" + b32(hashlib.sha256(b"reticula/person/v0\x00" + root_pub).digest())[:26]


def hub_id(address: str) -> str:
    return "h" + b32(hashlib.sha256(b"reticula/hub/v0\x00" + address.encode()).digest())[:26]


def uuid7(ts_ms: int, rnd: bytes) -> str:
    rand_a = ((rnd[0] & 0x0F) << 8) | rnd[1]
    rand_b = int.from_bytes(rnd[2:], "big") & ((1 << 62) - 1)
    value = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    h = f"{value:032x}"
    return "-".join([h[:8], h[8:12], h[12:16], h[16:20], h[20:]])


def canon(v: object) -> bytes:
    return json.dumps(
        v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def envelope(inputs: dict) -> bytes:
    payload = canon(inputs["payload"]).decode("utf-8")
    doc = dict(inputs.get("extra", {}))
    doc.update(
        {
            "v": 0,
            "type": inputs["type"],
            "address": inputs["address"],
            "author": inputs["author"],
            "key": inputs["key"],
            "created": inputs["created"],
            "parents": inputs["parents"],
            "hub": inputs["hub"],
            "audience": inputs["audience"],
            "alg": {"hash": "sha256", "sig": "ed25519", "enc": "none"},
            "salt": "none",
            "content_hash": "sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
            "payload": payload,
        }
    )
    return canon(doc)


def signature(seed: bytes, env: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).sign(env)
