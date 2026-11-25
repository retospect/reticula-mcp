-- 0353 (RECENT lane): readable_if_live(viewer, ids): of the given notes, those
-- the viewer could read were they not deleted. readable_nodes (0200, 0204)
-- leaves every deleted note out; a reader who could read a note before it was
-- deleted may still be told that it was deleted, never what it said.
--
-- The rule is readable_nodes' without the tombstone: the viewer's own notes,
-- and a note in audience 'hub' that its own author filed (not withdrawn,
-- membership audience 'hub') in a hub the viewer may read (readable_hubs).
-- A filing by anyone else only organises and tells nobody anything.
--
-- Callers: get of a deleted address (search/read.py _withdrawn) and the lines
-- about deleted notes in search(since=...) (search/recent.py). One copy of
-- the rule; tests/test_readable_if_live.py compares it with readable_nodes on
-- generated graphs (every live note it returns is one readable_nodes returns,
-- and the other way round).
--
-- A new function: no table, column, index or row changes.
CREATE OR REPLACE FUNCTION readable_if_live(viewer bigint, ids bigint[])
RETURNS TABLE (node_id bigint)
LANGUAGE sql STABLE AS $$
    WITH me AS (
        SELECT a.person_id FROM account a WHERE a.id = viewer
    ),
    reach AS MATERIALIZED (
        SELECT h.hub_id FROM readable_hubs(viewer) h
    )
    SELECT n.id
    FROM node n JOIN me ON n.author = me.person_id
    WHERE n.id = ANY (ids)
  UNION
    SELECT n.id
    FROM node n
    JOIN membership m ON m.node_id = n.id
    JOIN reach r ON r.hub_id = m.hub_node_id
    WHERE n.id = ANY (ids)
      AND n.audience = 'hub'
      AND NOT m.withdrawn AND m.audience = 'hub' AND m.author = n.author
  UNION
    -- a hub the viewer reaches by a grant (readable_hubs knows live hubs only:
    -- a deleted hub is told by the arms above, or not at all)
    SELECT r.hub_id FROM reach r WHERE r.hub_id = ANY (ids)
$$;
