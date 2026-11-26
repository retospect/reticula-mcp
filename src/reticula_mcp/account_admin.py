"""Deleting an account, and exporting it: what the account page offers
(auth/account_web.py). Reto's decisions for 0.5.0.

Delete erases everything at once:

- every live note, paper, hub and link the account wrote is deleted with a
  signed tombstone, as `delete` does (content erased in every version);
- every filing the account made of other people's notes is withdrawn;
- a last identity version says the account was deleted;
- every connection, token and session is revoked or ended;
- the e-mail address and the password hash are erased, the private keys too
  (the public keys stay, so old signatures can still be checked);
- the handle stays held: nobody else takes it.

Export is one zip with the account's own signed records (JSON Lines, with a
README on checking them) and a Markdown folder per hub.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any

from reticula_mcp import accounts, activity, records
from reticula_mcp import format as fmt
from reticula_mcp.accounts import Account
from reticula_mcp.auth import grants
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api

# --------------------------------------------------------------- deleting


def delete_everything(conn: Conn, account: Account) -> dict[str, int]:
    """Erase the account as described above, in the caller's transaction:
    every step or none. Returns what was done, for the page.

    The account row is locked FOR UPDATE and set disabled first: a write
    holds a share lock on it (accounts.lock_for_write), so one that started
    before waits to commit and its note is found below, and one after finds
    the account disabled. A step that fails raises, and the caller's
    transaction rolls everything back."""
    if account.status == "system":
        raise ReticulaError("Forbidden", "the instance's own account is not deleted")
    row = conn.execute(
        "SELECT email, status FROM account WHERE id = %s FOR UPDATE", (account.id,)
    ).fetchone()
    if row is None:
        raise ReticulaError("NotFound", "no such account")
    email = str(row["email"])
    conn.execute(
        "UPDATE account SET status = 'disabled', updated_at = now() WHERE id = %s",
        (account.id,),
    )
    counts = {"notes": 0, "hubs": 0, "filings": 0}
    # filings of other people's notes first, while the hubs still stand
    filings = conn.execute(
        "SELECT m.node_id, m.hub_node_id FROM membership m"
        " JOIN node n ON n.id = m.node_id JOIN node h ON h.id = m.hub_node_id"
        " WHERE m.author = %s AND NOT m.withdrawn AND n.author <> %s"
        " AND n.tombstoned_at IS NULL AND h.tombstoned_at IS NULL",
        (account.person_id, account.person_id),
    ).fetchall()
    for f in filings:
        api.remove_membership(conn, account, f["node_id"], f["hub_node_id"])
        counts["filings"] += 1
    # then every live atom: notes before hubs
    rows = conn.execute(
        "SELECT id, kind FROM node WHERE author = %s AND tombstoned_at IS NULL"
        " ORDER BY (kind = 'hub'), id DESC",
        (account.person_id,),
    ).fetchall()
    for r in rows:
        api.tombstone_atom(conn, account, r["id"])
        counts["hubs" if r["kind"] == "hub" else "notes"] += 1
    _publish_deleted_identity(conn, account)
    grants.revoke_everything(conn, account.id)
    _erase_activity(conn, account.id)
    _erase_residue(conn, account, email)
    conn.execute(
        "UPDATE account SET email = %s, password_hash = '!', verified = false,"
        " updated_at = now() WHERE id = %s",
        (f"deleted-{account.id}@deleted.invalid", account.id),
    )
    conn.execute(
        "UPDATE account_key SET private_key = NULL WHERE account_id = %s",
        (account.id,),
    )
    return counts


def _erase_residue(conn: Conn, account: Account, email: str) -> None:
    """What else names the account: its settings, the connections it was
    pointed to onboarding on, shares of other people's hubs with it and of
    its own hubs with anyone, shares that wait for it or from it and its
    blocks (migration 0304), unused sign-in codes, and the counters and
    sign-up attempts that hold its e-mail address."""
    for table in ("account_setting", "connection_setting", "onboarding_shown"):
        conn.execute(f"DELETE FROM {table} WHERE account_id = %s", (account.id,))
    conn.execute("DELETE FROM oauth_code WHERE account_id = %s", (account.id,))
    conn.execute(
        "DELETE FROM hub_grant WHERE grantee = %s OR hub_node_id IN"
        " (SELECT id FROM node WHERE author = %s AND kind = 'hub')",
        (account.person_id, account.person_id),
    )
    # shares that wait for it or from it, of its hubs, and its blocks either way
    conn.execute(
        "DELETE FROM share_offer WHERE grantee = %(p)s OR granted_by = %(p)s"
        " OR hub_node_id IN (SELECT id FROM node WHERE author = %(p)s"
        " AND kind = 'hub')",
        {"p": account.person_id},
    )
    conn.execute(
        "DELETE FROM share_block WHERE person = %(p)s OR blocked = %(p)s",
        {"p": account.person_id},
    )
    prefix = f"{email.lower()}|"
    conn.execute(
        "DELETE FROM auth_event WHERE lower(subject) = %s"
        " OR left(lower(subject), %s) = %s",
        (email.lower(), len(prefix), prefix),
    )
    conn.execute("DELETE FROM signup_attempt WHERE lower(email) = %s", (email.lower(),))


def _erase_activity(conn: Conn, account_id: int) -> None:
    """The account's rows of RETICULA3's activity log: the account row stays
    (the handle is held), so no cascade removes them."""
    activity.erase(conn, account_id)


def _publish_deleted_identity(conn: Conn, account: Account) -> None:
    previous = records.head(conn, account.identity_address)
    payload = accounts.identity_payload(conn, account)
    payload["deleted"] = True
    records.sign_and_append(
        conn,
        account,
        type="identity",
        address=account.identity_address,
        payload=payload,
        parents=(previous.version_hash,) if previous else (),
        audience="public",
        key=records.ROOT_KEY,
    )


# ---------------------------------------------------------------- export

_SLUG = re.compile(r"[^a-z0-9]+")

README = """Export of a Reticula account

signed/records.jsonl  every record this account signed, one JSON object per
                      line, oldest first: notes, hubs, links, filings,
                      shares, deletions and the identity record.
markdown/             the account's notes as Markdown, one folder per hub of
                      the account; notes in no hub of the account are in
                      markdown/notes/.

Checking a signature: each line has "envelope" and "signature", both
base64. The signature is Ed25519 over the envelope bytes exactly as given.
The public key is in the account's identity record (the line whose "type"
is "identity"; the newest of them): its envelope is JSON, and its "payload"
is JSON text whose "keys" list has each key's "id" and its "pub" in
base64url. The "key_id" of a record says which key signed it.
With Python and the cryptography package:

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(pub).verify(signature, envelope)

A deleted note's earlier versions are erased on the server and appear here
without their envelope.
"""


def _slug(text: str, address: str) -> str:
    base = _SLUG.sub("-", (text or "").lower()).strip("-")[:48] or "untitled"
    return f"{base}-{address[-8:]}"


def _markdown(row: dict[str, Any]) -> str:
    lines = [f"# {row['title'] or '(untitled)'}", ""]
    meta = [f"id: {row['address']}", f"kind: {row['kind']}"]
    if row["tags"]:
        meta.append("tags: " + ", ".join(row["tags"]))
    meta.append(f"written: {row['created_at']:%Y-%m-%d %H:%M} UTC")
    lines += [f"- {m}" for m in meta] + ["", row["text"] or "", ""]
    return "\n".join(lines)


#: The most an export may hold before compression; above it the page says so.
EXPORT_MAX_BYTES = 200 * 1024 * 1024
#: Exports held in memory up to this size, then in a temporary file.
SPOOL_BYTES = 16 * 1024 * 1024
TOO_LARGE = "TooLarge"

_running: set[int] = set()
_running_lock = threading.Lock()


@contextmanager
def export_slot(account_id: int) -> Iterator[None]:
    """One export per account at a time (in this process)."""
    with _running_lock:
        if account_id in _running:
            raise ReticulaError("Conflict", "an export of this account is running")
        _running.add(account_id)
    try:
        yield
    finally:
        with _running_lock:
            _running.discard(account_id)


@dataclass
class ExportRows:
    records: list[str]  # JSON Lines
    hubs: dict[int, str]  # hub id -> folder name
    notes: list[dict[str, Any]]


def export_rows(conn: Conn, account: Account) -> ExportRows:
    """Everything the export holds, read from the database; refused with
    TOO_LARGE above EXPORT_MAX_BYTES. The zip is built afterwards, without a
    connection (build_zip)."""
    size = 0

    def add(n: int) -> None:
        nonlocal size
        size += n
        if size > EXPORT_MAX_BYTES:
            raise ReticulaError(
                TOO_LARGE,
                f"the export would be larger than {EXPORT_MAX_BYTES // (1024 * 1024)} MB",
                hint="ask the operator of this server for a copy",
            )

    lines: list[str] = []
    for r in conn.execute(
        "SELECT address, type, version_hash, key_id, created_at, envelope,"
        " signature, blanked_at FROM record WHERE author = %s ORDER BY id",
        (account.person_id,),
    ):
        line: dict[str, Any] = {
            "address": r["address"],
            "type": r["type"],
            "version": r["version_hash"],
            "key_id": r["key_id"],
            "created": fmt.format_created(r["created_at"]),
        }
        if r["blanked_at"] is None:
            line["envelope"] = base64.b64encode(bytes(r["envelope"])).decode()
            line["signature"] = base64.b64encode(bytes(r["signature"])).decode()
        else:
            line["erased"] = True
        text = json.dumps(line, ensure_ascii=False) + "\n"
        add(len(text))
        lines.append(text)
    hubs = {
        r["id"]: _slug(r["title"] or "hub", r["address"])
        for r in conn.execute(
            "SELECT id, title, address FROM node WHERE author = %s"
            " AND kind = 'hub' AND tombstoned_at IS NULL",
            (account.person_id,),
        )
    }
    notes = []
    for n in conn.execute(
        "SELECT n.id, n.address, n.kind, n.title, n.text, n.tags, n.created_at,"
        " ARRAY(SELECT m.hub_node_id FROM membership m WHERE m.node_id = n.id"
        "   AND NOT m.withdrawn AND m.author = n.author) AS hubs"
        " FROM node n WHERE n.author = %s AND n.kind <> 'hub'"
        " AND n.tombstoned_at IS NULL ORDER BY n.id",
        (account.person_id,),
    ):
        add(len(n["text"] or "") * max(1, len(n["hubs"])))
        notes.append(dict(n))
    return ExportRows(lines, hubs, notes)


@contextmanager
def build_zip(account: Account, rows: ExportRows) -> Iterator[IO[bytes]]:
    """The zip described above, from ``rows``, in a spooled temporary file
    (memory up to SPOOL_BYTES, then disk), positioned at its start. Takes no
    database connection."""
    spooled = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)  # noqa: SIM115
    try:
        with zipfile.ZipFile(spooled, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", README)
            zf.writestr("signed/records.jsonl", "".join(rows.records))
            for n in rows.notes:
                name = _slug(n["title"] or (n["text"] or "")[:40], n["address"]) + ".md"
                text = _markdown(n)
                folders = [rows.hubs[h] for h in n["hubs"] if h in rows.hubs] or [
                    "notes"
                ]
                for folder in folders:
                    zf.writestr(f"markdown/{folder}/{name}", text)
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            zf.writestr(
                "markdown/README.md",
                f"# Notes of @{account.handle}\n\nExported {stamp}.\n",
            )
        spooled.seek(0)
        yield spooled
    finally:
        spooled.close()
