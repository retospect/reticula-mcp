-- 0350 (RECENT lane): what changed since a moment. search(since=...) and the
-- lines about new notes in start ask for the notes with a version since a
-- time and for the filings signed since then (search/recent.py). Without
-- these three indexes they read the node table and the record log in full.
-- Indexes only: no table, column, function or row changes.

-- notes and hubs by the time of their newest version
CREATE INDEX IF NOT EXISTS node_updated_idx ON node (updated_at);

-- filing statements by the time they were signed
CREATE INDEX IF NOT EXISTS record_member_created_idx
    ON record (created_at) WHERE type = 'member';

-- from a filing statement's newest record to the filing
CREATE INDEX IF NOT EXISTS membership_record_idx ON membership (record_id);
