"""Accounts, keys, passwords, bearer tokens. FROZEN signatures.

Every account has three key slots (``root``, ``k1``, ``e1``), generated at
sign-up and held (unencrypted) by the instance, a person ID derived from the
root key, and a signed identity record in the log.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg.errors
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from reticula_mcp import format as fmt
from reticula_mcp import keystore, records
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, CONFLICT, NOT_FOUND, ReticulaError

SYSTEM_HANDLE = "reticula"
SYSTEM_EMAIL = "system@reticula.invalid"
UNUSABLE_PASSWORD = "!"
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024

HANDLE_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESERVED_HANDLES = frozenset(
    [
        "abuse",
        "admin",
        "administrator",
        "all",
        "anonymous",
        "api",
        "debug",
        "everyone",
        "help",
        "hub",
        "hubs",
        "info",
        "login",
        "logout",
        "mcp",
        "me",
        "none",
        "null",
        "oauth",
        "operator",
        "postmaster",
        "public",
        "reticula",
        "root",
        "security",
        "signin",
        "signup",
        "static",
        "support",
        "system",
        "well-known",
        "www",
        "healthz",
    ]
)

#: argon2id with the library defaults; the algorithm tag lives in the hash
#: string itself, so parameters can change without a migration.
PASSWORD_HASHER = PasswordHasher()
_dummy_hash: str | None = None

_ACCOUNT_COLUMNS = (
    "id, person_id, email, handle, identity_address, verified, verified_at, status,"
    " created_at, updated_at"
)


@dataclass(frozen=True)
class Account:
    id: int
    person_id: str
    email: str
    handle: str
    identity_address: str
    verified: bool
    verified_at: datetime | None
    status: str  # active | provisional | disabled | system
    created_at: datetime
    updated_at: datetime

    @property
    def is_system(self) -> bool:
        return self.status == "system"


def _account(row: dict[str, Any]) -> Account:
    return Account(**{k: row[k] for k in _ACCOUNT_COLUMNS.replace(" ", "").split(",")})


@dataclass(frozen=True)
class TokenInfo:
    """What the bearer-token lookup knows about a token."""

    token_id: int
    account_id: int
    kind: str
    client_id: str | None
    scopes: tuple[str, ...]
    resource: str | None
    grant_id: str | None
    label: str | None
    expires_at: datetime | None


# --- lookups ----------------------------------------------------------------


def _get(conn: Conn, where: str, param: Any) -> Account | None:
    row = conn.execute(
        f"SELECT {_ACCOUNT_COLUMNS} FROM account WHERE {where}", (param,)
    ).fetchone()
    return _account(row) if row else None


def get_account(conn: Conn, account_id: int) -> Account | None:
    return _get(conn, "id = %s", account_id)


def get_account_by_person_id(conn: Conn, person_id: str) -> Account | None:
    return _get(conn, "person_id = %s", person_id)


def get_account_by_handle(conn: Conn, handle: str) -> Account | None:
    return _get(conn, "handle = %s", handle.lstrip("@"))


def get_account_by_email(conn: Conn, email: str) -> Account | None:
    return _get(conn, "email = %s", email.strip())


# --- passwords --------------------------------------------------------------


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def _burn_verify(password: str) -> None:
    """Spend the same time as a real check (unknown e-mail, unusable password)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = PASSWORD_HASHER.hash("reticula-timing-equaliser")
    try:
        PASSWORD_HASHER.verify(_dummy_hash, password)
    except VerificationError:
        pass


def set_password(conn: Conn, account: Account, password: str) -> None:
    """A new password for the account: the length rule and the list of common
    passwords as at sign-up. The caller revokes what the old one let in."""
    from reticula_mcp.auth.passwords import is_common  # here: auth imports this module

    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ReticulaError(
            BAD_INPUT,
            f"a password has {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters",
        )
    if is_common(password):
        raise ReticulaError(BAD_INPUT, "that password is one of the most common ones")
    conn.execute(
        "UPDATE account SET password_hash = %s WHERE id = %s",
        (hash_password(password), account.id),
    )


def set_status(conn: Conn, account: Account, status: str) -> None:
    conn.execute("UPDATE account SET status = %s WHERE id = %s", (status, account.id))


def authenticate(conn: Conn, email: str, password: str) -> Account | None:
    """The account for a correct e-mail + password, else None.

    ``disabled`` and ``system`` accounts never authenticate. Takes about the
    same time whether or not the e-mail exists.
    """
    row = conn.execute(
        f"SELECT {_ACCOUNT_COLUMNS}, password_hash FROM account WHERE email = %s",
        (email.strip(),),
    ).fetchone()
    if row is None or row["status"] not in ("active", "provisional"):
        _burn_verify(password)
        return None
    stored = row["password_hash"]
    try:
        PASSWORD_HASHER.verify(stored, password)
    except InvalidHashError:
        _burn_verify(password)
        return None
    except VerificationError:
        return None
    if PASSWORD_HASHER.check_needs_rehash(stored):
        conn.execute(
            "UPDATE account SET password_hash = %s WHERE id = %s",
            (hash_password(password), row["id"]),
        )
    return _account(row)


# --- handles ----------------------------------------------------------------


def slugify_handle(text: str) -> str:
    """Lowercase ``[a-z0-9_-]``, 3 to 28 characters (room for a numeric suffix)."""
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9_-]+", "-", ascii_text.lower()).strip("-_")
    slug = slug[:28].strip("-_")
    if not slug or _reserved_part(slug):
        return "user"
    return slug if len(slug) >= 3 else f"{slug}-user"


#: A new handle may not contain these anywhere (it could pass for the
#: instance's own account or an administrator's).
RESERVED_PARTS = ("reticula", "admin")


def _reserved_part(handle: str) -> bool:
    return any(part in handle for part in RESERVED_PARTS)


def is_reserved(handle: str) -> bool:
    """Reserved for new handles: one of RESERVED_HANDLES, or containing one of
    RESERVED_PARTS. Handles given before stay valid."""
    return handle in RESERVED_HANDLES or _reserved_part(handle)


def validate_handle(handle: str) -> str:
    """Normalise (lowercase, drop a leading @) and validate an explicit handle."""
    handle = handle.strip().lstrip("@").lower()
    if not HANDLE_RE.match(handle):
        raise ReticulaError(
            BAD_INPUT,
            f"invalid handle {handle!r}",
            hint="3-32 characters from a-z, 0-9, _ and -",
        )
    if is_reserved(handle):
        raise ReticulaError(BAD_INPUT, f"handle {handle!r} is reserved")
    return handle


def _candidate_handles(base: str):
    yield base
    for n in range(2, 1000):
        yield f"{base}{n}"
    while True:
        yield f"{base}{secrets.token_hex(3)}"


def _handle_taken(conn: Conn, handle: str, person_id: str | None = None) -> bool:
    """In use, reserved, or given up by someone other than ``person_id``.

    A released handle is refused exactly like one in use, so a refusal does not
    tell the caller which of the two it ran into.
    """
    if is_reserved(handle):
        return True
    if conn.execute("SELECT 1 FROM account WHERE handle = %s", (handle,)).fetchone():
        return True
    released = conn.execute(
        "SELECT person_id FROM released_handle WHERE handle = %s", (handle,)
    ).fetchone()
    return released is not None and released["person_id"] != person_id


# --- creation ---------------------------------------------------------------


def _identity_payload(
    conn: Conn,
    account_id: int,
    handle: str,
    verified: bool,
    verified_at: datetime | None,
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT key_id, use, role, alg, public_key FROM account_key"
        " WHERE account_id = %s AND revoked_at IS NULL ORDER BY id",
        (account_id,),
    ).fetchall()
    keys = []
    for r in rows:
        entry: dict[str, Any] = {
            "id": r["key_id"],
            "use": r["use"],
            "alg": r["alg"],
            "pub": fmt.b64u(bytes(r["public_key"])),
        }
        if r["role"]:
            entry["role"] = r["role"]
        keys.append(entry)
    return {
        "handle": handle,
        "keys": keys,
        "verified": {
            "value": verified,
            "at": fmt.format_created(verified_at) if verified_at else None,
        },
    }


def _publish_identity(conn: Conn, account: Account) -> records.Record:
    """Append the next identity version (signed by the root key)."""
    previous = records.head(conn, account.identity_address)
    return records.sign_and_append(
        conn,
        account,
        type="identity",
        address=account.identity_address,
        payload=_identity_payload(
            conn, account.id, account.handle, account.verified, account.verified_at
        ),
        parents=(previous.version_hash,) if previous else (),
        audience="public",
        key=records.ROOT_KEY,
    )


def _insert_account(
    conn: Conn,
    *,
    email: str,
    password_hash: str,
    handle: str | None,
    handle_base: str,
    status: str,
    verified: bool,
) -> Account:
    """Keys, account row, key rows, signed identity record. One transaction."""
    root_priv, root_pub = fmt.generate_signing_key()
    k1_priv, k1_pub = fmt.generate_signing_key()
    e1_priv, e1_pub = fmt.generate_encryption_key()
    person_id = fmt.person_id_from_root_key(root_pub)
    identity_address = fmt.new_address(person_id)
    now = datetime.now(UTC)
    row = None
    for candidate in [handle] if handle else _candidate_handles(handle_base):
        assert candidate is not None
        if _handle_taken(conn, candidate) and not (
            status == "system" and candidate == SYSTEM_HANDLE
        ):
            if handle:
                raise ReticulaError(CONFLICT, f"handle {candidate!r} is taken")
            continue
        try:
            with conn.transaction():
                row = conn.execute(
                    "INSERT INTO account (person_id, email, password_hash, handle,"
                    " identity_address, verified, verified_at, status)"
                    f" VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_ACCOUNT_COLUMNS}",
                    (
                        person_id,
                        email,
                        password_hash,
                        candidate,
                        identity_address,
                        verified,
                        now if verified else None,
                        status,
                    ),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "account_handle_key" and not handle:
                continue
            if exc.diag.constraint_name == "account_email_key":
                raise ReticulaError(
                    CONFLICT, "that e-mail address is already registered"
                ) from exc
            raise ReticulaError(CONFLICT, f"handle {candidate!r} is taken") from exc
        break
    assert row is not None
    account = _account(row)
    for key_id, use, role, alg, priv, pub in (
        ("root", "sign", "root", fmt.ALG_SIGN, root_priv, root_pub),
        ("k1", "sign", "delegate", fmt.ALG_SIGN, k1_priv, k1_pub),
        ("e1", "encrypt", None, fmt.ALG_ENCRYPT, e1_priv, e1_pub),
    ):
        conn.execute(
            "INSERT INTO account_key (account_id, key_id, use, role, alg, public_key,"
            " private_key) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                account.id,
                key_id,
                use,
                role,
                alg,
                pub,
                keystore.encrypt(account.id, key_id, priv),
            ),
        )
    _publish_identity(conn, account)
    return account


def create_account(
    conn: Conn, email: str, password: str, handle: str | None = None
) -> Account:
    """Sign up: password hash, keys, handle, signed identity, content hook.

    All in the caller's transaction. ``Conflict`` when the e-mail or an
    explicit handle is taken; ``BadInput`` for a malformed e-mail, a short
    password, an invalid or reserved handle.
    """
    email = email.strip()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ReticulaError(BAD_INPUT, "that does not look like an e-mail address")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ReticulaError(
            BAD_INPUT,
            f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters",
        )
    explicit = validate_handle(handle) if handle else None
    account = _insert_account(
        conn,
        email=email,
        password_hash=hash_password(password),
        handle=explicit,
        handle_base=slugify_handle(email.split("@", 1)[0].split("+", 1)[0]),
        status="active",
        verified=False,
    )
    from reticula_mcp.content import hooks

    hooks.on_account_created(conn, account)
    return account


def ensure_system_account(conn: Conn) -> Account:
    """The instance's own account (handle ``reticula``, status ``system``).

    Idempotent. It has no usable password and never authenticates; tokens for
    it come only from :func:`mint_token`. Does NOT call the content hook.
    """
    row = conn.execute(
        f"SELECT {_ACCOUNT_COLUMNS} FROM account WHERE status = 'system'"
    ).fetchone()
    if row:
        return _account(row)
    return _insert_account(
        conn,
        email=SYSTEM_EMAIL,
        password_hash=UNUSABLE_PASSWORD,
        handle=SYSTEM_HANDLE,
        handle_base=SYSTEM_HANDLE,
        status="system",
        verified=True,
    )


def set_handle(conn: Conn, account: Account, handle: str) -> Account:
    """Rename: updates the account and appends a new identity version.

    The old handle is recorded as released: only this person may take it back.
    """
    handle = validate_handle(handle)
    if handle == account.handle:
        return account
    if _handle_taken(conn, handle, account.person_id):
        raise ReticulaError(CONFLICT, f"handle {handle!r} is taken")
    try:
        with conn.transaction():
            row = conn.execute(
                "UPDATE account SET handle = %s, updated_at = now() WHERE id = %s"
                f" RETURNING {_ACCOUNT_COLUMNS}",
                (handle, account.id),
            ).fetchone()
            conn.execute(
                "INSERT INTO released_handle (handle, person_id) VALUES (%s, %s)"
                " ON CONFLICT (handle) DO NOTHING",
                (account.handle, account.person_id),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise ReticulaError(CONFLICT, f"handle {handle!r} is taken") from exc
    if row is None:
        raise ReticulaError(NOT_FOUND, "no such account")
    updated = _account(row)
    _publish_identity(conn, updated)
    return updated


def set_verified(conn: Conn, account: Account, verified: bool = True) -> Account:
    """Flip the verified flag and append a new identity version.

    E-mail verification is designed in and switched off: nothing in the
    foundation calls this; the sign-in lane will when it turns verification on.
    """
    row = conn.execute(
        "UPDATE account SET verified = %s, verified_at = %s, updated_at = now()"
        f" WHERE id = %s RETURNING {_ACCOUNT_COLUMNS}",
        (verified, datetime.now(UTC) if verified else None, account.id),
    ).fetchone()
    if row is None:
        raise ReticulaError(NOT_FOUND, "no such account")
    updated = _account(row)
    _publish_identity(conn, updated)
    return updated


# --- bearer tokens ----------------------------------------------------------

TOKEN_PREFIX = "ret_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token(
    conn: Conn, account: Account, label: str, ttl: timedelta | None = None
) -> str:
    """Create an access token for ``account`` and return it ONCE (stored hashed).

    Used by tests, the end-to-end script and ``reticula-mcp token``. Tokens
    minted here have no OAuth client, scope ``mcp`` and no expiry unless
    ``ttl`` is given.
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO oauth_token (token_hash, kind, account_id, scopes, label,"
        " expires_at) VALUES (%s, 'access', %s, %s, %s, %s)",
        (
            hash_token(token),
            account.id,
            ["mcp"],
            label,
            datetime.now(UTC) + ttl if ttl else None,
        ),
    )
    return token


def lookup_token(conn: Conn, token: str) -> tuple[Account, TokenInfo] | None:
    """Resolve a bearer token to ``(account, token info)``, bumping last_used_at.

    None when the token is unknown, not an access token, expired, revoked, or
    the account is disabled.
    """
    row = conn.execute(
        "UPDATE oauth_token SET last_used_at = now()"
        " WHERE token_hash = %s AND kind = 'access' AND revoked_at IS NULL"
        " AND (expires_at IS NULL OR expires_at > now())"
        " AND (grant_id IS NULL OR EXISTS (SELECT 1 FROM oauth_grant g"
        "      WHERE g.grant_id = oauth_token.grant_id AND g.revoked_at IS NULL))"
        " RETURNING id, account_id, kind, client_id, scopes, resource, grant_id,"
        " label, expires_at",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        return None
    account = get_account(conn, row["account_id"])
    if account is None or account.status == "disabled":
        return None
    info = TokenInfo(
        token_id=row["id"],
        account_id=row["account_id"],
        kind=row["kind"],
        client_id=row["client_id"],
        scopes=tuple(row["scopes"]),
        resource=row["resource"],
        grant_id=row["grant_id"],
        label=row["label"],
        expires_at=row["expires_at"],
    )
    return account, info


def account_for_token(conn: Conn, token: str) -> Account | None:
    """The account a valid bearer token belongs to, else None."""
    found = lookup_token(conn, token)
    return found[0] if found else None


# --- confirmation codes ------------------------------------------------------

#: A confirmation code holds for the window it was made in and the one after.
CONFIRMATION_WINDOW_SECONDS = 15 * 60
_CODE_RE = re.compile(r"[0-9a-f]{4}")


def confirmation_window(now: float | None = None) -> int:
    """The number of the current window of CONFIRMATION_WINDOW_SECONDS."""
    moment = datetime.now(UTC).timestamp() if now is None else now
    return int(moment // CONFIRMATION_WINDOW_SECONDS)


def confirmation_code(account_id: int, node_id: int, version: str, window: int) -> str:
    """The code that confirms deleting one note (graph.handlers.delete): four
    hex characters of HMAC-SHA256 under keystore.confirmation_key() over the
    account, the note, its version and the window. An edit of the note, another
    account or another window gives another code; nobody without the server's
    secret can compute one."""
    message = f"delete:{account_id}:{node_id}:{version}:{window}".encode()
    return hmac.new(keystore.confirmation_key(), message, hashlib.sha256).hexdigest()[
        :4
    ]


def confirmation_holds(
    account_id: int, node_id: int, version: str, code: str, now: float | None = None
) -> bool:
    """Is ``code`` the code of this window or of the one before?"""
    if not _CODE_RE.fullmatch(code):  # compare_digest takes ASCII only
        return False
    window = confirmation_window(now)
    return any(
        hmac.compare_digest(code, confirmation_code(account_id, node_id, version, w))
        for w in (window, window - 1)
    )
