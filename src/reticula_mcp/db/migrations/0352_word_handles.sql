-- 0352 (RECENT lane): word handles (handle_words.py) and the settings of a
-- connection (connection.py).
--
-- handle_number(address): the last 28 bits of an address, the random tail of
-- its UUID, which a note's words name; NULL for an address that does not end
-- in seven hex digits (the index computes it for every row written, so it must
-- not fail). The index lets two to four words be looked up as a range of
-- these numbers.
--
-- connection_setting: what one connection switched on, such as word handles
-- (profile(word_handles="on")). A connection is 'grant:<grant id>' or
-- 'token:<token row id>'.
--
-- A new function, a new index and a new table: no existing table, column or
-- row changes.
CREATE OR REPLACE FUNCTION handle_number(address text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN right(address, 7) ~ '^[0-9a-fA-F]{7}$'
                THEN ('x' || right(address, 7))::bit(28)::integer END
$$;

CREATE INDEX IF NOT EXISTS node_handle_idx ON node (handle_number(address));

CREATE TABLE IF NOT EXISTS connection_setting (
    connection  text        NOT NULL,
    account_id  bigint      NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    name        text        NOT NULL,
    value       text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection, name)
);
