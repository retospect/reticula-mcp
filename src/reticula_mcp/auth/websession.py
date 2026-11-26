"""Sessions of the account pages: a random token in a cookie, its digest in
web_session (migration 0356).

- Without "keep me signed in": 30 minutes idle, 12 hours at most.
- With it: 30 days for both.
- Export, revoke and delete need the password again within REAUTH_WINDOW,
  however old the session is.
- The CSRF token of a session's forms is derived from the session token
  (HMAC-SHA256 with a fixed label): it changes with the session, needs no
  storage, and cannot be computed without the cookie.
- A new session is made at every sign-in (no fixation), and grants.
  revoke_everything ends them all.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from reticula_mcp.db.pool import Conn

IDLE = timedelta(minutes=30)
MAXIMUM = timedelta(hours=12)
KEEP = timedelta(days=30)
REAUTH_WINDOW = timedelta(minutes=10)
#: Sessions one account may have; the oldest end first.
MAX_PER_ACCOUNT = 20
TOKEN_BYTES = 32
_CSRF_LABEL = b"reticula account form"


@dataclass(frozen=True)
class Session:
    id_hash: str
    account_id: int
    created_at: datetime
    expires_at: datetime
    keep: bool
    reauth_at: datetime | None
    fresh_reauth: bool  # the password was given within REAUTH_WINDOW


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    """The form token of the session whose cookie holds ``token``."""
    return hmac.new(token.encode(), _CSRF_LABEL, hashlib.sha256).hexdigest()


def csrf_ok(token: str, form_value: str) -> bool:
    # as bytes: compare_digest refuses a str with non-ASCII characters
    return bool(form_value) and hmac.compare_digest(
        csrf_for(token).encode(), form_value.encode("utf-8", "replace")
    )


def create(
    conn: Conn, account_id: int, *, keep: bool, agent: str = "", bucket: str = ""
) -> str:
    """A new session; returns its token (only its digest is stored). The
    password was just given, so it also counts as a fresh re-authentication."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    lifetime = KEEP if keep else MAXIMUM
    idle = KEEP if keep else IDLE
    conn.execute(
        "INSERT INTO web_session (id_hash, account_id, created_at, expires_at,"
        " idle_seconds, reauth_at, keep, agent, bucket)"
        " VALUES (%s, %s, clock_timestamp(), now() + %s, %s, now(), %s, %s, %s)",
        (
            digest(token),
            account_id,
            lifetime,
            int(idle.total_seconds()),
            keep,
            agent[:120],
            bucket[:64],
        ),
    )
    # at most MAX_PER_ACCOUNT: the oldest beyond that end
    conn.execute(
        "DELETE FROM web_session WHERE account_id = %s AND id_hash IN ("
        " SELECT id_hash FROM web_session WHERE account_id = %s"
        " ORDER BY created_at DESC, id_hash OFFSET %s)",
        (account_id, account_id, MAX_PER_ACCOUNT),
    )
    return token


def load(conn: Conn, token: str | None) -> Session | None:
    """The live session of ``token``, its last use moved to now; None when
    unknown, past its end, or idle too long (then it is deleted)."""
    if not token or len(token) > 100:
        return None
    row = conn.execute(
        "SELECT id_hash, account_id, created_at, expires_at, keep, reauth_at,"
        " (expires_at > now()"
        "  AND last_seen_at + make_interval(secs => idle_seconds) > now()) AS live,"
        " (reauth_at IS NOT NULL AND reauth_at > now() - %s) AS fresh"
        " FROM web_session WHERE id_hash = %s",
        (REAUTH_WINDOW, digest(token)),
    ).fetchone()
    if row is None:
        return None
    if not row["live"]:
        conn.execute("DELETE FROM web_session WHERE id_hash = %s", (row["id_hash"],))
        return None
    conn.execute(
        "UPDATE web_session SET last_seen_at = now() WHERE id_hash = %s",
        (row["id_hash"],),
    )
    return Session(
        row["id_hash"],
        row["account_id"],
        row["created_at"],
        row["expires_at"],
        row["keep"],
        row["reauth_at"],
        bool(row["fresh"]),
    )


def reauthenticated(conn: Conn, session: Session) -> None:
    conn.execute(
        "UPDATE web_session SET reauth_at = now() WHERE id_hash = %s",
        (session.id_hash,),
    )


def end(conn: Conn, session: Session) -> None:
    conn.execute("DELETE FROM web_session WHERE id_hash = %s", (session.id_hash,))


def end_all(conn: Conn, account_id: int) -> int:
    """Sign out everywhere: every session of the account."""
    return conn.execute(
        "DELETE FROM web_session WHERE account_id = %s", (account_id,)
    ).rowcount


def purge(conn: Conn) -> None:
    """Housekeeping: sessions past their end or idle too long."""
    conn.execute(
        "DELETE FROM web_session WHERE expires_at <= now()"
        " OR last_seen_at + make_interval(secs => idle_seconds) <= now()"
    )


def count(conn: Conn, account_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM web_session WHERE account_id = %s"
        " AND expires_at > now()"
        " AND last_seen_at + make_interval(secs => idle_seconds) > now()",
        (account_id,),
    ).fetchone()
    return int(row["n"]) if row else 0
