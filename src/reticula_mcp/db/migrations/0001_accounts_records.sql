-- 0001: extensions, accounts, keys, and the append-only signed record log.
-- FROZEN (foundation range 0001-0099). Lanes add tables in their own ranges.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- One row per person on this instance. person_id is derived from the ROOT
-- signing key at sign-up and never changes (format v0).
CREATE TABLE account (
    id               bigserial PRIMARY KEY,
    person_id        text    NOT NULL UNIQUE,
    email            citext  NOT NULL UNIQUE,
    -- '$argon2id$...' or '!' (no usable password: the system account)
    password_hash    text    NOT NULL,
    handle           citext  NOT NULL UNIQUE,
    -- address of this account's identity record; later versions share it
    identity_address text    NOT NULL UNIQUE,
    verified         boolean NOT NULL DEFAULT false,
    verified_at      timestamptz,
    status           text    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'provisional', 'disabled', 'system')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Key slots: root (ed25519 sign, role root), k1 (ed25519 sign, role delegate),
-- e1 (x25519 encrypt, unused in v0). Private keys are held UNENCRYPTED by the
-- instance (owner decision: a hosted instance holds the key). alg names are
-- stored so algorithms can be replaced.
CREATE TABLE account_key (
    id          bigserial PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES account(id),
    key_id      text   NOT NULL,
    use         text   NOT NULL CHECK (use IN ('sign', 'encrypt')),
    role        text   CHECK (role IN ('root', 'delegate')),
    alg         text   NOT NULL,
    public_key  bytea  NOT NULL,
    private_key bytea,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz,
    UNIQUE (account_id, key_id)
);

-- The append-only log of signed records. Each account's feed is
-- WHERE author = ... ORDER BY id. envelope/signature are the exact bytes that
-- were signed; they are stored and re-served byte for byte, never rebuilt.
CREATE TABLE record (
    id             bigserial PRIMARY KEY,
    version_hash   text   NOT NULL UNIQUE,
    address        text   NOT NULL,
    type           text   NOT NULL,
    author         text   NOT NULL,
    key_id         text   NOT NULL,
    created_at     timestamptz NOT NULL,
    parents        text[] NOT NULL DEFAULT '{}',
    hub_id         text,
    audience       text   NOT NULL,
    content_hash   text   NOT NULL,
    format_version integer NOT NULL,
    envelope       bytea  NOT NULL,
    signature      bytea  NOT NULL,
    received_at    timestamptz NOT NULL DEFAULT now(),
    -- set only by blank_record_content(): the envelope/signature were erased
    blanked_at     timestamptz
);
CREATE INDEX record_author_idx  ON record (author, id);
CREATE INDEX record_address_idx ON record (address, id);
CREATE INDEX record_hub_idx     ON record (hub_id) WHERE hub_id IS NOT NULL;
CREATE INDEX record_type_idx    ON record (type);

-- Append-only: no UPDATE, no DELETE ... except the one deliberate path below.
CREATE FUNCTION record_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'record is append-only (DELETE refused)';
    END IF;
    -- UPDATE is allowed only through blank_record_content(), which sets this
    -- transaction-local flag, and only to erase envelope+signature.
    IF coalesce(current_setting('reticula.blank_record', true), '') = 'on'
       AND OLD.blanked_at IS NULL
       AND NEW.blanked_at IS NOT NULL
       AND NEW.envelope = '\x'::bytea
       AND NEW.signature = '\x'::bytea
       AND (NEW.id, NEW.version_hash, NEW.address, NEW.type, NEW.author, NEW.key_id,
            NEW.created_at, NEW.parents, NEW.hub_id, NEW.audience, NEW.content_hash,
            NEW.format_version, NEW.received_at)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.version_hash, OLD.address, OLD.type, OLD.author, OLD.key_id,
            OLD.created_at, OLD.parents, OLD.hub_id, OLD.audience, OLD.content_hash,
            OLD.format_version, OLD.received_at)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'record is append-only (UPDATE refused)';
END $$;

CREATE TRIGGER record_append_only
    BEFORE UPDATE OR DELETE ON record
    FOR EACH ROW EXECUTE FUNCTION record_guard();

-- The deliberate erasure path for tombstoned content. Erases the envelope and
-- signature (which carry the payload) of ONE non-tombstone version of an
-- address that already has a tombstone record. The row stays (hash, address,
-- parents, author, times) so the chain of hashes is still visible, but the
-- version can no longer be served or verified. Who calls this, and when
-- ("derived" handling etc.) is the graph lane's decision.
CREATE FUNCTION blank_record_content(rec_id bigint) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    r public.record%ROWTYPE;
BEGIN
    SELECT * INTO r FROM record WHERE id = rec_id;
    IF NOT FOUND OR r.type = 'tombstone' OR r.blanked_at IS NOT NULL THEN
        RETURN false;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM record t
                   WHERE t.address = r.address AND t.type = 'tombstone') THEN
        RAISE EXCEPTION 'refusing to blank %: address has no tombstone', r.address;
    END IF;
    PERFORM set_config('reticula.blank_record', 'on', true);
    UPDATE record SET envelope = '\x'::bytea, signature = '\x'::bytea,
                      blanked_at = now()
    WHERE id = rec_id;
    PERFORM set_config('reticula.blank_record', 'off', true);
    RETURN true;
END $$;
