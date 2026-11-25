-- 0005: the visibility functions. readable_nodes is the ONE place that answers
-- "which notes may this account read". Every read path (search, get, resolve_ref,
-- can_read) goes through it; nothing else may re-implement the rule.
--
-- FINAL SEMANTICS (the graph lane replaces the stub body below with
-- CREATE OR REPLACE FUNCTION in its own migration; the signature and the
-- column name node_id are frozen):
--   * the viewer's own atoms are readable;
--   * an atom with audience 'hub' is readable when it has a non-withdrawn
--     membership (membership.audience = 'hub') in a hub on which the viewer
--     holds a grant with mode 'read' or 'add', directly or on any ANCESTOR hub
--     (hub a is an ancestor of hub b when b is, transitively, a member of a);
--   * a hub atom is itself readable under the same rule (membership of the hub
--     in its parent hubs, or a grant on it or an ancestor);
--   * audience 'private' is readable by its author only, always;
--   * tombstoned atoms (tombstoned_at IS NOT NULL) are never readable.
--
-- STUB BODY (foundation): the viewer's own non-tombstoned atoms.
CREATE FUNCTION readable_nodes(viewer bigint) RETURNS TABLE (node_id bigint)
LANGUAGE sql STABLE AS $$
    SELECT n.id
    FROM node n
    JOIN account a ON a.person_id = n.author
    WHERE a.id = viewer
      AND n.tombstoned_at IS NULL
$$;

-- current_nodes: readable nodes minus those superseded by an atom the SAME
-- viewer can read (a private superseder must not hide the original from
-- others). Final; builds on readable_nodes, so it needs no change when the
-- graph lane replaces readable_nodes.
CREATE FUNCTION current_nodes(viewer bigint) RETURNS TABLE (node_id bigint)
LANGUAGE sql STABLE AS $$
    SELECT r.node_id
    FROM readable_nodes(viewer) r
    WHERE NOT EXISTS (
        SELECT 1
        FROM supersedes s
        JOIN readable_nodes(viewer) newer ON newer.node_id = s.new_node_id
        WHERE s.old_node_id = r.node_id
    )
$$;
