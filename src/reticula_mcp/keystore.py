"""Private keys encrypted at rest, with a secret kept outside the database.

The secret is 32 random bytes, base64, in the file RETICULA_KEY_FILE
(``reticula-mcp key init`` writes one). It never enters the database or a
dump. From it come two values, kept apart:

- the cipher key: HKDF-SHA256(secret, info=b"reticula private keys v1"),
  for AES-256-GCM;
- the key id: SHA-256(b"reticula key id v1" || secret)[:4], stored with each
  value, so that a wrong secret is noticed before anything is decrypted.

A stored private key is then

    0x01 (version) || key id (4) || nonce (12) || ciphertext and tag (48)

65 bytes, and the associated data b"reticula-key-v1" || account id (8 bytes)
|| key id (``root``, ``k1``, ``e1``) binds it to its row: a value copied to
another account or key slot does not decrypt. A plaintext key is 32 bytes;
migration 0355 lets the column hold nothing else.

Only the signing path (records.sign_and_append), account creation
(accounts._insert_account) and the operator commands (cli) use this module.
The cipher key lives in a module-private object; no function returns it.

RETICULA_KEY_FILE=none keeps keys unencrypted, for tests and development
only, and says so at every start.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, INTERNAL, ReticulaError

log = logging.getLogger(__name__)

VERSION = 1
NONE = "none"
SECRET_BYTES = 32
PLAIN_LENGTH = 32
_KID_LENGTH = 4
_NONCE_LENGTH = 12
SEALED_LENGTH = 1 + _KID_LENGTH + _NONCE_LENGTH + PLAIN_LENGTH + 16
_INFO = b"reticula private keys v1"
_KID_PREFIX = b"reticula key id v1"
_AD_PREFIX = b"reticula-key-v1"


class KeyError_(ReticulaError):
    """The secret is missing, unreadable, wrong for the stored keys, or a value
    does not decrypt."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(BAD_INPUT, message, hint=hint)


@dataclass(frozen=True)
class _Sealer:
    kid: bytes
    aead: AESGCM


def _sealer(secret: bytes) -> _Sealer:
    if len(secret) != SECRET_BYTES:
        raise KeyError_(f"the key file must hold {SECRET_BYTES} bytes, base64")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
        secret
    )
    kid = hashlib.sha256(_KID_PREFIX + secret).digest()[:_KID_LENGTH]
    return _Sealer(kid, AESGCM(key))


# The configured state of this process: None (not configured), the opt-out
# NONE, or the sealer. Module-private on purpose.
_state: _Sealer | str | None = None


def read_secret(path: str | os.PathLike[str]) -> bytes:
    """The secret in ``path``. Refused when the file is missing, when its
    group or others have any access to it (mode & 0o077), when this process
    cannot read it, or when it is not 32 bytes of base64 (whitespace around
    it is fine: ``openssl rand -base64 32`` makes a valid file)."""
    p = Path(path)
    fix = f"chown <server uid>:<server gid> {p} && chmod 600 {p}"
    try:
        info = p.stat()
    except OSError as exc:
        raise KeyError_(
            f"RETICULA_KEY_FILE {str(p)!r} does not exist",
            hint="`reticula-mcp key init --file <path>` writes one, or"
            " `(umask 077; openssl rand -base64 32 > <path>)`",
        ) from exc
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise KeyError_(
            f"RETICULA_KEY_FILE {str(p)!r} has mode {mode:04o}: its group or"
            " others can use it",
            hint=fix,
        )
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise KeyError_(
            f"cannot read {str(p)!r} as uid {os.getuid()}",
            hint=fix,
        ) from exc
    try:
        text = "".join(raw.decode("ascii").split())
        secret = base64.b64decode(text, validate=True)
    except (ValueError, UnicodeDecodeError) as exc:
        raise KeyError_(f"RETICULA_KEY_FILE {str(p)!r} is not base64") from exc
    if len(secret) != SECRET_BYTES:
        raise KeyError_(f"RETICULA_KEY_FILE must hold {SECRET_BYTES} bytes, base64")
    return secret


def configure(key_file: str | None) -> None:
    """Set this process up from RETICULA_KEY_FILE: a path, or ``none``.
    Refused without one: no start falls back to unencrypted keys."""
    global _state
    if key_file is None or not key_file.strip():
        raise KeyError_(
            "RETICULA_KEY_FILE is not set: private keys are encrypted with a secret"
            " kept outside the database",
            hint="`reticula-mcp key init --file <path>`, then set RETICULA_KEY_FILE"
            " to that path (RETICULA_KEY_FILE=none keeps keys unencrypted, for"
            " development only)",
        )
    if key_file.strip().lower() == NONE:
        log.warning(
            "RETICULA_KEY_FILE=none: private keys are stored unencrypted in the"
            " database (for development and tests only)"
        )
        _state = NONE
        return
    _state = _sealer(read_secret(key_file))


def configure_secret(secret: bytes) -> None:
    """For tests: set up from a secret held in memory."""
    global _state
    _state = _sealer(secret)


def is_configured() -> bool:
    return _state is not None


def encrypting() -> bool:
    return isinstance(_state, _Sealer)


def _ad(account_id: int, key_id: str) -> bytes:
    return _AD_PREFIX + account_id.to_bytes(8, "big") + key_id.encode()


def _seal(sealer: _Sealer, account_id: int, key_id: str, raw: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    sealed = sealer.aead.encrypt(nonce, raw, _ad(account_id, key_id))
    return bytes([VERSION]) + sealer.kid + nonce + sealed


def _open(sealer: _Sealer, account_id: int, key_id: str, stored: bytes) -> bytes:
    if len(stored) != SEALED_LENGTH or stored[0] != VERSION:
        raise KeyError_("a stored private key is not in a known form")
    if stored[1 : 1 + _KID_LENGTH] != sealer.kid:
        raise KeyError_(
            "a stored private key was encrypted with another secret",
            hint="RETICULA_KEY_FILE names the wrong file",
        )
    nonce = stored[1 + _KID_LENGTH : 1 + _KID_LENGTH + _NONCE_LENGTH]
    try:
        return sealer.aead.decrypt(
            nonce, stored[1 + _KID_LENGTH + _NONCE_LENGTH :], _ad(account_id, key_id)
        )
    except Exception as exc:  # InvalidTag: tampered, or moved to another row
        raise KeyError_("a stored private key does not decrypt") from exc


def _need() -> _Sealer | str:
    if _state is None:
        raise ReticulaError(INTERNAL, "the key store is not configured")
    return _state


def encrypt(account_id: int, key_id: str, raw: bytes) -> bytes:
    """What to store for a new private key."""
    state = _need()
    if state == NONE:
        return raw
    assert isinstance(state, _Sealer)
    return _seal(state, account_id, key_id, raw)


def decrypt(account_id: int, key_id: str, stored: bytes) -> bytes:
    """The private key from what is stored. A plaintext value is refused
    while a secret is configured: run `reticula-mcp key encrypt`."""
    state = _need()
    stored = bytes(stored)
    if state == NONE:
        if len(stored) != PLAIN_LENGTH:
            raise KeyError_(
                "stored private keys are encrypted but RETICULA_KEY_FILE=none"
            )
        return stored
    assert isinstance(state, _Sealer)
    if len(stored) == PLAIN_LENGTH:
        raise KeyError_(
            "a stored private key is not encrypted",
            hint="run `reticula-mcp key encrypt` with the server stopped",
        )
    return _open(state, account_id, key_id, stored)


def check_database(conn: Conn) -> None:
    """At start: the stored keys fit the configuration, or the start is
    refused. With a secret, every key is encrypted with it; with none, every
    key is plaintext."""
    state = _need()
    rows = conn.execute(
        "SELECT count(*) FILTER (WHERE octet_length(private_key) = %s) AS plain,"
        " count(*) FILTER (WHERE octet_length(private_key) = %s"
        "   AND substring(private_key from 2 for 4) = %s) AS ours,"
        " count(*) FILTER (WHERE octet_length(private_key) = %s"
        "   AND substring(private_key from 2 for 4) <> %s) AS other"
        " FROM account_key WHERE private_key IS NOT NULL",
        (
            PLAIN_LENGTH,
            SEALED_LENGTH,
            state.kid if isinstance(state, _Sealer) else b"",
            SEALED_LENGTH,
            state.kid if isinstance(state, _Sealer) else b"",
        ),
    ).fetchone()
    plain = rows["plain"] if rows else 0
    other = rows["other"] if rows else 0
    if state == NONE:
        if (rows["ours"] if rows else 0) or other:
            raise KeyError_(
                "stored private keys are encrypted but RETICULA_KEY_FILE=none"
            )
        return
    if other:
        raise KeyError_(
            f"{other} stored private keys were encrypted with another secret",
            hint="RETICULA_KEY_FILE names the wrong file",
        )
    if plain:
        raise KeyError_(
            f"{plain} stored private keys are not encrypted",
            hint="stop the server and run `reticula-mcp key encrypt` (the upgrade"
            " step of 0.4.2)",
        )


# --------------------------------------------------------- operator commands


def init_file(path: str | os.PathLike[str]) -> None:
    """Write a new secret to ``path``, readable by its owner only. Refused when
    the file exists: a secret is never overwritten."""
    p = Path(path)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(base64.b64encode(secrets.token_bytes(SECRET_BYTES)).decode() + "\n")


@dataclass
class Counts:
    encrypted: int = 0
    already: int = 0
    accounts: int = 0


def encrypt_all(conn: Conn, *, after_account: object = None) -> Counts:
    """Encrypt every plaintext private key with the configured secret: one
    transaction per account, its rows locked. Safe to run twice: a key already
    encrypted with this secret is left alone; one encrypted with another stops
    the run. ``after_account`` is called after each account commits (tests
    use it to stop a run half way)."""
    state = _need()
    if not isinstance(state, _Sealer):
        raise KeyError_("`key encrypt` needs RETICULA_KEY_FILE to name a key file")
    counts = Counts()
    ids = [
        r["account_id"]
        for r in conn.execute(
            "SELECT DISTINCT account_id FROM account_key"
            " WHERE private_key IS NOT NULL ORDER BY account_id"
        ).fetchall()
    ]
    conn.commit()
    for account_id in ids:
        with conn.transaction():
            rows = conn.execute(
                "SELECT id, key_id, private_key FROM account_key"
                " WHERE account_id = %s AND private_key IS NOT NULL FOR UPDATE",
                (account_id,),
            ).fetchall()
            for r in rows:
                stored = bytes(r["private_key"])
                if len(stored) == PLAIN_LENGTH:
                    conn.execute(
                        "UPDATE account_key SET private_key = %s WHERE id = %s",
                        (_seal(state, account_id, r["key_id"], stored), r["id"]),
                    )
                    counts.encrypted += 1
                else:
                    _open(state, account_id, r["key_id"], stored)  # ours, or stop
                    counts.already += 1
        conn.commit()
        counts.accounts += 1
        if callable(after_account):
            after_account(account_id)
    return counts


def rotate_all(conn: Conn, new_secret: bytes) -> Counts:
    """Re-encrypt every key from the configured secret to ``new_secret``, one
    transaction per account. Safe to run twice: a key already under the new
    secret is left alone. The server is stopped meanwhile."""
    old = _need()
    if not isinstance(old, _Sealer):
        raise KeyError_("`key rotate` needs RETICULA_KEY_FILE to name the old key file")
    new = _sealer(new_secret)
    counts = Counts()
    ids = [
        r["account_id"]
        for r in conn.execute(
            "SELECT DISTINCT account_id FROM account_key"
            " WHERE private_key IS NOT NULL ORDER BY account_id"
        ).fetchall()
    ]
    conn.commit()
    for account_id in ids:
        with conn.transaction():
            rows = conn.execute(
                "SELECT id, key_id, private_key FROM account_key"
                " WHERE account_id = %s AND private_key IS NOT NULL FOR UPDATE",
                (account_id,),
            ).fetchall()
            for r in rows:
                stored = bytes(r["private_key"])
                if (
                    len(stored) == SEALED_LENGTH
                    and stored[1 : 1 + _KID_LENGTH] == new.kid
                ):
                    counts.already += 1
                    continue
                raw = _open(old, account_id, r["key_id"], stored)
                conn.execute(
                    "UPDATE account_key SET private_key = %s WHERE id = %s",
                    (_seal(new, account_id, r["key_id"], raw), r["id"]),
                )
                counts.encrypted += 1
        conn.commit()
        counts.accounts += 1
    return counts
