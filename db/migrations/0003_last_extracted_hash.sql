-- 0003_last_extracted_hash.sql: remember which page version was last extracted.
--
-- last_hash is the fingerprint of the last page we fetched (the `fetch` command sets it too).
-- last_extracted_hash is the fingerprint of the last page whose extraction was validated and
-- saved without errors. The weekly run compares with last_extracted_hash, so a page that was
-- fetched but never extracted (or whose extraction failed) is extracted on the next run.
--
-- It starts empty on purpose: every page counts as "not extracted yet" once, so pages that
-- were only fetched before extraction existed are finally sent to the LLM.
alter table sources add column last_extracted_hash text;
