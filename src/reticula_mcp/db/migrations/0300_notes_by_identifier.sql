-- 0300 (SEARCH lane): get of a paper lists the notes written about it. A note
-- is about a paper when its payload carries the paper's identifier
-- (payload.ext.key, the same key as the paper's node.ext_key). This index finds
-- them; nothing else changes.

CREATE INDEX IF NOT EXISTS node_about_ext_key_idx
    ON node ((payload->'ext'->>'key'))
    WHERE kind <> 'paper' AND payload ? 'ext';
