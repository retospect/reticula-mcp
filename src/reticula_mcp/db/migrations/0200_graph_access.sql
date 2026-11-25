-- 0200: the real visibility rule and the access helpers (GRAPH lane).
--
-- readable_nodes(viewer) is the ONE place that decides what an account may
-- read. Semantics (see also 0005_visibility.sql):
--   * the viewer's own non-tombstoned atoms;
--   * "reach": the hubs the viewer may read = hubs they authored, hubs they hold a
--     'read'/'add' grant on, and, transitively, every hub filed (by its own
--     author, non-withdrawn, membership audience 'hub') under a hub in reach;
--     tombstoned hubs and hubs whose audience is not 'hub' are never entered
--     through a grant or a membership edge;
--   * a hub in reach is readable; so is every atom with audience 'hub' that has a
--     non-withdrawn membership (audience 'hub', filed by the atom's own author)
--     in a hub in reach;
--   * audience 'private' (and any unknown audience) is author-only, always;
--   * tombstoned atoms are never readable.
-- A grant with mode 'none' only means "no grant here": there are no deny rules,
-- so access inherited from a hub above stays.
-- Everything fails closed: an unknown audience, a statement filed by someone
-- other than the atom's author, or a withdrawn statement grants nothing.

CREATE INDEX hub_grant_grantee_idx ON hub_grant (grantee) WHERE mode <> 'none';
CREATE INDEX node_hub_author_idx   ON node (author) WHERE kind = 'hub';
CREATE INDEX membership_active_idx ON membership (node_id, hub_node_id) WHERE NOT withdrawn;

CREATE OR REPLACE FUNCTION readable_nodes(viewer bigint) RETURNS TABLE (node_id bigint)
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

-- hub_access(person, hub): the strongest access person_id holds on a hub, as
-- 'add', 'read' or NULL. Same rule as readable_nodes, walked UPWARD from the hub:
-- the author of the hub or of any hub above it is its admin ('add'); a grant on
-- the hub or any hub above it gives its mode. Private and tombstoned hubs
-- confer nothing to anyone but their author.
CREATE OR REPLACE FUNCTION hub_access(person text, hub bigint) RETURNS text
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up(id) AS (
        SELECT h.id FROM node h
        WHERE h.id = hub AND h.kind = 'hub' AND h.tombstoned_at IS NULL
      UNION
        SELECT p.id
        FROM up
        JOIN node c ON c.id = up.id
        JOIN membership m ON m.node_id = up.id
        JOIN node p ON p.id = m.hub_node_id
        WHERE NOT m.withdrawn AND m.audience = 'hub' AND m.author = c.author
          AND c.audience = 'hub'
          AND p.kind = 'hub' AND p.tombstoned_at IS NULL
    )
    SELECT CASE
        WHEN bool_or(a.author = person) OR bool_or(g.mode = 'add') THEN 'add'
        WHEN bool_or(g.mode = 'read') THEN 'read'
    END
    FROM up
    JOIN node a ON a.id = up.id
    LEFT JOIN hub_grant g
           ON g.hub_node_id = up.id AND g.grantee = person
          AND g.mode IN ('read', 'add') AND a.audience = 'hub'
$$;

-- can_add_person(person, hub): may person_id file atoms under hub? The hub's
-- admin or anyone holding 'add' on it or a hub above it, or anyone when the hub's
-- payload says "accept": "anyone" (which makes nobody a reader).
CREATE OR REPLACE FUNCTION can_add_person(person text, hub bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce(hub_access(person, hub) = 'add', false)
        OR EXISTS (
            SELECT 1 FROM node h
            WHERE h.id = hub AND h.kind = 'hub' AND h.tombstoned_at IS NULL
              AND h.audience = 'hub' AND h.payload->>'accept' = 'anyone'
        )
$$;
