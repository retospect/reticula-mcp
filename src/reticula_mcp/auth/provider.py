"""The OAuth authorization server's state machine (SDK provider protocol).

Protocol parsing, PKCE verification and redirect-URI matching are done by the
SDK handlers (``/authorize``, ``/token``); this class owns what must be
transactional: parked authorization requests, single-use codes, grants,
refresh rotation with replay detection, and bearer-token lookup.

Everything secret is stored as a sha256 digest. Race-sensitive steps are single
SQL statements (``UPDATE ... WHERE used_at IS NULL RETURNING``) or row locks.
Replay handling revokes a grant and must COMMIT before the error is raised, so
those paths do their work, leave the ``with`` block, then raise.
"""

from __future__ import annotations

import contextvars
import re
import secrets
from typing import Any

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.types.json import Jsonb
from pydantic import AnyUrl

from reticula_mcp import accounts, connection
from reticula_mcp.auth import config, grants
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn, Database

DEFAULT_CLIENT_ID = "reticula-token"  # operator tokens (CLI, tests) have no client

_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SHAPE = re.compile(r"[A-Za-z0-9_-]{20,200}\Z")


#: The throttle bucket of the browser that called /authorize, set by
#: ``auth/authorize.py`` around the SDK handler (which does not pass the request
#: on to the provider).
caller_bucket_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "reticula_caller_bucket", default=None
)


def sha256_hex(text: str) -> str:
    return accounts.hash_token(text)


def secret_shape(raw: str, prefix: str = "") -> bool:
    """Cheap sanity check before a database lookup."""
    return raw.startswith(prefix) and bool(_SHAPE.fullmatch(raw[len(prefix) :]))


def same_resource(a: str | None, b: str | None) -> bool:
    return (a or "").rstrip("/") == (b or "").rstrip("/")


class PendingInvalid(Exception):
    """The parked request cannot be completed (used, or its redirect is not registered).

    A plain exception on purpose: the SDK's AuthorizeError is a frozen dataclass,
    which cannot travel through a ``with`` block (contextlib sets __traceback__).
    """


class StoredCode(AuthorizationCode):
    grant_id: str
    account_id: int


class StoredRefresh(RefreshToken):
    grant_id: str
    account_id: int


def _invalid_grant(
    what: str = "credential is invalid, expired or revoked",
) -> TokenError:
    return TokenError("invalid_grant", what)


class ReticulaProvider(
    OAuthAuthorizationServerProvider[StoredCode, StoredRefresh, AccessToken]
):
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    @property
    def resource(self) -> str:
        return self.settings.resource_url

    # --- clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await anyio.to_thread.run_sync(self.client_sync, client_id)

    def client_sync(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id or len(client_id) > 128:
            return None
        with self.db.connection() as conn:
            return self.load_client(conn, client_id)

    @staticmethod
    def load_client(conn: Conn, client_id: str) -> OAuthClientInformationFull | None:
        row = conn.execute(
            "SELECT client_info FROM oauth_client WHERE client_id = %s", (client_id,)
        ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate(row["client_info"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Registration is served by auth/registration.py (it validates, caps and
        # throttles before calling store_client); nothing else may register.
        raise NotImplementedError("clients register through /register only")

    @staticmethod
    def store_client(conn: Conn, info: OAuthClientInformationFull) -> None:
        conn.execute(
            "INSERT INTO oauth_client (client_id, client_info) VALUES (%s, %s)",
            (info.client_id, Jsonb(info.model_dump(mode="json", exclude_none=True))),
        )

    # --- authorize: park the request, send the browser to the one page ----------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if client.client_id is None or not any(
            str(params.redirect_uri) == str(u) for u in client.redirect_uris or ()
        ):
            raise AuthorizeError("invalid_request", "unregistered redirect URI")
        if params.resource is not None and not same_resource(
            params.resource, self.resource
        ):
            raise AuthorizeError(
                "invalid_request", "resource does not match this MCP endpoint"
            )
        if not _CHALLENGE.fullmatch(params.code_challenge):
            raise AuthorizeError(
                "invalid_request", "an S256 code_challenge is required"
            )
        if params.state is not None and len(params.state) > config.MAX_STATE_LENGTH:
            raise AuthorizeError("invalid_request", "state is too long")
        handle = await anyio.to_thread.run_sync(
            self._park, client.client_id, params, caller_bucket_var.get()
        )
        if handle is None:
            raise AuthorizeError(
                "temporarily_unavailable", "too many pending sign-ins; retry later"
            )
        return f"{self.settings.base_url}/signin?req={handle}"

    def _park(
        self, client_id: str, params: AuthorizationParams, bucket: str | None
    ) -> str | None:
        """The page handle, or None when the client or the address has too many
        parked requests (the address cap stops one caller from using up the
        client's quota, which every user of a public client shares)."""
        handle = secrets.token_urlsafe(32)
        with self.db.connection() as conn:
            grants.purge_stale(conn)
            row = conn.execute(
                "SELECT count(*) AS n FROM oauth_pending WHERE client_id = %s",
                (client_id,),
            ).fetchone()
            if row and row["n"] >= config.MAX_PENDING_PER_CLIENT:
                return None
            if bucket is not None:
                row = conn.execute(
                    "SELECT count(*) AS n FROM oauth_pending WHERE ip = %s", (bucket,)
                ).fetchone()
                if row and row["n"] >= config.MAX_PENDING_PER_ADDRESS:
                    return None
            conn.execute(
                "INSERT INTO oauth_pending (id, client_id, params, expires_at, ip)"
                " VALUES (%s, %s, %s, now() + %s, %s)",
                (
                    sha256_hex(handle),
                    client_id,
                    Jsonb(
                        {
                            "redirect_uri": str(params.redirect_uri),
                            "explicit": params.redirect_uri_provided_explicitly,
                            "state": params.state,
                            "code_challenge": params.code_challenge,
                            "resource": self.resource,
                        }
                    ),
                    config.PENDING_TTL,
                    bucket,
                ),
            )
        return handle

    # --- the sign-in page's side of a parked request ---------------------------

    @staticmethod
    def find_pending(
        conn: Conn, handle: str, *, lock: bool = False
    ) -> dict[str, Any] | None:
        """The live parked request for ``handle`` with its client, or None."""
        if not secret_shape(handle):
            return None
        row = conn.execute(
            "SELECT p.id, p.client_id, p.params, p.csrf_hash, c.client_info"
            " FROM oauth_pending p JOIN oauth_client c ON c.client_id = p.client_id"
            " WHERE p.id = %s AND p.expires_at > now()"
            + (" FOR UPDATE OF p" if lock else ""),
            (sha256_hex(handle),),
        ).fetchone()
        return row

    @staticmethod
    def set_csrf(conn: Conn, handle: str, nonce: str) -> bool:
        """Store the token's digest on the parked request, once: False when
        one is there already (it is never replaced)."""
        row = conn.execute(
            "UPDATE oauth_pending SET csrf_hash = %s WHERE id = %s"
            " AND csrf_hash IS NULL RETURNING id",
            (sha256_hex(nonce), sha256_hex(handle)),
        ).fetchone()
        return row is not None

    def finish_authorization(
        self, conn: Conn, handle: str, pending: dict[str, Any], account_id: int
    ) -> str:
        """Consume the parked request and return the redirect URL carrying a code.

        The target is the redirect URI the request was parked with, and it is
        checked again here against what the client registered: the browser is
        never sent anywhere else.
        """
        client = OAuthClientInformationFull.model_validate(pending["client_info"])
        params = pending["params"]
        target = str(AnyUrl(params["redirect_uri"]))
        if not any(target == str(u) for u in client.redirect_uris or ()):
            raise PendingInvalid("unregistered redirect URI")
        claimed = conn.execute(
            "DELETE FROM oauth_pending WHERE id = %s RETURNING id",
            (sha256_hex(handle),),
        ).fetchone()
        if claimed is None:
            raise PendingInvalid("this sign-in was already used")
        code = grants.new_secret(grants.CODE_PREFIX)
        conn.execute(
            "INSERT INTO oauth_code (code_hash, client_id, account_id, code_challenge,"
            " redirect_uri, redirect_uri_provided_explicitly, scopes, resource,"
            " expires_at, grant_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,"
            " now() + %s, %s)",
            (
                sha256_hex(code),
                pending["client_id"],
                account_id,
                params["code_challenge"],
                target,
                bool(params.get("explicit", True)),
                [config.SCOPE],
                params.get("resource") or self.resource,
                config.CODE_TTL,
                secrets.token_hex(16),
            ),
        )
        return construct_redirect_uri(target, code=code, state=params.get("state"))

    # --- code exchange ---------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> StoredCode | None:
        return await anyio.to_thread.run_sync(
            self._load_code, client.client_id or "", authorization_code
        )

    def _load_code(self, client_id: str, raw: str) -> StoredCode | None:
        if not secret_shape(raw, grants.CODE_PREFIX):
            return None
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT account_id, grant_id, code_challenge, redirect_uri,"
                " redirect_uri_provided_explicitly, scopes, resource, used_at,"
                " extract(epoch FROM expires_at) AS exp, expires_at > now() AS live"
                " FROM oauth_code WHERE code_hash = %s AND client_id = %s",
                (sha256_hex(raw), client_id),
            ).fetchone()
            if row is None:
                return None
            if row["used_at"] is not None:
                # A code is single use; presenting it again means it leaked.
                # Revoke whatever the first exchange issued (committed on exit).
                if row["grant_id"]:
                    grants.revoke_grant_key(conn, row["grant_id"])
                return None
            if not row["live"]:
                return None
        return StoredCode(
            code=raw,
            scopes=list(row["scopes"]),
            expires_at=float(row["exp"]),
            client_id=client_id,
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
            subject=str(row["account_id"]),
            grant_id=row["grant_id"] or "",
            account_id=row["account_id"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: StoredCode
    ) -> OAuthToken:
        token = await anyio.to_thread.run_sync(
            self._exchange_code, client, authorization_code
        )
        if token is None:
            raise _invalid_grant()
        return token

    def _exchange_code(
        self, client: OAuthClientInformationFull, code: StoredCode
    ) -> OAuthToken | None:
        client_id = client.client_id or ""
        with self.db.connection() as conn:
            row = conn.execute(
                "UPDATE oauth_code SET used_at = now() WHERE code_hash = %s"
                " AND client_id = %s AND used_at IS NULL AND expires_at > now()"
                " RETURNING account_id, grant_id, scopes, resource",
                (sha256_hex(code.code), client_id),
            ).fetchone()
            if row is None:
                # Lost a race with a concurrent exchange, or expired: refuse, and
                # kill whatever the winner got (a code must work once).
                if code.grant_id:
                    grants.revoke_grant_key(conn, code.grant_id)
                return None
            account = accounts.get_account(conn, row["account_id"])
            if account is None or account.status not in ("active", "provisional"):
                return None
            label = (client.client_name or "an assistant")[: config.CLIENT_NAME_LENGTH]
            grant_id = grants.create_grant(
                conn,
                account_id=account.id,
                client_id=client_id,
                label=label,
                scopes=list(row["scopes"]),
                resource=row["resource"],
                grant_id=row["grant_id"] or None,
            )
            return grants.issue_pair(
                conn,
                account_id=account.id,
                client_id=client_id,
                grant_id=grant_id,
                label=label,
                scopes=list(row["scopes"]),
                resource=row["resource"],
            )

    # --- refresh with rotation -------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefresh | None:
        return await anyio.to_thread.run_sync(
            self._load_refresh, client.client_id or "", refresh_token
        )

    def _load_refresh(self, client_id: str, raw: str) -> StoredRefresh | None:
        if not secret_shape(raw, grants.REFRESH_PREFIX):
            return None
        with self.db.connection() as conn:
            # Consumed rows load on purpose: exchange must see the replay.
            row = conn.execute(
                "SELECT account_id, grant_id, scopes, resource,"
                " extract(epoch FROM expires_at) AS exp FROM oauth_token"
                " WHERE token_hash = %s AND kind = 'refresh' AND client_id = %s"
                " AND revoked_at IS NULL AND expires_at > now()"
                " AND EXISTS (SELECT 1 FROM oauth_grant g"
                "   WHERE g.grant_id = oauth_token.grant_id AND g.revoked_at IS NULL)",
                (sha256_hex(raw), client_id),
            ).fetchone()
        if row is None or not row["grant_id"]:
            return None
        return StoredRefresh(
            token=raw,
            client_id=client_id,
            scopes=list(row["scopes"]),
            expires_at=int(row["exp"]),
            resource=row["resource"],
            subject=str(row["account_id"]),
            grant_id=row["grant_id"],
            account_id=row["account_id"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefresh,
        scopes: list[str],
    ) -> OAuthToken:
        token = await anyio.to_thread.run_sync(
            self._exchange_refresh, client.client_id or "", refresh_token, scopes
        )
        if token is None:
            raise _invalid_grant()
        return token

    def _exchange_refresh(
        self, client_id: str, token: StoredRefresh, scopes: list[str]
    ) -> OAuthToken | None:
        replayed = False
        with self.db.connection() as conn:
            # The grant row is authoritative and is locked FIRST (revoking
            # updates it first), so a rotation and a revocation serialise: the
            # rotation either sees the grant revoked and refuses, or it commits
            # its new pair before the revocation proceeds, and the revocation's
            # next statement sees and revokes that pair.
            grant = conn.execute(
                "SELECT revoked_at FROM oauth_grant WHERE grant_id = %s FOR SHARE",
                (token.grant_id,),
            ).fetchone()
            if grant is None or grant["revoked_at"] is not None:
                return None
            row = conn.execute(
                "SELECT id, label, consumed_at, scopes, resource FROM oauth_token"
                " WHERE token_hash = %s AND kind = 'refresh' AND client_id = %s"
                " AND revoked_at IS NULL AND expires_at > now() FOR UPDATE",
                (sha256_hex(token.token), client_id),
            ).fetchone()
            if row is None:
                return None
            if row["consumed_at"] is not None:
                # A rotated refresh token came back: someone holds a stale copy.
                replayed = True
            else:
                account = accounts.get_account(conn, token.account_id)
                if account is None or account.status not in ("active", "provisional"):
                    return None
                conn.execute(
                    "UPDATE oauth_token SET consumed_at = now() WHERE id = %s",
                    (row["id"],),
                )
                return grants.issue_pair(
                    conn,
                    account_id=account.id,
                    client_id=client_id,
                    grant_id=token.grant_id,
                    label=row["label"] or "an assistant",
                    scopes=[s for s in scopes if s in row["scopes"]]
                    or list(row["scopes"]),
                    resource=row["resource"],
                )
        if replayed:
            # Kill the whole grant, in its own transaction: the share lock above
            # is released, so two concurrent replays cannot deadlock upgrading it.
            with self.db.connection() as conn:
                grants.revoke_grant_key(conn, token.grant_id)
        return None

    # --- bearer tokens ---------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self._load_access, token)

    def _load_access(self, token: str) -> AccessToken | None:
        with self.db.connection() as conn:
            found = accounts.lookup_token(conn, token)
        if found is None:
            return None
        account, info = found
        # Operator tokens (accounts.mint_token) carry no resource: they are
        # for this server by construction. Issued tokens must name it.
        if info.resource is not None and not same_resource(
            info.resource, self.resource
        ):
            return None
        return AccessToken(
            token="",  # never keep the raw token around
            client_id=info.client_id or DEFAULT_CLIENT_ID,
            scopes=list(info.scopes),
            expires_at=int(info.expires_at.timestamp()) if info.expires_at else None,
            resource=self.resource,
            subject=str(account.id),
            claims={
                "iss": self.settings.base_url,
                # which connection this is (connection.py), for the tool call
                connection.CLAIM: connection.key(
                    grant_id=info.grant_id, token_id=info.token_id
                ),
            },
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await anyio.to_thread.run_sync(
            self.revoke_by_token, token.client_id, token.token
        )

    def revoke_by_token(self, client_id: str, raw: str) -> None:
        """Revoke the grant a token belongs to (or the lone token), if it is ours."""
        if not raw or len(raw) > 300:
            return
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT id, grant_id FROM oauth_token WHERE token_hash = %s"
                " AND client_id = %s",
                (sha256_hex(raw), client_id),
            ).fetchone()
            if row is None:
                return
            if row["grant_id"]:
                grants.revoke_grant_key(conn, row["grant_id"])
            else:
                conn.execute(
                    "UPDATE oauth_token SET revoked_at = now() WHERE id = %s"
                    " AND revoked_at IS NULL",
                    (row["id"],),
                )
