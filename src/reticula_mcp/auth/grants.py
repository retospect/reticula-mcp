"""Grants: one row per connected assistant, and the tokens issued under it.

A grant is born when an authorization code is exchanged and dies when it is
revoked (by the person, through ``/revoke``, by the ``connections`` CLI, or
because a consumed refresh token or a used code was replayed). Revoking a grant
revokes every token under it, so the foundation's ``accounts.lookup_token``
(which only looks at the token row) needs no knowledge of grants.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.shared.auth import OAuthToken

from reticula_mcp.accounts import TOKEN_PREFIX, hash_token
from reticula_mcp.auth import config
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import NOT_FOUND, ReticulaError

REFRESH_PREFIX = "rtr_"
CODE_PREFIX = "rtc_"


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def create_grant(
    conn: Conn,
    *,
    account_id: int,
    client_id: str,
    label: str,
    scopes: list[str],
    resource: str | None,
    grant_id: str | None = None,
) -> str:
    key = grant_id or secrets.token_hex(16)
    conn.execute(
        "INSERT INTO oauth_grant (grant_id, account_id, client_id, label, scopes,"
        " resource) VALUES (%s, %s, %s, %s, %s, %s)",
        (key, account_id, client_id, label, scopes, resource),
    )
    return key


def issue_pair(
    conn: Conn,
    *,
    account_id: int,
    client_id: str,
    grant_id: str,
    label: str,
    scopes: list[str],
    resource: str | None,
) -> OAuthToken:
    """A fresh access + refresh token under ``grant_id`` (digests stored)."""
    now = datetime.now(UTC)
    access = new_secret(TOKEN_PREFIX)
    refresh = new_secret(REFRESH_PREFIX)
    for kind, raw, expires in (
        ("access", access, now + config.ACCESS_TTL),
        ("refresh", refresh, now + config.REFRESH_TTL),
    ):
        conn.execute(
            "INSERT INTO oauth_token (token_hash, kind, client_id, account_id,"
            " scopes, resource, grant_id, label, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                hash_token(raw),
                kind,
                client_id,
                account_id,
                scopes,
                resource,
                grant_id,
                label,
                expires,
            ),
        )
    return OAuthToken(
        access_token=access,
        refresh_token=refresh,
        expires_in=int(config.ACCESS_TTL.total_seconds()),
        scope=" ".join(scopes),
    )


def revoke_grant_key(conn: Conn, grant_id: str) -> bool:
    """Revoke a grant and all its tokens. True when it was live."""
    row = conn.execute(
        "UPDATE oauth_grant SET revoked_at = now()"
        " WHERE grant_id = %s AND revoked_at IS NULL RETURNING id",
        (grant_id,),
    ).fetchone()
    conn.execute(
        "UPDATE oauth_token SET revoked_at = now()"
        " WHERE grant_id = %s AND revoked_at IS NULL",
        (grant_id,),
    )
    return row is not None


def revoke_everything(conn: Conn, account_id: int) -> tuple[int, int]:
    """Revoke every grant and every token of the account: connections and
    operator tokens alike. Returns (grants, tokens) that were live."""
    grants_n = conn.execute(
        "UPDATE oauth_grant SET revoked_at = now()"
        " WHERE account_id = %s AND revoked_at IS NULL",
        (account_id,),
    ).rowcount
    tokens_n = conn.execute(
        "UPDATE oauth_token SET revoked_at = now()"
        " WHERE account_id = %s AND revoked_at IS NULL",
        (account_id,),
    ).rowcount
    return grants_n, tokens_n


@dataclass(frozen=True)
class Connection:
    id: int
    grant_id: str
    client_id: str
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def live(self) -> bool:
        return self.revoked_at is None


def list_connections(conn: Conn, account_id: int) -> list[Connection]:
    rows = conn.execute(
        "SELECT g.id, g.grant_id, g.client_id, g.label, g.created_at, g.revoked_at,"
        " (SELECT max(t.last_used_at) FROM oauth_token t"
        "   WHERE t.grant_id = g.grant_id) AS last_used_at"
        " FROM oauth_grant g WHERE g.account_id = %s ORDER BY g.id",
        (account_id,),
    ).fetchall()
    return [Connection(**_conn_row(r)) for r in rows]


def _conn_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        k: r[k]
        for k in (
            "id",
            "grant_id",
            "client_id",
            "label",
            "created_at",
            "last_used_at",
            "revoked_at",
        )
    }


def revoke_connection(conn: Conn, account_id: int, connection_id: int) -> Connection:
    """Revoke the account's connection ``connection_id`` (its grant and tokens)."""
    row = conn.execute(
        "SELECT g.id, g.grant_id, g.client_id, g.label, g.created_at, g.revoked_at,"
        " NULL::timestamptz AS last_used_at FROM oauth_grant g"
        " WHERE g.id = %s AND g.account_id = %s",
        (connection_id, account_id),
    ).fetchone()
    if row is None:
        raise ReticulaError(
            NOT_FOUND,
            f"no connection {connection_id} on that account",
            hint="`reticula-mcp connections list --email ...` shows the ids",
        )
    revoke_grant_key(conn, row["grant_id"])
    return Connection(**_conn_row(row))


@dataclass(frozen=True)
class OperatorToken:
    id: int
    label: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


def list_operator_tokens(conn: Conn, account_id: int) -> list[OperatorToken]:
    """Tokens minted with ``reticula-mcp token``: access tokens with no client/grant."""
    rows = conn.execute(
        "SELECT id, label, created_at, last_used_at, expires_at, revoked_at"
        " FROM oauth_token WHERE account_id = %s AND kind = 'access'"
        " AND client_id IS NULL AND grant_id IS NULL ORDER BY id",
        (account_id,),
    ).fetchall()
    return [OperatorToken(**r) for r in rows]


def revoke_operator_token(conn: Conn, account_id: int, token_id: int) -> OperatorToken:
    row = conn.execute(
        "SELECT id, label, created_at, last_used_at, expires_at, revoked_at"
        " FROM oauth_token WHERE id = %s AND account_id = %s AND kind = 'access'"
        " AND client_id IS NULL AND grant_id IS NULL",
        (token_id, account_id),
    ).fetchone()
    if row is None:
        raise ReticulaError(
            NOT_FOUND,
            f"no operator token {token_id} on that account",
            hint="`reticula-mcp connections list --email ...` shows the ids",
        )
    conn.execute(
        "UPDATE oauth_token SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
        (token_id,),
    )
    return OperatorToken(**row)


def purge_stale(conn: Conn) -> None:
    """Housekeeping: drop expired parked requests, spent codes and tokens, unused clients.

    Consumed refresh tokens are kept until they would have expired anyway (a
    replay of one must still be recognised and kill its grant); only then, plus
    three days, do they go. Operator tokens never expire and are never purged.
    """
    grace = timedelta(days=3)
    conn.execute("DELETE FROM oauth_pending WHERE expires_at < now()")
    conn.execute("DELETE FROM oauth_code WHERE expires_at < now() - %s", (grace,))
    conn.execute(
        "DELETE FROM oauth_token WHERE expires_at IS NOT NULL"
        " AND expires_at < now() - %s AND client_id IS NOT NULL",
        (grace,),
    )
    conn.execute(
        "DELETE FROM oauth_client c WHERE c.created_at < now() - %s"
        " AND NOT EXISTS (SELECT 1 FROM oauth_token t WHERE t.client_id = c.client_id)"
        " AND NOT EXISTS (SELECT 1 FROM oauth_grant g WHERE g.client_id = c.client_id)"
        " AND NOT EXISTS (SELECT 1 FROM oauth_code k WHERE k.client_id = c.client_id)"
        " AND NOT EXISTS (SELECT 1 FROM oauth_pending p WHERE p.client_id = c.client_id)",
        (timedelta(days=30),),
    )
