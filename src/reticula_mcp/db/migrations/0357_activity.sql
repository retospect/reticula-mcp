-- 0357 (RECENT lane): the activity log, one row per tool call of a person
-- (activity.py), kept for RETICULA_ACTIVITY_DAYS days.
--
-- A row is only about the caller's own call: `at` is when it ended,
-- `account_id` the account that made it, `connection` its connection key
-- (connection.py), `client` the assistant's name and the start of its client
-- id, `tool` the tool, `outcome` `ok` or the error code, and `ref` the short id
-- of the note or hub the call named when the caller wrote it. Never a query,
-- a text, a handle or an address.
--
-- The table is partitioned by day. The server makes the partitions
-- (`activity_YYYYMMDD`) when it starts and every hour, and drops the ones that
-- are older than the setting, so retention never deletes a row. A row whose
-- account is deleted goes with it.
--
-- A new table and a new index: no existing table, column or row changes.
CREATE TABLE IF NOT EXISTS activity (
    at          timestamptz NOT NULL,
    account_id  bigint      NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    connection  text,
    client      text,
    tool        text        NOT NULL,
    outcome     text        NOT NULL,
    ref         text
) PARTITION BY RANGE (at);

CREATE INDEX IF NOT EXISTS activity_account_at_idx ON activity (account_id, at DESC);
