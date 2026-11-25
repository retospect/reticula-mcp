-- 0101: indexes for the OAuth server's housekeeping and per-address caps, and
-- the address a parked authorization request came from.

ALTER TABLE oauth_pending ADD COLUMN ip text;

CREATE INDEX oauth_pending_client_idx  ON oauth_pending (client_id);
CREATE INDEX oauth_pending_ip_idx      ON oauth_pending (ip) WHERE ip IS NOT NULL;
CREATE INDEX oauth_pending_expires_idx ON oauth_pending (expires_at);

CREATE INDEX oauth_code_client_idx  ON oauth_code (client_id);
CREATE INDEX oauth_code_expires_idx ON oauth_code (expires_at);

CREATE INDEX oauth_token_client_idx  ON oauth_token (client_id) WHERE client_id IS NOT NULL;
CREATE INDEX oauth_token_expires_idx ON oauth_token (expires_at) WHERE expires_at IS NOT NULL;

CREATE INDEX oauth_grant_client_idx ON oauth_grant (client_id);
