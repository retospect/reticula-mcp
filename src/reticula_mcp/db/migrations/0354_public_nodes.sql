-- 0354 (RECENT lane): public_nodes(), what the public page /hubs may show to
-- a reader who is not signed in. Beside readable_nodes, with no viewer.
--
-- A hub is public when its own author granted it to 'public' with mode
-- 'read' (share(hub=..., person="public", mode="read")); 'public' is a
-- grantee that is never a person ID, so readable_hubs never matches it for
-- any account. Shown, and nothing else:
--
-- - the public hub, while it is live and in audience 'hub';
-- - its sub-hubs filed by their own author when that author is the public
--   hub's author (a member's own sub-hub is not made public by the owner's
--   share: that member shares it themselves), recursively;
-- - in each of these hubs, the notes and papers filed by their own author
--   when that author is the public hub's author, live, in audience 'hub',
--   and not superseded by a live note of that same author (the page shows
--   only the owner's notes, so only the owner's correction takes one off;
--   another member's superseding note does not).
--
-- Not shown: notes by other members, private notes, deleted or superseded
-- notes, instructions, debug notes, handles and person IDs (the page reads
-- only title and address of these ids).
--
-- Returns the tree: (node_id, parent_hub_id); parent_hub_id is NULL for a
-- public hub itself. A note filed in two shown hubs comes twice.
--
-- A new function: no table, column, index or row changes.
CREATE OR REPLACE FUNCTION public_nodes()
RETURNS TABLE (node_id bigint, parent_hub_id bigint)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE roots AS (
        SELECT h.id, h.author AS owner
        FROM hub_grant g
        JOIN node h ON h.id = g.hub_node_id
        WHERE g.grantee = 'public' AND g.mode = 'read' AND g.granted_by = h.author
          AND h.kind = 'hub' AND h.tombstoned_at IS NULL AND h.audience = 'hub'
    ),
    tree(hub_id, owner, parent) AS (
        SELECT r.id, r.owner, NULL::bigint FROM roots r
      UNION
        SELECT c.id, t.owner, t.hub_id
        FROM tree t
        JOIN membership m ON m.hub_node_id = t.hub_id
        JOIN node c ON c.id = m.node_id
        WHERE NOT m.withdrawn AND m.audience = 'hub'
          AND m.author = c.author AND c.author = t.owner
          AND c.kind = 'hub' AND c.tombstoned_at IS NULL AND c.audience = 'hub'
    )
    SELECT t.hub_id, t.parent FROM tree t
  UNION
    SELECT n.id, t.hub_id
    FROM tree t
    JOIN membership m ON m.hub_node_id = t.hub_id
    JOIN node n ON n.id = m.node_id
    WHERE NOT m.withdrawn AND m.audience = 'hub'
      AND m.author = n.author AND n.author = t.owner
      AND n.kind IN ('note', 'paper')
      AND n.tombstoned_at IS NULL AND n.audience = 'hub'
      AND NOT EXISTS (
            SELECT 1 FROM supersedes s JOIN node w ON w.id = s.new_node_id
            WHERE s.old_node_id = n.id AND w.tombstoned_at IS NULL
              AND w.author = t.owner)
$$;
