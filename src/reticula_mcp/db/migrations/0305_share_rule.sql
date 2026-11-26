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
--
-- hub_grant gains the yes a grant carries (yes_by said yes to yes_to): the
-- grantee's accepting answer to a share of the sharer's, or a share of the
-- granter that applied at once. A change between read and add keeps it; mode
-- none ends it, as the grant ends (access.have_shared reads read/add rows
-- only). Rows there now get it from the record that set them; the server's own
-- grants carry none.
--
-- share_refusal: a share the grantee refused, held against that one sharer
-- of that hub (share_offer had one row per hub and grantee, so a refusal
-- there kept out every other sharer of the hub). share_offer.refused stays,
-- always false: the refused rows move here.

CREATE TABLE IF NOT EXISTS share_rule_start (
    one       boolean PRIMARY KEY DEFAULT true CHECK (one),
    record_id bigint  NOT NULL
);
INSERT INTO share_rule_start (record_id)
SELECT coalesce(max(id), 0) FROM record
ON CONFLICT (one) DO NOTHING;

CREATE INDEX IF NOT EXISTS share_offer_grantee_idx
    ON share_offer (grantee, granted_by);

ALTER TABLE hub_grant ADD COLUMN IF NOT EXISTS yes_by text;
ALTER TABLE hub_grant ADD COLUMN IF NOT EXISTS yes_to text;
UPDATE hub_grant g
SET yes_by = r.author,
    yes_to = CASE WHEN r.author = g.grantee THEN g.granted_by ELSE g.grantee END
FROM record r
WHERE r.id = g.record_id AND g.yes_by IS NULL
  AND g.mode IN ('read', 'add') AND g.grantee <> 'public'
  AND r.author IN (g.grantee, g.granted_by)
  AND NOT EXISTS (SELECT 1 FROM account a WHERE a.person_id = r.author
                  AND a.status = 'system');
CREATE INDEX IF NOT EXISTS hub_grant_yes_idx ON hub_grant (yes_by, yes_to);

CREATE TABLE IF NOT EXISTS share_refusal (
    hub_node_id bigint      NOT NULL REFERENCES node(id),
    grantee     text        NOT NULL,           -- person_id who refused
    sharer      text        NOT NULL,           -- person_id refused
    address     text        NOT NULL,           -- address of the refused share's record
    record_id   bigint      NOT NULL REFERENCES record(id),  -- the refusal
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (hub_node_id, grantee, sharer)
);
INSERT INTO share_refusal (hub_node_id, grantee, sharer, address, record_id, created_at)
SELECT hub_node_id, grantee, granted_by, address, record_id, created_at
FROM share_offer WHERE refused
ON CONFLICT DO NOTHING;
DELETE FROM share_offer WHERE refused;
