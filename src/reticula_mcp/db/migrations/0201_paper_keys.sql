-- 0201: re-key paper stubs written before the wider ext_id normalisation
-- (a bare DOI or arXiv id, capitals in DOI/arXiv keys). `reticula-mcp rebuild`
-- derives the same keys from the signed payloads; this saves existing databases a
-- rebuild.
UPDATE node
SET ext_key = 'doi:' || lower(ext_key)
WHERE kind = 'paper' AND ext_key ~ '^10\.[0-9]{4,9}/[^[:space:]]+$';

UPDATE node
SET ext_key = 'arxiv:' || regexp_replace(lower(ext_key), 'v[0-9]+$', '')
WHERE kind = 'paper' AND ext_key ~* '^[0-9]{4}\.[0-9]{4,5}(v[0-9]+)?$';

UPDATE node
SET ext_key = 'doi:' || lower(substr(ext_key, 5))
WHERE kind = 'paper' AND ext_key ~* '^doi:' AND ext_key <> lower(ext_key);

UPDATE node
SET ext_key = 'arxiv:' || regexp_replace(lower(substr(ext_key, 7)), 'v[0-9]+$', '')
WHERE kind = 'paper' AND ext_key ~* '^arxiv:'
  AND ext_key <> 'arxiv:' || regexp_replace(lower(substr(ext_key, 7)), 'v[0-9]+$', '');
