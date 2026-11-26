-- 0355 (RECENT lane): a stored private key is either a plaintext key of 32
-- bytes (before `reticula-mcp key encrypt`, or with RETICULA_KEY_FILE=none)
-- or a value of keystore.py version 1: 65 bytes starting with the byte 0x01.
-- Nothing else can be written to the column. Existing rows are all of the
-- first form, so the check holds for them.
--
-- A new constraint: no table, column or row changes.
ALTER TABLE account_key DROP CONSTRAINT IF EXISTS account_key_private_key_form;
ALTER TABLE account_key ADD CONSTRAINT account_key_private_key_form CHECK (
    private_key IS NULL
    OR octet_length(private_key) = 32
    OR (octet_length(private_key) = 65 AND get_byte(private_key, 0) = 1)
);
