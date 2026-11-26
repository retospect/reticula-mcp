"""The directory of people: ``search(kind="person")``.

An account is listed when its owner said ``edit(ref="settings",
text="listed: yes")``; a new one is not (0.5.0; migration 0358 kept every older
account listed). The setting lives in :mod:`reticula_mcp.settings`. A line names the handle, whether the account is
verified and the day it joined. It never shows an e-mail address, a person ID
or anything else about the account. Someone who is not listed can still be
shared with by their exact handle.
"""

from __future__ import annotations

from reticula_mcp import settings
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.search.render import quote

#: Accounts that can be listed: never ``system``, never ``disabled``.
LISTABLE = ("active", "provisional")

NEXT = (
    'next: share(hub=<hub>, person="@handle", mode="add" or "read")'
    " shares a hub with one of them"
)


def _like_pattern(part: str) -> str:
    """``part`` as the middle of a LIKE pattern: ``%``, ``_`` and the escape
    character itself match literally."""
    escaped = part.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_people(conn: Conn, viewer: Account, query: str, limit: int) -> str:
    """The listed accounts whose handle contains ``query``, by handle."""
    part = query.strip().lstrip("@").strip()
    params = {
        "statuses": list(LISTABLE),
        "pattern": _like_pattern(part),
        "limit": limit,
    }
    where = (
        f"NOT ({settings.unlisted_sql('account.id')})"
        " AND status = ANY(%(statuses)s) AND handle::text ILIKE %(pattern)s"
        " ESCAPE '\\'"
    )
    total = conn.execute(
        f"SELECT count(*) AS n FROM account WHERE {where}", params
    ).fetchone()
    count = int(total["n"]) if total else 0
    if not count:
        shown = quote(part, 40)
        return (
            f"no listed person matches {shown}. Someone who is not listed can"
            " still be shared with by their exact handle."
        )
    rows = conn.execute(
        "SELECT id, handle::text AS handle, verified, created_at FROM account"
        f" WHERE {where} ORDER BY handle::text LIMIT %(limit)s",
        params,
    ).fetchall()
    out = [
        f"people matching {quote(part, 40)}: {count}"
        if part
        else f"people on this instance: {count} listed"
    ]
    for row in rows:
        you = " (you)" if row["id"] == viewer.id else ""
        out.append(
            f"- @{row['handle']}{you} · verified: {'yes' if row['verified'] else 'no'}"
            f" · joined {row['created_at']:%Y-%m-%d}"
        )
    if count > len(rows):
        out.append(
            f"{count - len(rows)} more; a longer part of the handle narrows the list"
        )
    out.append(NEXT)
    return "\n".join(out)
