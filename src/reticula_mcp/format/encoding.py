"""Text encodings used by format v0.

``b64u``: base64url (RFC 4648 section 5) without padding.
``b32``: base32 (RFC 4648 section 6), lowercased, without padding.
"""

from __future__ import annotations

import base64
import re

_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")


def b64u(data: bytes) -> str:
    """Encode ``data`` as unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Decode unpadded base64url strictly (no padding, no foreign characters).

    Raises ``ValueError`` on anything that is not the canonical encoding of
    some byte string, so that two different texts never decode to the same
    bytes.
    """
    if not _B64U_RE.match(text) or len(text) % 4 == 1:
        raise ValueError("not canonical unpadded base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u(data) != text:
        raise ValueError("not canonical unpadded base64url")
    return data


def b32(data: bytes) -> str:
    """Encode ``data`` as lowercase unpadded base32."""
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()
