-- 0303 (SEARCH lane): a deleted vector puts its node into embedding_backlog
-- (0302) only when the vector was of the model in embedding_complete, the one
-- in use and caught up.
--
-- 0302 did it for a vector of any model. After a switch, removing the vectors
-- of the old model (reticula-mcp reindex --drop-other-models) would have put
-- every note into the backlog although the new model has them all. While no
-- model is in embedding_complete (a switch the worker has not caught up yet),
-- a search looks among all notes anyway, so nothing is missed.
--
-- No table, no index and no row is changed.

CREATE OR REPLACE FUNCTION embedding_backlog_vector() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO embedding_backlog (node_id)
    SELECT n.id FROM node n
    WHERE n.id = OLD.node_id AND n.tombstoned_at IS NULL
      AND EXISTS (SELECT 1 FROM embedding_complete c WHERE c.model = OLD.model)
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END
$$;
