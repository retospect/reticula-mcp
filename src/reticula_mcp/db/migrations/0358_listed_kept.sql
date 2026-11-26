-- 0358 (SEARCH lane): from 0.5.0 a new account is not listed in the directory
-- until it sets "listed: yes" (settings.py, the default of "listed" is "no").
-- Every account that exists now and never set it was listed, and stays so:
-- it gets the row "listed: yes". An account that set either value keeps it.
--
-- Safe to run again: an account with a row is left alone. No table, no index
-- and no other row is changed.

INSERT INTO account_setting (account_id, name, value)
SELECT a.id, 'listed', 'yes'
FROM account a
WHERE NOT EXISTS (
    SELECT 1 FROM account_setting s
    WHERE s.account_id = a.id AND s.name = 'listed'
);
