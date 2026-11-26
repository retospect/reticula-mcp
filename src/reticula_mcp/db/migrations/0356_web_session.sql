-- 0356 (RECENT lane): sessions of the account pages (auth/websession.py).
--
-- A session is a random token in a cookie; only its SHA-256 digest is stored.
-- expires_at is its absolute end (12 hours, or 30 days with "keep me signed
-- in"); idle_seconds how long it may go unused; reauth_at when the password
-- was last given again (export, revoke and delete need it within 10 minutes).
-- A session is never a bearer token for /mcp, and a token never a session.
--
-- A new table: no existing table, column or row changes.
CREATE TABLE IF NOT EXISTS web_session (
    id_hash       text        PRIMARY KEY,
    account_id    bigint      NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    idle_seconds  integer     NOT NULL,
    reauth_at     timestamptz,
    keep          boolean     NOT NULL DEFAULT false,
    agent         text        NOT NULL DEFAULT '',
    bucket        text        NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS web_session_account_idx ON web_session (account_id);
