"""Test helpers: write atoms and materialise `node` rows the way a lane would."""

from __future__ import annotations

from typing import Any

from reticula_mcp import format as fmt
from reticula_mcp import records
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn


def make_atom(
    conn: Conn,
    account: Account,
    text: str,
    *,
    kind: str = "note",
    title: str | None = None,
    audience: str = "hub",
    hub_id: str | None = None,
) -> dict[str, Any]:
    """Sign an atom record and insert its `node` row; return the node row."""
    payload: dict[str, Any] = {"kind": kind, "text": text}
    if title:
        payload["title"] = title
    address = fmt.new_address(account.person_id)
    rec = records.sign_and_append(
        conn,
        account,
        type="atom",
        address=address,
        payload=payload,
        hub_id=hub_id,
        audience=audience,
    )
    row = conn.execute(
        "INSERT INTO node (address, author, author_account_id, kind, head_record_id,"
        " head_version, title, text, audience, payload) VALUES"
        " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
        (
            address,
            account.person_id,
            account.id,
            kind,
            rec.id,
            rec.version_hash,
            title,
            text,
            audience,
            fmt.canonical_text(payload),
        ),
    ).fetchone()
    assert row is not None
    return row
