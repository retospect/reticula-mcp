-- 0100: OAuth authorization server (sign-in lane). Reshapes the 0004 tables a
-- little and adds the per-assistant grant table and the failure counters.

-- A refresh token is "consumed" when it has been rotated; presenting a consumed
-- one again is a replay and kills its whole grant. (revoked_at stays for
-- explicit revocation, so the two cases can be told apart.)
ALTER TABLE oauth_token ADD COLUMN consumed_at timestamptz;

-- The grant an authorization code will belong to (created when the code is
-- exchanged), so a replayed code can revoke what the first exchange issued.
ALTER TABLE oauth_code ADD COLUMN grant_id text;

-- Digest of the CSRF nonce of the sign-in page that parked this request.
ALTER TABLE oauth_pending ADD COLUMN csrf_hash text;

-- One row per connected assistant. Tokens point at it through
-- oauth_token.grant_id (the text key); `id` is what `connections revoke` takes.
CREATE TABLE oauth_grant (
    id          bigserial PRIMARY KEY,
    grant_id    text   NOT NULL UNIQUE,
    account_id  bigint NOT NULL REFERENCES account(id),
    client_id   text   NOT NULL,
    label       text   NOT NULL,                  -- from the registered client name
    scopes      text[] NOT NULL DEFAULT '{}',
    resource    text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);
CREATE INDEX oauth_grant_account_idx ON oauth_grant (account_id);

-- Sliding-window event counters for throttling (failed sign-ins per address and
-- per e-mail, client registrations per address). Sign-ups use signup_attempt.
CREATE TABLE auth_event (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,                     -- signin_ip | signin_email | register_ip
    subject    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_event_idx ON auth_event (kind, subject, created_at);
