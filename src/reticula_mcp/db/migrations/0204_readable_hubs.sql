-- 0204 (GRAPH lane number, given by the coordinator; written by the search
-- lane): the hubs an account may read become a function of their own.
--
-- readable_nodes (0200) worked them out inside itself, as "reach", and nothing
-- else could ask for them. A reader that only needs to know "may the viewer
-- read this hub" had to ask for every readable note to find out. Now:
--
-- readable_hubs(viewer): the hubs the viewer may read. It is the "reach" of
-- 0200 taken out word for word: the hubs the viewer wrote, the hubs they hold a
-- 'read' or 'add' grant on, and every hub filed (by its own author, not
-- withdrawn, membership audience 'hub') under a hub already in it. A hub that
-- is deleted is never in it; a hub whose audience is not 'hub' is in it for
-- its author only.
--
-- readable_nodes(viewer): redefined on top of readable_hubs. Its three
-- branches are the ones of 0200, unchanged. The hubs are read once
-- (MATERIALIZED) for the two branches that use them, as the recursive "reach"
-- was before.
--
-- Every hub in readable_hubs is in readable_nodes, and every hub in
-- readable_nodes is in readable_hubs: "h is a hub the viewer may read" can be
-- asked of either. tests/test_readable_hubs.py holds the body of 0200 as text
-- and compares both functions with it on generated graphs.
--
-- current_nodes is untouched. No table, no index and no row is changed.

CREATE OR REPLACE FUNCTION readable_hubs(viewer bigint) RETURNS TABLE (hub_id bigint)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE me AS (
        SELECT a.person_id FROM account a WHERE a.id = viewer
    ),
    reach(hub_id) AS (
        SELECT h.id
        FROM node h JOIN me ON h.author = me.person_id
        WHERE h.kind = 'hub' AND h.tombstoned_at IS NULL
      UNION
        SELECT h.id
        FROM hub_grant g
        JOIN me ON g.grantee = me.person_id
        JOIN node h ON h.id = g.hub_node_id
        WHERE g.mode IN ('read', 'add')
          AND h.kind = 'hub' AND h.tombstoned_at IS NULL AND h.audience = 'hub'
      UNION
        SELECT c.id
        FROM reach r
        JOIN membership m ON m.hub_node_id = r.hub_id
        JOIN node c ON c.id = m.node_id
        WHERE NOT m.withdrawn AND m.audience = 'hub' AND m.author = c.author
          AND c.kind = 'hub' AND c.tombstoned_at IS NULL AND c.audience = 'hub'
    )
    SELECT r.hub_id FROM reach r
$$;

CREATE OR REPLACE FUNCTION readable_nodes(viewer bigint) RETURNS TABLE (node_id bigint)
LANGUAGE sql STABLE AS $$
    WITH me AS (
        SELECT a.person_id FROM account a WHERE a.id = viewer
    ),
    reach AS MATERIALIZED (
        SELECT h.hub_id FROM readable_hubs(viewer) h
    )
    SELECT n.id
    FROM node n JOIN me ON n.author = me.person_id
    WHERE n.tombstoned_at IS NULL
  UNION
    SELECT n.id
    FROM reach r JOIN node n ON n.id = r.hub_id
    WHERE n.tombstoned_at IS NULL AND n.audience = 'hub'
  UNION
    SELECT n.id
    FROM reach r
    JOIN membership m ON m.hub_node_id = r.hub_id
    JOIN node n ON n.id = m.node_id
    WHERE NOT m.withdrawn AND m.audience = 'hub' AND m.author = n.author
      AND n.tombstoned_at IS NULL AND n.audience = 'hub'
$$;
