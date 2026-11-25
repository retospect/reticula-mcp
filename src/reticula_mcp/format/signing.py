"""Key generation, Ed25519 signing, and the text form of a signed record.

Signatures are over the stored envelope bytes exactly as received. Nothing
here re-serialises an envelope.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from reticula_mcp.format.encoding import b64u, b64u_decode

ALG_SIGN = "ed25519"
ALG_ENCRYPT = "x25519"
SIGNATURE_LEN = 64

_RAW = serialization.Encoding.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw
_RAW_PUB = serialization.PublicFormat.Raw
_NO_ENC = serialization.NoEncryption()


def generate_signing_key(seed: bytes | None = None) -> tuple[bytes, bytes]:
    """Return ``(private_raw32, public_raw32)`` for Ed25519.

    ``seed`` (32 bytes) makes the key deterministic, for vectors and tests.
    """
    key = (
        Ed25519PrivateKey.generate()
        if seed is None
        else Ed25519PrivateKey.from_private_bytes(seed)
    )
    return (
        key.private_bytes(_RAW, _RAW_PRIV, _NO_ENC),
        key.public_key().public_bytes(_RAW, _RAW_PUB),
    )


def generate_encryption_key() -> tuple[bytes, bytes]:
    """Return ``(private_raw32, public_raw32)`` for X25519 (slot unused in v0)."""
    key = X25519PrivateKey.generate()
    return (
        key.private_bytes(_RAW, _RAW_PRIV, _NO_ENC),
        key.public_key().public_bytes(_RAW, _RAW_PUB),
    )


def sign(private_key: bytes, message: bytes) -> bytes:
    """Ed25519 signature (64 raw bytes) over ``message``."""
    return Ed25519PrivateKey.from_private_bytes(private_key).sign(message)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True iff ``signature`` is a valid Ed25519 signature of ``message``."""
    if len(signature) != SIGNATURE_LEN or len(public_key) != 32:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except InvalidSignature:
        return False
    return True


def encode_signed(envelope: bytes, signature: bytes) -> str:
    """Text form for export: ``b64u(envelope) + "." + b64u(signature)``."""
    return f"{b64u(envelope)}.{b64u(signature)}"


def decode_signed(text: str) -> tuple[bytes, bytes]:
    """Inverse of :func:`encode_signed`; ``ValueError`` when malformed."""
    head, sep, tail = text.partition(".")
    if not sep or "." in tail:
        raise ValueError("signed record text must be '<b64u>.<b64u>'")
    return b64u_decode(head), b64u_decode(tail)


def verify_signed(text: str, public_key: bytes) -> bool:
    """Verify a text-form record against ``public_key`` (no re-serialising)."""
    try:
        envelope, signature = decode_signed(text)
    except ValueError:
        return False
    return verify(public_key, envelope, signature)
