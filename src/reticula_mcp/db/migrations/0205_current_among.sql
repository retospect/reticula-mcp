-- 0205 (GRAPH lane number, given by the coordinator; written by the search
-- lane): "which of these notes are current" gets a function of its own.
--
-- current_nodes(viewer) (0203) answers for every note the viewer may read. A
-- caller that holds a short list of ids had to ask for all of them to learn
-- about twenty. Now:
--
-- current_among(viewer, ids): the ids among `ids` that current_nodes(viewer)
-- returns, each once. It is current_nodes asked about the given ids only:
--   * asked:  the ids given, without NULLs and doubles;
--   * sup:    the supersedes rows whose older note is one of them;
--   * rn:     readable_nodes(viewer), cut to the ids given and to the newer
--             notes of those rows;
--   * hidden: an older note is hidden when a newer note in rn hides it by
--             supersession_hides, as in current_nodes;
--   * the answer: the ids given that are in rn and not hidden.
-- A newer note hides an older one whether or not the newer note is among the
-- ids given. A NULL array and an empty array return no row. An id that is in
-- no table, or that the viewer may not read, is not returned.
--
-- The two rules stay where they are: who may read a note is readable_nodes
-- alone, and when a newer note hides an older one is supersession_hides
-- alone. This function only composes them a second time.
-- tests/test_current_among.py compares it with current_nodes cut to the ids
-- on generated graphs, for every viewer.
--
-- The cut is written as `= ANY (ARRAY(...))` on purpose. In that form the
-- planner takes the condition into each branch of readable_nodes and reads
-- only the rows of the ids in question. tests/test_current_among_cost.py
-- fails when it stops doing so.
--
-- current_nodes, readable_nodes and supersession_hides are untouched. No
-- table, no index and no row is changed.

CREATE OR REPLACE FUNCTION current_among(viewer bigint, ids bigint[])
RETURNS TABLE (node_id bigint)
LANGUAGE sql STABLE AS $$
    WITH asked AS MATERIALIZED (
        SELECT DISTINCT a.id FROM unnest(ids) AS a(id) WHERE a.id IS NOT NULL
    ),
    sup AS MATERIALIZED (
        SELECT s.old_node_id AS old_id, s.new_node_id AS new_id
        FROM supersedes s
        WHERE s.old_node_id = ANY (ARRAY(SELECT a.id FROM asked a))
    ),
    rn AS MATERIALIZED (
        SELECT DISTINCT r.node_id
        FROM (SELECT x.node_id FROM readable_nodes(viewer) x) r
        WHERE r.node_id = ANY (ARRAY(
            SELECT a.id FROM asked a
            UNION ALL
            SELECT s.new_id FROM sup s
        ))
    ),
    hidden AS MATERIALIZED (
        SELECT DISTINCT s.old_id AS id
        FROM sup s
        JOIN rn ON rn.node_id = s.new_id
        CROSS JOIN LATERAL supersession_hides(s.old_id, s.new_id) p
        WHERE p.hides
    )
    SELECT a.id
    FROM asked a
    WHERE EXISTS (SELECT 1 FROM rn r WHERE r.node_id = a.id)
      AND NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = a.id)
$$;
