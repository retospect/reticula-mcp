"""Database-backed sliding-window counters (work across workers and restarts)."""

from __future__ import annotations

from datetime import timedelta

from reticula_mcp.db.pool import Conn

SIGNIN_IP = "signin_ip"
SIGNIN_EMAIL = "signin_email"
REGISTER_IP = "register_ip"
WRITE_ACCOUNT = "write_account"  # one event per write tool call, per account


def _seconds(window: timedelta | int) -> float:
    return window.total_seconds() if isinstance(window, timedelta) else float(window)


def count(conn: Conn, kind: str, subject: str, window: timedelta | int) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM auth_event WHERE kind = %s AND subject = %s"
        " AND created_at > now() - make_interval(secs => %s)",
        (kind, subject, _seconds(window)),
    ).fetchone()
    return int(row["n"]) if row else 0


def email_failures(conn: Conn, email_key: str, window: timedelta | int) -> int:
    """Failed sign-ins for one e-mail address from every address bucket: the
    SIGNIN_EMAIL counters are kept per (e-mail, bucket) as ``<e-mail>|<bucket>``."""
    prefix = f"{email_key}|"
    row = conn.execute(
        "SELECT count(*) AS n FROM auth_event WHERE kind = %s"
        " AND left(subject, %s) = %s"
        " AND created_at > now() - make_interval(secs => %s)",
        (SIGNIN_EMAIL, len(prefix), prefix, _seconds(window)),
    ).fetchone()
    return int(row["n"]) if row else 0


def record(conn: Conn, kind: str, subject: str) -> None:
    conn.execute(
        "INSERT INTO auth_event (kind, subject) VALUES (%s, %s)", (kind, subject)
    )


def purge(conn: Conn, older_than: timedelta = timedelta(days=2)) -> None:
    """Drop old counters (and old sign-up attempts); called opportunistically."""
    secs = older_than.total_seconds()
    conn.execute(
        "DELETE FROM auth_event WHERE created_at < now() - make_interval(secs => %s)",
        (secs,),
    )
    conn.execute(
        "DELETE FROM signup_attempt"
        " WHERE created_at < now() - make_interval(secs => %s)",
        (secs,),
    )


def locked(conn: Conn, kind: str, subject: str, limit: int, window: timedelta) -> bool:
    return count(conn, kind, subject, window) >= limit


def acquire(
    conn: Conn, kind: str, subject: str, limit: int, window: timedelta | int
) -> bool:
    """Count one event unless the window is already full (atomic per subject).

    True when the event was allowed (and recorded).
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 7310))",
        (f"{kind}:{subject}",),
    )
    if count(conn, kind, subject, window) >= limit:
        return False
    record(conn, kind, subject)
    return True


def signup_allowed(
    conn: Conn, ip: str, email: str | None, limit: int, window: int
) -> bool:
    """Record a sign-up attempt for ``ip`` unless the per-address rate is used up.

    Every attempt counts, successful or not, so the page cannot be used to
    probe e-mail addresses or burn CPU on argon2 faster than the rate.
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 7311))", (f"signup:{ip}",)
    )
    row = conn.execute(
        "SELECT count(*) AS n FROM signup_attempt WHERE ip = %s"
        " AND created_at > now() - make_interval(secs => %s)",
        (ip, float(window)),
    ).fetchone()
    if row and int(row["n"]) >= limit:
        return False
    conn.execute(
        "INSERT INTO signup_attempt (ip, email) VALUES (%s, %s)",
        (ip, (email or "")[:254] or None),
    )
    return True


def acquire_many(
    conn: Conn, checks: list[tuple[str, str, int, timedelta]]
) -> list[int] | None:
    """Count one event on each ``(kind, subject, limit, window)`` or none at all.

    Locks every key first (in sorted order, so two callers cannot deadlock),
    so parallel callers see each other's events. Returns the ids of the rows
    inserted (so a caller can take them back), or None when any key is full.
    """
    for kind, subject, _, _ in sorted(checks, key=lambda c: (c[0], c[1])):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 7310))",
            (f"{kind}:{subject}",),
        )
    if any(count(conn, k, s, w) >= limit for k, s, limit, w in checks):
        return None
    ids = []
    for kind, subject, _, _ in checks:
        row = conn.execute(
            "INSERT INTO auth_event (kind, subject) VALUES (%s, %s) RETURNING id",
            (kind, subject),
        ).fetchone()
        assert row is not None
        ids.append(int(row["id"]))
    return ids


def trim(conn: Conn, kind: str, subject: str, window: timedelta | int) -> None:
    """Drop this key's events that have left the window (they no longer count)."""
    conn.execute(
        "DELETE FROM auth_event WHERE kind = %s AND subject = %s"
        " AND created_at <= now() - make_interval(secs => %s)",
        (kind, subject, _seconds(window)),
    )


def seconds_until_free(
    conn: Conn, kind: str, subject: str, limit: int, window: timedelta | int
) -> int | None:
    """Whole seconds until one more event would be allowed (0 when it is now).

    None when it never would be (a limit of zero).
    """
    if limit <= 0:
        return None
    used = count(conn, kind, subject, window)
    if used < limit:
        return 0
    row = conn.execute(
        "SELECT ceil(extract(epoch FROM created_at + make_interval(secs => %s)"
        " - now())) AS wait FROM auth_event WHERE kind = %s AND subject = %s"
        " AND created_at > now() - make_interval(secs => %s)"
        " ORDER BY created_at OFFSET %s LIMIT 1",
        (_seconds(window), kind, subject, _seconds(window), used - limit),
    ).fetchone()
    return max(int(row["wait"]), 1) if row else 0


def forget(conn: Conn, ids: list[int]) -> None:
    conn.execute("DELETE FROM auth_event WHERE id = ANY(%s)", (ids,))
