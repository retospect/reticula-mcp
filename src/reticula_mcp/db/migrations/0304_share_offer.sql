-- 0304 (SEARCH lane, for graph/ by RETICULA1's leave): shares that wait.
--
-- A share between two people who have no hub between them yet waits until the
-- person it is for accepts it (graph.handlers.share). Until then it is a row
-- here, never in hub_grant, so readable_nodes, readable_hubs and every read
-- path stay as they were: nothing of the hub reaches that person.
--
-- Both tables are views of the record log, like hub_grant: a grant record
-- with "pending": true makes an offer; the grantee's own grant record that
-- "answers" it turns it into a hub_grant row (read, add) or marks it
-- refused (none), and one that also "blocks" adds a share_block row, also
-- after the refusal (materialize.py).
-- A rebuild empties and refills them from the log.
--
-- Applied on a database of 0.4.2 as a pending file after 0355; it needs only
-- node, record and account (0002).

CREATE TABLE IF NOT EXISTS share_offer (
    hub_node_id        bigint      NOT NULL REFERENCES node(id),
    grantee            text        NOT NULL,           -- person_id
    grantee_account_id bigint      REFERENCES account(id),
    mode               text        NOT NULL CHECK (mode IN ('read', 'add')),
    granted_by         text        NOT NULL,           -- person_id
    address            text        NOT NULL,           -- address of the grant record
    record_id          bigint      NOT NULL REFERENCES record(id),
    created_at         timestamptz NOT NULL,
    -- the grantee blocked the sharer: kept, so the sharer's own view is the
    -- same as for any share that waits, and never shown to the grantee
    held               boolean     NOT NULL DEFAULT false,
    -- the grantee refused it: kept, never shown or accepted, so that a
    -- block afterwards still knows whose share it was
    refused            boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (hub_node_id, grantee)
);
CREATE INDEX IF NOT EXISTS share_offer_account_idx
    ON share_offer (grantee_account_id, created_at DESC);

-- share_block: the grantee refused a share and blocked its sharer. Their
-- shares that wait, and later ones, are held. A share the grantee makes to
-- them lifts the block and the hold.
CREATE TABLE IF NOT EXISTS share_block (
    person     text        NOT NULL,                   -- who blocked
    blocked    text        NOT NULL,                   -- whose shares
    record_id  bigint      NOT NULL REFERENCES record(id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (person, blocked)
);
