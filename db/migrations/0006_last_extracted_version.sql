-- 0006_last_extracted_version.sql: remember which prompt and model last extracted each page.
--
-- last_extracted_version is saved together with last_extracted_hash, for example
-- 'extract_v3+gemini-3.8-flash' (prompt version + the model that answered). The weekly run
-- extracts a page again when its text changed (hash) OR when this version is not one the
-- current prompt and models would produce, so a new prompt or a new model re-checks every page.
--
-- It starts empty on purpose: we do not know which prompt and model made the old extractions,
-- so every page is extracted once more on the next weekly run, then only when needed.
alter table sources add column last_extracted_version text;
