-- 0203 (GRAPH lane number, given by the coordinator; written by the search
-- lane): two rules that were written out in more than one place become one
-- function each. First the supersession rule for one pair of nodes; at the end
-- of the file shown_access, the access a person is shown for a hub.
--
-- Until now current_nodes carried the rule inline and the read side (get)
-- carried a copy of it to say "superseded by" or "corrected by". Both call
-- supersession_hides now. Nothing changes in what either returns.
--
-- supersession_hides(old, new): one row saying whether the newer node hides the
-- original from those who can read the newer one; no row when either node does
-- not exist. It hides it when
--   * both nodes have the same author; or
--   * (i) both are filed in at least one common live hub, each through a live
--     membership written by its own author, (ii) the original is neither a hub
--     nor an instruction and (iii) the original's author has not objected with a
--     link of rel 'disputes' from the original to the newer node.
--
-- It is a table function on purpose. Called in FROM (LATERAL), a SQL function
-- of this shape is inlined by the planner, so current_nodes keeps the plan it
-- had; a scalar function with sub-selects is never inlined and would be run
-- once per supersedes row. tests/test_search_review.py pins the inlining.

CREATE OR REPLACE FUNCTION supersession_hides(old_id bigint, new_id bigint)
RETURNS TABLE (hides boolean)
LANGUAGE sql STABLE AS $$
    SELECT o.author = n.author
        OR (
             o.kind NOT IN ('hub', 'instruction')
             -- scalar sub-selects with LIMIT 1: evaluated once per pair through
             -- the indexes, never flattened into a hash over all memberships
             -- of a big hub
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
    FROM node o, node n
    WHERE o.id = old_id AND n.id = new_id
$$;

-- current_nodes: readable nodes minus those superseded by a node the SAME viewer
-- can read, where the pair counts by supersession_hides.
CREATE OR REPLACE FUNCTION current_nodes(viewer bigint) RETURNS TABLE (node_id bigint)
LANGUAGE sql STABLE AS $$
    WITH rn AS MATERIALIZED (
        SELECT r.node_id FROM readable_nodes(viewer) r
    ),
    hidden AS MATERIALIZED (
        SELECT DISTINCT s.old_node_id AS id
        FROM supersedes s
        JOIN rn ON rn.node_id = s.new_node_id
        CROSS JOIN LATERAL supersession_hides(s.old_node_id, s.new_node_id) p
        WHERE p.hides
    )
    SELECT r.node_id
    FROM rn r
    WHERE NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.node_id)
$$;

-- shown_access(person, hub): the access to SHOW a person for a hub and to gate
-- the list of who has access on. hub_access inherits 'add' from a parent hub
-- also into a personal hub, where only the owner adds (can_add_person). Such a
-- person reads the hub and nothing more: they are shown 'read' and get the
-- reader's view of the access list. The hub listing of search calls it. The
-- access line of get (a CASE in its query) and graph/access.py shown_access (in
-- Python) state the same rule and can call it when they are next changed.
CREATE OR REPLACE FUNCTION shown_access(person text, hub bigint) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN a.mode = 'add' AND NOT can_add_person(person, hub)
                THEN 'read' ELSE a.mode END
    FROM (SELECT hub_access(person, hub) AS mode) a
$$;
