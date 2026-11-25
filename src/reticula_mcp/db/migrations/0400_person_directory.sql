-- 0400 (CONTENT lane): the directory of people. search(kind="person") lists
-- the accounts that are listed, by handle; profile(listed=false) takes an
-- account out of that list. Every account is listed by default. One column
-- is added: no other table, column or row changes.

ALTER TABLE account ADD COLUMN listed boolean NOT NULL DEFAULT true;
