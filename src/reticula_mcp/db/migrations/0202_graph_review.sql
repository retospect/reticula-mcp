-- 0202: findings of the permission review (GRAPH lane).
--
--  * membership.moderated: this (withdrawn) statement is a hub admin's removal of
--    somebody else's atom; the atom cannot be filed there again except by an admin.
--  * hub_ancestors / hub_descendants / is_hub_admin: the hub hierarchy walks that
--    invitation checks, revocation and withdrawal authority share. An edge is a
--    live hub membership written by the child hub's own author, both hubs live,
--    the child hub's audience 'hub' (the same edge readable_nodes uses).
--  * can_add_person: a hub whose preset is 'personal' takes adds only from its
--    author, whatever a parent hub grants.
--  * current_nodes: supersession by ANOTHER person is honoured only inside a circle
--    and the original's author can object (see below).

ALTER TABLE membership ADD COLUMN moderated boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION hub_ancestors(hub bigint) RETURNS TABLE (hub_id bigint)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up(id) AS (
        SELECT m.hub_node_id
        FROM membership m
        JOIN node c ON c.id = m.node_id
        JOIN node p ON p.id = m.hub_node_id
        WHERE m.node_id = hub AND NOT m.withdrawn AND m.audience = 'hub'
          AND m.author = c.author AND c.kind = 'hub' AND c.audience = 'hub'
          AND c.tombstoned_at IS NULL AND p.kind = 'hub' AND p.tombstoned_at IS NULL
      UNION
        SELECT m.hub_node_id
        FROM up
        JOIN membership m ON m.node_id = up.id
        JOIN node c ON c.id = m.node_id
        JOIN node p ON p.id = m.hub_node_id
        WHERE NOT m.withdrawn AND m.audience = 'hub'
          AND m.author = c.author AND c.kind = 'hub' AND c.audience = 'hub'
          AND c.tombstoned_at IS NULL AND p.kind = 'hub' AND p.tombstoned_at IS NULL
    )
    SELECT id FROM up
$$;

CREATE OR REPLACE FUNCTION hub_descendants(hub bigint) RETURNS TABLE (hub_id bigint)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE down(id) AS (
        SELECT m.node_id
        FROM membership m
        JOIN node c ON c.id = m.node_id
        JOIN node p ON p.id = m.hub_node_id
        WHERE m.hub_node_id = hub AND NOT m.withdrawn AND m.audience = 'hub'
          AND m.author = c.author AND c.kind = 'hub' AND c.audience = 'hub'
          AND c.tombstoned_at IS NULL AND p.kind = 'hub' AND p.tombstoned_at IS NULL
      UNION
        SELECT m.node_id
        FROM down
        JOIN membership m ON m.hub_node_id = down.id
        JOIN node c ON c.id = m.node_id
        JOIN node p ON p.id = m.hub_node_id
        WHERE NOT m.withdrawn AND m.audience = 'hub'
          AND m.author = c.author AND c.kind = 'hub' AND c.audience = 'hub'
          AND c.tombstoned_at IS NULL AND p.kind = 'hub' AND p.tombstoned_at IS NULL
    )
    SELECT id FROM down
$$;

-- is_hub_admin(person, hub): person authored the hub or a hub above it.
CREATE OR REPLACE FUNCTION is_hub_admin(person text, hub bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM node h WHERE h.id = hub AND h.author = person)
        OR EXISTS (
            SELECT 1 FROM hub_ancestors(hub) a JOIN node h ON h.id = a.hub_id
            WHERE h.author = person
        )
$$;

CREATE OR REPLACE FUNCTION can_add_person(person text, hub bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM node h
        WHERE h.id = hub AND h.kind = 'hub' AND h.tombstoned_at IS NULL
          AND (
              h.author = person
              OR (coalesce(h.payload->>'preset', '') <> 'personal'
                  AND coalesce(hub_access(person, hub) = 'add', false))
              OR (h.audience = 'hub' AND h.payload->>'accept' = 'anyone')
          )
    )
$$;

-- current_nodes: readable nodes minus those superseded by a node the SAME viewer
-- can read, where the supersession counts when
--   * both nodes have the same author; or
--   * (i) both are filed in at least one common live hub, each through a live
--     membership written by its own author, (ii) the original is neither a hub
--     nor an instruction and (iii) the original's author has not objected with a
--     link of rel 'disputes' from the original to the newer node.
CREATE OR REPLACE FUNCTION current_nodes(viewer bigint) RETURNS TABLE (node_id bigint)
LANGUAGE sql STABLE AS $$
    WITH rn AS MATERIALIZED (
        SELECT r.node_id FROM readable_nodes(viewer) r
    ),
    hidden AS MATERIALIZED (
        SELECT DISTINCT s.old_node_id AS id
        FROM supersedes s
        JOIN rn ON rn.node_id = s.new_node_id
        JOIN node o ON o.id = s.old_node_id
        JOIN node n ON n.id = s.new_node_id
        WHERE o.author = n.author
           OR (
                o.kind NOT IN ('hub', 'instruction')
                -- scalar sub-selects with LIMIT 1: evaluated once per supersedes
                -- row through the indexes, never flattened into a hash over all
                -- memberships of a big hub
                AND coalesce((
                    SELECT true
                    FROM membership mo
                    JOIN membership mn ON mn.hub_node_id = mo.hub_node_id
                    JOIN node h ON h.id = mo.hub_node_id
                    WHERE mo.node_id = o.id AND NOT mo.withdrawn
                      AND mo.audience = 'hub' AND mo.author = o.author
                      AND mn.node_id = n.id AND NOT mn.withdrawn
                      AND mn.audience = 'hub' AND mn.author = n.author
                      AND h.kind = 'hub' AND h.tombstoned_at IS NULL
                    LIMIT 1
                ), false)
                AND NOT coalesce((
                    SELECT true FROM link l
                    WHERE l.src_node_id = o.id AND l.rel = 'disputes'
                      AND l.dst_address = n.address
                    LIMIT 1
                ), false)
           )
    )
    SELECT r.node_id
    FROM rn r
    WHERE NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.node_id)
$$;
