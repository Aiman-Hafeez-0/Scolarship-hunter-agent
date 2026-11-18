-- 0004_match_first_seen.sql: remember when a match first appeared.
--
-- computed_at changes every time matching runs, so it cannot tell which matches are new.
-- first_matched_at is set once, when the row is inserted, and never updated by matching.
-- The weekly digest lists matches whose first_matched_at is after the last digest.
--
-- Existing rows get the time this migration runs, so they count as already known after the
-- first digest.
alter table matches add column first_matched_at timestamptz not null default now();
