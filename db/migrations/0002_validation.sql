-- 0002_validation.sql: what saving validated extractions needs.

-- A deadline is a calendar date (the time, if any, is in timezone_note). The date may be
-- unknown: the page can omit the year, or validation can reject a date it cannot verify.
alter table deadlines rename column deadline_at to deadline_date;
alter table deadlines alter column deadline_date type date using deadline_date::date;
alter table deadlines alter column deadline_date drop not null;
alter index deadlines_deadline_at_idx rename to deadlines_deadline_date_idx;

alter table scholarships
    -- The fee exactly as the page writes it ("EUR 30", "free"). application_fee_eur stays for
    -- a later conversion step; we never convert currencies by guessing.
    add column application_fee text,
    -- Why needs_review is true, e.g. "deadlines[1] quote not found on page (score 62)".
    add column review_notes text[] not null default '{}',
    -- The previous extraction (without quotes), used to find what changed next time.
    add column last_extraction jsonb;

alter table runs
    add column items_saved          integer not null default 0,
    add column items_needing_review integer not null default 0;
