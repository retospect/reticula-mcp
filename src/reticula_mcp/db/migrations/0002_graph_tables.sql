-- 0002: materialised graph state (rebuildable from `record`).
-- Table shapes are FROZEN; the graph lane fills them and may add indexes or
-- columns in its own range (0200-0299), but never changes these.

-- Local mapping + head state of ATOMS. id is a private local key: it never
-- leaves this instance; the address is the identity everywhere else.
CREATE TABLE node (
    id                bigserial PRIMARY KEY,
    address           text   NOT NULL UNIQUE,
    author            text   NOT NULL,                 -- person_id
    author_account_id bigint REFERENCES account(id),   -- null for foreign authors
    kind              text   NOT NULL,                 -- note|hub|paper|instruction|debug|...
    head_record_id    bigint NOT NULL REFERENCES record(id),
    head_version      text   NOT NULL,                 -- version hash of the head
    title             text,
    text              text   NOT NULL DEFAULT '',
    tags              text[] NOT NULL DEFAULT '{}',
    payload           jsonb  NOT NULL DEFAULT '{}',
    ext_key           text,                            -- doi:10.1000/x, arxiv:2401.00001
    audience          text   NOT NULL DEFAULT 'hub',   -- hub|private
    tombstoned_at     timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    fts tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || text)
    ) STORED
);
CREATE INDEX node_author_idx   ON node (author);
CREATE INDEX node_kind_idx     ON node (kind);
CREATE INDEX node_ext_key_idx  ON node (ext_key) WHERE ext_key IS NOT NULL;
CREATE INDEX node_tags_idx     ON node USING gin (tags);
CREATE INDEX node_fts_idx      ON node USING gin (fts);
CREATE INDEX node_title_trgm   ON node USING gin (title gin_trgm_ops);
CREATE INDEX node_text_trgm    ON node USING gin (text gin_trgm_ops);
-- "ends with these 6+ characters" resolution (resolve_ref)
CREATE INDEX node_address_tail ON node (reverse(address) text_pattern_ops);

-- "This atom belongs to that hub" statements (type 'member' records).
CREATE TABLE membership (
    id           bigserial PRIMARY KEY,
    address      text   NOT NULL UNIQUE,   -- address of the member record
    node_id      bigint NOT NULL REFERENCES node(id),
    hub_node_id  bigint NOT NULL REFERENCES node(id),
    author       text   NOT NULL,
    audience     text   NOT NULL DEFAULT 'hub',
    withdrawn    boolean NOT NULL DEFAULT false,
    record_id    bigint NOT NULL REFERENCES record(id),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX membership_hub_idx  ON membership (hub_node_id) WHERE NOT withdrawn;
CREATE INDEX membership_node_idx ON membership (node_id);

-- One effective row per (hub, person); history lives in `record`.
CREATE TABLE hub_grant (
    id                 bigserial PRIMARY KEY,
    hub_node_id        bigint NOT NULL REFERENCES node(id),
    grantee            text   NOT NULL,                  -- person_id
    grantee_account_id bigint REFERENCES account(id),
    mode               text   NOT NULL CHECK (mode IN ('read', 'add', 'none')),
    granted_by         text   NOT NULL,                  -- person_id
    address            text   NOT NULL,                  -- address of the grant record
    record_id          bigint NOT NULL REFERENCES record(id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (hub_node_id, grantee)
);
CREATE INDEX hub_grant_account_idx ON hub_grant (grantee_account_id);

CREATE TABLE supersedes (
    new_node_id bigint NOT NULL REFERENCES node(id),
    old_node_id bigint NOT NULL REFERENCES node(id),
    PRIMARY KEY (new_node_id, old_node_id)
);
CREATE INDEX supersedes_old_idx ON supersedes (old_node_id);

CREATE TABLE link (
    src_node_id bigint NOT NULL REFERENCES node(id),
    rel         text   NOT NULL,
    dst_address text   NOT NULL,
    dst_node_id bigint REFERENCES node(id),
    PRIMARY KEY (src_node_id, rel, dst_address)
);
CREATE INDEX link_dst_idx ON link (dst_node_id);
