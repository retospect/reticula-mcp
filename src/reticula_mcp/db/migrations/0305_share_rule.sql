-- 0305 (SEARCH lane, for graph/ by RETICULA1's leave): from here on, whether
-- a share waits for its grantee is decided when its record is applied, from
-- the tables as they are then (materialize._apply_grant), never from the
-- record. A rebuild applies the same rule in log order, so it decides the
-- same.
--
-- share_rule_start holds the id of the last record written before this
-- migration. A grant record up to it keeps the meaning it was written with:
-- one of 0.4 and before applies at once, as it did then; one written while
-- 0304 alone was in place waits when it says "pending". So a rebuild does not
-- turn the shares people had into shares that wait.
--
-- share_offer gains an index for the counts per grantee and sharer.

CREATE TABLE IF NOT EXISTS share_rule_start (
    one       boolean PRIMARY KEY DEFAULT true CHECK (one),
    record_id bigint  NOT NULL
);
INSERT INTO share_rule_start (record_id)
SELECT coalesce(max(id), 0) FROM record
ON CONFLICT (one) DO NOTHING;

CREATE INDEX IF NOT EXISTS share_offer_grantee_idx
    ON share_offer (grantee, granted_by);
