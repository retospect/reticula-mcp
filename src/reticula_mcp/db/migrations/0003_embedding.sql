-- 0003: local embedding index. OWNED BY the search lane (it may reshape this in
-- 0300-0399). Local, rebuildable, never shared: dropping it changes no shared
-- data. `vec` has no fixed dimension; build per-model indexes later.
CREATE TABLE embedding (
    node_id      bigint NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    model        text   NOT NULL,
    dim          integer NOT NULL,
    vec          vector NOT NULL,
    version_hash text   NOT NULL,   -- head version the vector was computed from
    PRIMARY KEY (node_id, model)
);
