-- 0004: OAuth + sign-up tables. OWNED BY the sign-in lane, which may reshape
-- them in its own migration (0100-0199). Tokens are stored hashed (sha256 hex),
-- never in clear. Foundation code touches only `oauth_token`
-- (accounts.mint_token / account_for_token).

CREATE TABLE oauth_client (
    client_id   text PRIMARY KEY,
    client_info jsonb NOT NULL,                 -- RFC 7591 registration document
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- An authorize request parked while the user signs in or signs up.
CREATE TABLE oauth_pending (
    id         text PRIMARY KEY,                -- random handle carried by the login page
    client_id  text NOT NULL REFERENCES oauth_client(client_id),
    params     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE oauth_code (
    code_hash      text PRIMARY KEY,
    client_id      text NOT NULL REFERENCES oauth_client(client_id),
    account_id     bigint NOT NULL REFERENCES account(id),
    code_challenge text NOT NULL,
    redirect_uri   text NOT NULL,
    redirect_uri_provided_explicitly boolean NOT NULL DEFAULT true,
    scopes         text[] NOT NULL DEFAULT '{}',
    resource       text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    used_at        timestamptz
);

-- One row per issued token. client_id is null for operator-minted tokens
-- (`reticula-mcp token`, accounts.mint_token). grant_id ties an access token
-- to the refresh token / connection it came from so one assistant connection
-- can be revoked as a unit.
CREATE TABLE oauth_token (
    id           bigserial PRIMARY KEY,
    token_hash   text   NOT NULL UNIQUE,
    kind         text   NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id    text,
    account_id   bigint NOT NULL REFERENCES account(id),
    scopes       text[] NOT NULL DEFAULT '{}',
    resource     text,
    grant_id     text,
    label        text,
    expires_at   timestamptz,
    revoked_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);
CREATE INDEX oauth_token_account_idx ON oauth_token (account_id);
CREATE INDEX oauth_token_grant_idx   ON oauth_token (grant_id) WHERE grant_id IS NOT NULL;

-- Rate limiting of sign-ups (per IP).
CREATE TABLE signup_attempt (
    id         bigserial PRIMARY KEY,
    ip         text NOT NULL,
    email      text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX signup_attempt_ip_idx ON signup_attempt (ip, created_at);
