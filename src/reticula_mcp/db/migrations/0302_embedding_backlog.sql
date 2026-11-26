-- 0302 (SEARCH lane): the notes a write could not embed.
--
-- A write embeds the note's text in its own transaction when the model is
-- loaded, so the note is findable by meaning as soon as put or edit returns.
-- When it cannot (the model is still loading, or it failed), the write goes on
-- as before and leaves a row here. The background worker embeds them, and a
-- search embeds a few of those its viewer may read before it compares vectors.
-- Before this table a search had to look for notes without a current vector
-- among all notes, on every call.
--
-- A row says only "embed this node again"; embedding it removes the row. A
-- node that is deleted takes its row with it.
--
-- Two triggers fill it, so that no way of writing a node is missed (a tool
-- call, records applied by a command, a test that inserts rows): a node that
-- is inserted or whose head version changes, and a live node whose vector row
-- is deleted. A write that embeds the note in the same transaction removes
-- the row again before it commits. Every node that exists when this
-- migration runs and has no vector of any model goes in once.
--
-- A switch to another model is not in it. For that, embedding_complete holds
-- the one model for which the worker last found nothing left to embed. While
-- that model is in use, the backlog is all a search has to look at. With any
-- other model (just switched to, or switched back to after notes changed), a
-- search looks for notes without a vector of it among all the notes the
-- viewer may read, as before, until the worker has caught up. No other table
-- is changed.

CREATE TABLE IF NOT EXISTS embedding_backlog (
    node_id bigint PRIMARY KEY REFERENCES node(id) ON DELETE CASCADE,
    since   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS embedding_complete (
    model text PRIMARY KEY,
    at    timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION embedding_backlog_node() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tombstoned_at IS NULL THEN
        INSERT INTO embedding_backlog (node_id) VALUES (NEW.id)
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION embedding_backlog_vector() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO embedding_backlog (node_id)
    SELECT n.id FROM node n WHERE n.id = OLD.node_id AND n.tombstoned_at IS NULL
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS embedding_backlog_insert ON node;
CREATE TRIGGER embedding_backlog_insert AFTER INSERT ON node
    FOR EACH ROW EXECUTE FUNCTION embedding_backlog_node();

DROP TRIGGER IF EXISTS embedding_backlog_head ON node;
CREATE TRIGGER embedding_backlog_head AFTER UPDATE OF head_version ON node
    FOR EACH ROW WHEN (OLD.head_version IS DISTINCT FROM NEW.head_version)
    EXECUTE FUNCTION embedding_backlog_node();

DROP TRIGGER IF EXISTS embedding_backlog_vector_gone ON embedding;
CREATE TRIGGER embedding_backlog_vector_gone AFTER DELETE ON embedding
    FOR EACH ROW EXECUTE FUNCTION embedding_backlog_vector();

INSERT INTO embedding_backlog (node_id)
SELECT n.id FROM node n
WHERE n.tombstoned_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM embedding e WHERE e.node_id = n.id)
ON CONFLICT DO NOTHING;
