-- Handles a person gave up by renaming. Nobody else may take one: a stranger
-- who took a released handle would receive the shares meant for its old holder
-- and read as that person in other people's output. The person who held it may
-- take it back. No expiry.
CREATE TABLE released_handle (
    handle      citext      PRIMARY KEY,
    person_id   text        NOT NULL,
    released_at timestamptz NOT NULL DEFAULT now()
);
