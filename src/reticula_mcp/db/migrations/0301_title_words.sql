-- 0301 (SEARCH lane): which titles have one of these words. The search asks
-- this to find a short title inside a long query (find._trigram). node.fts
-- answers it for title and text together, which in a big account is most
-- notes for any common word; this index answers it for the title alone.
-- Nothing else changes.

CREATE INDEX IF NOT EXISTS node_title_words_idx
    ON node USING gin (to_tsvector('english', coalesce(title, '')));
