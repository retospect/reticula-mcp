"""Low-level record signing and appending. FROZEN.

Everything that writes to the signed log goes through :func:`sign_and_append`
(local writes) or :func:`append_signed` (records produced elsewhere, later
federation). Neither rebuilds an envelope: the bytes that were signed are the
bytes that are stored and served.

Conventions:

* ``head`` of an address = its newest row by ``id``.
* ``feed`` is newest-first.
* Known record types are signed by the right key slot: ``identity`` by the
  ``root`` key, every other known type by the delegate (``k1``). Unknown
  types may use any non-revoked signing key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg.errors

from reticula_mcp import format as fmt
from reticula_mcp import keystore
from reticula_mcp.errors import BAD_INPUT, CONFLICT, FORBIDDEN, NOT_FOUND, ReticulaError

if TYPE_CHECKING:
    from reticula_mcp.accounts import Account
    from reticula_mcp.db.pool import Conn

ROOT_KEY = "root"
DELEGATE_KEY = "k1"

_COLUMNS = (
    "id, version_hash, address, type, author, key_id, created_at, parents, hub_id,"
    " audience, content_hash, format_version, envelope, signature, received_at,"
    " blanked_at"
)


@dataclass(frozen=True)
class Record:
    """One row of the signed log."""

    id: int
    version_hash: str
    address: str
    type: str
    author: str
    key_id: str
    created_at: datetime
    parents: tuple[str, ...]
    hub_id: str | None
    audience: str
    content_hash: str
    format_version: int
    envelope: bytes
    signature: bytes
    received_at: datetime
    blanked_at: datetime | None = None

    @property
    def blanked(self) -> bool:
        return self.blanked_at is not None

    @property
    def text_form(self) -> str:
        """``b64u(envelope).b64u(signature)``: the export form."""
        return fmt.encode_signed(self.envelope, self.signature)

    @property
    def fields(self) -> dict[str, Any]:
        """The parsed envelope (read-only view; never re-serialise it)."""
        return fmt.parse_envelope(self.envelope)

    @property
    def payload(self) -> Any:
        """The parsed payload JSON (``enc`` none)."""
        return fmt.payload_json(self.fields)


def _record(row: Mapping[str, Any]) -> Record:
    return Record(
        id=row["id"],
        version_hash=row["version_hash"],
        address=row["address"],
        type=row["type"],
        author=row["author"],
        key_id=row["key_id"],
        created_at=row["created_at"],
        parents=tuple(row["parents"]),
        hub_id=row["hub_id"],
        audience=row["audience"],
        content_hash=row["content_hash"],
        format_version=row["format_version"],
        envelope=bytes(row["envelope"]),
        signature=bytes(row["signature"]),
        received_at=row["received_at"],
        blanked_at=row["blanked_at"],
    )


def get_record(conn: Conn, ref: str | int) -> Record | None:
    """Fetch by version hash (``"sha256:..."``) or by numeric id."""
    if isinstance(ref, int):
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM record WHERE id = %s", (ref,)
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM record WHERE version_hash = %s", (ref,)
        ).fetchone()
    return _record(row) if row else None


def head(conn: Conn, address: str) -> Record | None:
    """Newest version of ``address`` (by log order), or None."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM record WHERE address = %s ORDER BY id DESC LIMIT 1",
        (address,),
    ).fetchone()
    return _record(row) if row else None


def versions(conn: Conn, address: str) -> list[Record]:
    """All versions of ``address``, oldest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM record WHERE address = %s ORDER BY id", (address,)
    ).fetchall()
    return [_record(r) for r in rows]


def feed(
    conn: Conn,
    author: str,
    after_id: int | None = None,
    limit: int = 100,
    *,
    since_id: int | None = None,
) -> list[Record]:
    """One author's records, NEWEST FIRST.

    ``after_id``: paging cursor, returns records strictly OLDER than this id
    (pass the last id of the previous page). ``since_id``: returns only
    records strictly newer than this id (a pull cursor). Both optional.
    """
    clauses = ["author = %s"]
    params: list[Any] = [author]
    if after_id is not None:
        clauses.append("id < %s")
        params.append(after_id)
    if since_id is not None:
        clauses.append("id > %s")
        params.append(since_id)
    params.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM record WHERE {' AND '.join(clauses)}"
        " ORDER BY id DESC LIMIT %s",
        params,
    ).fetchall()
    return [_record(r) for r in rows]


def _author_key(conn: Conn, person_id: str, key_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT k.public_key, k.role, k.revoked_at FROM account_key k"
        " JOIN account a ON a.id = k.account_id"
        " WHERE a.person_id = %s AND k.key_id = %s AND k.use = 'sign'",
        (person_id, key_id),
    ).fetchone()


def check_record(conn: Conn, rec: Record) -> list[str]:
    """Return every reason ``rec`` fails verification (empty list = valid).

    Checks the stored envelope bytes against: the version hash, the column
    copies (address, author, key, type, ...), the format rules, the content
    hash, and the Ed25519 signature under the author's stored public key.
    """
    if rec.blanked:
        return ["content was erased (tombstoned)"]
    problems: list[str] = []
    if fmt.version_hash(rec.envelope) != rec.version_hash:
        problems.append("version_hash does not match the envelope bytes")
    try:
        env = fmt.parse_envelope(rec.envelope)
    except ValueError as exc:
        return [*problems, str(exc)]
    problems += fmt.check_envelope(env)
    for column, name in (
        ("address", "address"),
        ("author", "author"),
        ("key_id", "key"),
        ("type", "type"),
        ("hub_id", "hub"),
        ("audience", "audience"),
        ("content_hash", "content_hash"),
    ):
        if getattr(rec, column) != env.get(name):
            problems.append(f"column {column} disagrees with the envelope")
    if list(rec.parents) != env.get("parents"):
        problems.append("column parents disagrees with the envelope")
    key = _author_key(conn, rec.author, rec.key_id)
    if key is None:
        problems.append("author key is not known to this instance")
    elif not fmt.verify(bytes(key["public_key"]), rec.envelope, rec.signature):
        problems.append("signature does not verify")
    return problems


def verify_record(conn: Conn, rec: Record) -> bool:
    """True iff the record's signature and hashes check out. See :func:`check_record`."""
    return not check_record(conn, rec)


def append_signed(conn: Conn, envelope: bytes, signature: bytes) -> Record:
    """Insert an already-signed record (the lowest-level write).

    Validates structure (``check_envelope``), that the author and key are
    known to this instance, the key's role for known types, and the signature.
    Unknown types/fields/audiences are accepted and stored byte for byte.
    ``BadInput`` on any failure; ``Conflict`` when the version already exists.
    """
    try:
        env = fmt.parse_envelope(envelope)
    except ValueError as exc:
        raise ReticulaError(BAD_INPUT, f"bad envelope: {exc}") from exc
    problems = fmt.check_envelope(env)
    if problems:
        raise ReticulaError(BAD_INPUT, "bad envelope: " + "; ".join(problems))
    key = _author_key(conn, env["author"], env["key"])
    if key is None:
        raise ReticulaError(BAD_INPUT, "unknown author or key slot")
    if key["revoked_at"] is not None:
        raise ReticulaError(FORBIDDEN, f"key {env['key']!r} is revoked")
    if env["type"] == "identity" and key["role"] != "root":
        raise ReticulaError(
            BAD_INPUT, "identity records must be signed by the root key"
        )
    if (
        env["type"] in fmt.KNOWN_TYPES
        and env["type"] != "identity"
        and key["role"] != "delegate"
    ):
        raise ReticulaError(
            BAD_INPUT, "this record type must be signed by the delegate key"
        )
    if not fmt.verify(bytes(key["public_key"]), envelope, signature):
        raise ReticulaError(BAD_INPUT, "signature does not verify")
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO record (version_hash, address, type, author, key_id,"
                " created_at, parents, hub_id, audience, content_hash, format_version,"
                " envelope, signature) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                f" RETURNING {_COLUMNS}",
                (
                    fmt.version_hash(envelope),
                    env["address"],
                    env["type"],
                    env["author"],
                    env["key"],
                    fmt.parse_created(env["created"]),
                    env["parents"],
                    env["hub"],
                    env["audience"],
                    env["content_hash"],
                    env["v"],
                    envelope,
                    signature,
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise ReticulaError(CONFLICT, "this exact version already exists") from exc
    assert row is not None
    return _record(row)


def sign_and_append(
    conn: Conn,
    account: Account,
    *,
    type: str,
    address: str,
    payload: Mapping[str, Any],
    parents: Sequence[str] = (),
    hub_id: str | None = None,
    audience: str = "hub",
    key: str | None = None,
    created: str | datetime | None = None,
) -> Record:
    """Build the envelope, sign it with the account's key, append it.

    ``key``: ``"root"`` or ``"k1"``; default by type (identity: root, others:
    k1). ``created``: RFC 3339 text, a datetime, or None for now.
    Local-write rules enforced here (not on :func:`append_signed`):

    * the address must belong to ``account`` (its person part);
    * ``parents == ()`` only for the FIRST version of an address;
    * every parent must be an existing version of the same address;
    * a tombstoned address takes no further versions.
    """
    try:
        addr = fmt.parse_address(address)
    except ValueError as exc:
        raise ReticulaError(BAD_INPUT, f"bad address: {exc}") from exc
    if addr.person_id != account.person_id:
        raise ReticulaError(FORBIDDEN, "you can only write under your own address")
    if hub_id is not None and not fmt.is_hub_id(hub_id):
        raise ReticulaError(BAD_INPUT, "hub_id must be an opaque hub ID or None")
    key_id = key or (ROOT_KEY if type == "identity" else DELEGATE_KEY)
    existing = versions(conn, address)
    if parents:
        known = {r.version_hash for r in existing}
        missing = [p for p in parents if p not in known]
        if missing:
            raise ReticulaError(
                CONFLICT, f"parent not found for {address}: {missing[0]}"
            )
    elif existing:
        raise ReticulaError(
            CONFLICT,
            f"{address} already has versions",
            hint="pass the previous version hash(es) as parents",
        )
    if any(r.type == "tombstone" for r in existing):
        raise ReticulaError(
            CONFLICT, f"{address} is tombstoned and takes no new versions"
        )
    secret = conn.execute(
        "SELECT private_key FROM account_key WHERE account_id = %s AND key_id = %s"
        " AND use = 'sign'",
        (account.id, key_id),
    ).fetchone()
    if secret is None or secret["private_key"] is None:
        raise ReticulaError(NOT_FOUND, f"no signing key {key_id!r} for this account")
    created_text = created if isinstance(created, str) else fmt.format_created(created)
    envelope = fmt.build_envelope(
        type=type,
        address=address,
        author=account.person_id,
        key=key_id,
        created=created_text,
        payload=payload,
        parents=parents,
        hub=hub_id,
        audience=audience,
    )
    private = keystore.decrypt(account.id, key_id, bytes(secret["private_key"]))
    signature = fmt.sign(private, envelope)
    return append_signed(conn, envelope, signature)
