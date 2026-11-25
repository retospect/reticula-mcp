-- 0351 (RECENT lane): the connections that `start` has pointed to the
-- instruction on using Reticula as the person's memory (connection.py). A
-- connection is a connector sign-in ('grant:<grant id>') or a minted token
-- ('token:<token row id>'). One row per connection, written once, by the
-- first `start` through it.
-- A new table: no existing table, column, function or row changes.
CREATE TABLE IF NOT EXISTS onboarding_shown (
    connection  text        PRIMARY KEY,
    account_id  bigint      NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    shown_at    timestamptz NOT NULL DEFAULT now()
);
